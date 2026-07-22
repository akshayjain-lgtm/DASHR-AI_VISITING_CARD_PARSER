"""Orchestrates the fan-out over every public-source lookup for one
`Company`, flattens the results onto `CompanySignals`, and writes one
`CompanyEnrichment` audit row per source that actually returned data.

Every lookup below is wrapped (via `_run_lookup`) in its own try/except:
one source being down, blocked, or erroring must never block the others
from populating their columns (see `.claude/specs/07-data-enrichment.md`).
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.company_enrichment import CompanyEnrichment
from app.models.company_signals import CompanySignals
from app.models.organization import Organization
from app.models.seller_profile import SellerProfile
from app.models.user import User
from app.services.enrichment_providers import (
    compliance_provider,
    firmographics_provider,
    gem_tender_provider,
    hiring_signal_provider,
    local_presence_provider,
    news_signal_provider,
    news_summary_provider,
    registry_provider,
    share_price_provider,
    trade_data_provider,
    website_signal_provider,
)
from app.services.company_name_utils import normalize_company_name

logger = logging.getLogger(__name__)

# Named thresholds, not inline magic numbers, so hiring-signal cutoffs are
# one place to tune later — same rationale as CLAUDE.md's "scoring weights
# live as data" rule applied to signal derivation, mirroring designation.py.
_HIRING_SIGNAL_EXPANDING_THRESHOLD = 3
_HIRING_SIGNAL_STABLE_THRESHOLD = 1

# Udyam's own turnover-threshold-based classification doubles as a free,
# public revenue-band proxy — the only free public source that publishes
# anything usable for this, since Probe42/Tofler/CorpVeda are paid-only
# (see spec Overview) and were dropped from this feature entirely.
_UDYAM_REVENUE_BAND_BY_CATEGORY: dict[str, str] = {
    "micro": "< ₹5 Cr turnover (Udyam micro)",
    "small": "₹5–50 Cr turnover (Udyam small)",
    "medium": "₹50–250 Cr turnover (Udyam medium)",
}

# Fallback capital-based bands, only used when Udyam didn't answer.
_PAID_UP_CAPITAL_BAND_THRESHOLDS: tuple[tuple[Decimal, str], ...] = (
    (Decimal("500000"), "< ₹5 lakh paid-up capital"),
    (Decimal("10000000"), "₹5 lakh–1 Cr paid-up capital"),
    (Decimal("100000000"), "₹1–10 Cr paid-up capital"),
)
_PAID_UP_CAPITAL_TOP_BAND = "> ₹10 Cr paid-up capital"

# Lead-scoring v2's shared expansion/revenue-growth distress override (see
# .claude/specs/10-lead-scoring.md "v2 rework only") fires when a QOQ
# share-price change is at or below this threshold.
_SIGNIFICANT_DECLINE_THRESHOLD_PCT = Decimal("-10")

# Tiered CompanySignals freshness clocks (see
# .claude/specs/24-company-linkage-tiered-expiry.md) — factual data (registry/
# compliance/revenue/website-derived product lines) rarely changes and gets a
# long TTL; dynamic data (firmographics/growth/news/local-presence) moves
# faster and gets a shorter one. Independent of the per-lead billed cooldown
# in lead_cooldown_service.py, which is a rate-limit concept, not a
# freshness one.
_FACTUAL_SIGNALS_TTL_DAYS = 180
_DYNAMIC_SIGNALS_TTL_DAYS = 90


def stale_tiers(signals: CompanySignals | None) -> list[str]:
    """Which of "factual"/"dynamic" need a re-fetch, based on an *existing*
    CompanySignals row's own clocks — a None/expired timestamp on that row
    counts as stale for that tier. A None `signals` row itself returns []
    (nothing to refresh): callers only ever pass this for an already-
    `"enriched"` Company, which in practice always has a CompanySignals row
    (run_all_signal_lookups creates one unconditionally before an
    enrichment run ever reaches "enriched"); a None row in that situation is
    an inconsistent state this function treats conservatively rather than
    assuming both tiers need fetching. Pure function — never reads or
    writes the DB, never mutates `signals`."""
    if signals is None:
        return []
    now = datetime.now(timezone.utc)
    tiers = []
    if (
        signals.factual_fetched_at is None
        or now - signals.factual_fetched_at >= timedelta(days=_FACTUAL_SIGNALS_TTL_DAYS)
    ):
        tiers.append("factual")
    if (
        signals.dynamic_fetched_at is None
        or now - signals.dynamic_fetched_at >= timedelta(days=_DYNAMIC_SIGNALS_TTL_DAYS)
    ):
        tiers.append("dynamic")
    return tiers


def match_linked_org(db: Session, company: Company) -> Organization | None:
    """Matches a scanned Company against a registered DASHR org by comparing
    normalized company names — company.normalized_name against each
    SellerProfile.company_name, normalized the same way
    (company_name_utils.normalize_company_name, shared with
    extraction_service.py rather than duplicated). Domain-based matching is
    out of scope: SellerProfile carries no domain/website field today.

    Returns None (never links) when zero or more than one distinct
    Organization matches — an ambiguous link would be worse than staying
    unlinked, and this is re-checked on every enrichment run in case one
    side's declared name changes. Ambiguity is computed on distinct
    Organization.org_id, not distinct SellerProfile rows, so an admin and a
    member at the same org sharing a declared company_name is never counted
    as ambiguous.
    """
    if not company.normalized_name:
        return None

    rows = db.execute(
        select(Organization, SellerProfile)
        .join(User, User.org_id == Organization.org_id)
        .join(SellerProfile, SellerProfile.user_id == User.user_id)
        .where(SellerProfile.company_name.isnot(None))
    ).all()

    matched_orgs: dict[uuid.UUID, Organization] = {}
    for org, profile in rows:
        if normalize_company_name(profile.company_name) == company.normalized_name:
            matched_orgs[org.org_id] = org

    if len(matched_orgs) != 1:
        return None
    return next(iter(matched_orgs.values()))


def _preferred_product_lines(db: Session, org_id: uuid.UUID) -> str | None:
    """The linked org's own declared product lines, preferring its admin's
    SellerProfile when populated, falling back to any other member's
    populated one. Returns None (never an empty string) when no member has
    filled product_lines in yet, so the caller can fall through to the
    normal website-scrape behavior instead of writing a blank value."""
    profiles = db.execute(
        select(SellerProfile, User.role)
        .join(User, User.user_id == SellerProfile.user_id)
        .where(User.org_id == org_id, SellerProfile.product_lines.isnot(None))
    ).all()
    for profile, role in profiles:
        if role == "admin" and profile.product_lines:
            return profile.product_lines
    for profile, _role in profiles:
        if profile.product_lines:
            return profile.product_lines
    return None


def classify_hiring_signal(active_job_postings_count: int | None) -> str | None:
    """Returns None only when no hiring source answered at all; otherwise
    one of "expanding"/"stable"/"unknown", threshold-bucketed here (not
    inline) so the postings-count cutoffs are one place to tune later."""
    if active_job_postings_count is None:
        return None
    if active_job_postings_count >= _HIRING_SIGNAL_EXPANDING_THRESHOLD:
        return "expanding"
    if active_job_postings_count >= _HIRING_SIGNAL_STABLE_THRESHOLD:
        return "stable"
    return "unknown"


def classify_revenue_band(
    udyam_category: str | None, paid_up_capital: Decimal | None
) -> str | None:
    """Bucketed from Udyam's own turnover-threshold-based category first (a
    real public classification), falling back to a paid-up-capital-based
    rough band only when Udyam didn't answer. Returns None when neither
    signal is present — no free source publishes an exact revenue figure,
    so there is never a numeric fallback here."""
    if udyam_category and udyam_category in _UDYAM_REVENUE_BAND_BY_CATEGORY:
        return _UDYAM_REVENUE_BAND_BY_CATEGORY[udyam_category]
    if paid_up_capital is not None:
        for threshold, band in _PAID_UP_CAPITAL_BAND_THRESHOLDS:
            if paid_up_capital < threshold:
                return band
        return _PAID_UP_CAPITAL_TOP_BAND
    return None


def _run_lookup(
    db: Session,
    company_id: uuid.UUID,
    source_default: str,
    call: Callable[[], Any],
    fields: list[str],
) -> dict[str, Any]:
    """Runs one source lookup in isolation: calls `call()`, records a
    `CompanyEnrichment` audit row if it returned anything, and extracts
    `fields` off the result into a plain dict. Returns `{}` (contributing
    nothing) on any exception, logged and swallowed here so one source
    failing never blocks the others — the single place this isolation is
    implemented, reused by every lookup below instead of a hand-copied
    try/except per call site."""
    try:
        result = call()
        source = getattr(result, "source_tag", None) or source_default
        payload = getattr(result, "raw_payload", None)
        if payload is not None:
            db.add(CompanyEnrichment(company_id=company_id, source=source, payload=payload))
        return {field: getattr(result, field) for field in fields}
    except Exception:
        logger.exception(
            "enrichment_service: %s lookup failed for company_id=%s", source_default, company_id
        )
        return {}


# Lookup #12's field list — shared by run_all_signal_lookups (the original
# fan-out, gated on a same-run catalog_url) and rerun_supplier_profile_lookup
# (a correction-triggered re-fetch against an already-known catalog_url) so
# the two call sites can't drift out of sync with each other.
_SUPPLIER_PROFILE_FIELDS = [
    "marketplace_verified_badge", "indiamart_rating", "indiamart_rating_count",
    "indiamart_member_since_year", "indiamart_business_type",
    "indiamart_employee_count_band", "indiamart_annual_turnover_band",
    "indiamart_year_established", "indiamart_gst_number",
    "indiamart_gst_registration_year", "indiamart_call_response_rate",
]


def _apply_marketplace_vintage_years(signals: CompanySignals) -> None:
    """marketplace_vintage_years is derived from the supplier-profile
    lookup's indiamart_member_since_year, same "bucketed here, not in the
    provider" convention as hiring_signal/estimated_revenue_band. Reads
    signals.indiamart_member_since_year (already set by the caller's
    setattr loop), not a lookup's raw data dict — that dict is fully
    consumed by that loop. Shared by run_all_signal_lookups and
    rerun_supplier_profile_lookup so the current_year - member_since_year
    formula lives in exactly one place."""
    if signals.indiamart_member_since_year is not None:
        vintage = datetime.now(timezone.utc).year - signals.indiamart_member_since_year
        if vintage >= 0:
            signals.marketplace_vintage_years = vintage


def _run_factual_lookups(
    db: Session, company: Company, gst_number: str | None
) -> dict[str, Any]:
    """Lookups #1/#2/#3/#5 — registry, GSTIN, Udyam, and website-derived
    product lines/plant size. #5 is replaced by the linked org's own
    declared SellerProfile.product_lines (never scraped) when
    company.linked_org_id is set and that org has one on file — CLAUDE.md's
    "prefer declared data over generic third-party data" rule, mapped onto
    the one CompanySignals column that exists for it. plant_size_signal has
    no SellerProfile equivalent, so it's simply not populated on that path
    (never fabricated, never re-scraped once linked)."""
    data: dict[str, Any] = {}
    name = company.name or ""

    # 1. Registry — MCA public master-data search, falling back to Zauba Corp
    data.update(_run_lookup(
        db, company.company_id, "mca",
        lambda: registry_provider.get_registry_provider().lookup(name),
        ["cin", "incorporation_date", "registry_status", "registered_address",
         "authorized_capital", "paid_up_capital"],
    ))

    # 2. GSTIN verification — only when this company has a captured GSTIN to check
    if gst_number:
        data.update(_run_lookup(
            db, company.company_id, "gstin",
            lambda: compliance_provider.get_compliance_provider().verify_gstin(gst_number),
            ["gstin_verified", "gstin_status"],
        ))

    # 3. Udyam/MSME registration — attempted off name alone even without a GSTIN
    data.update(_run_lookup(
        db, company.company_id, "udyam",
        lambda: compliance_provider.get_compliance_provider().lookup_udyam(name, gst_number),
        ["udyam_registered", "udyam_category"],
    ))

    # 5. Company website product lines — skipped (never fetched from the
    # linked org's own website) when a linked org's declared data is
    # available for this run.
    preferred_product_lines = (
        _preferred_product_lines(db, company.linked_org_id)
        if company.linked_org_id is not None
        else None
    )
    if preferred_product_lines is not None:
        data["product_lines_summary"] = preferred_product_lines
    elif company.website:
        data.update(_run_lookup(
            db, company.company_id, "website",
            lambda: website_signal_provider.get_website_signal_provider().lookup(company.website),
            ["product_lines_summary", "plant_size_signal"],
        ))

    return data


def _run_dynamic_lookups(
    db: Session,
    company: Company,
    email_domain: str | None,
    website: str | None,
    products_offered: str | None,
    address: str | None,
) -> tuple[dict[str, Any], bool]:
    """Lookups #4, #6-#14 — LinkedIn, hiring, GeM, Volza, Google News, Google
    Maps, the whole IndiaMART block, the AI news summary, and share price.
    Returns (data, any_signal_found) — any_signal_found is computed before
    news_distress_detected is folded into `data` below: that field is
    always a real bool (True or False, never None), so including it first
    would make any_signal_found permanently True regardless of whether any
    lookup actually found anything."""
    data: dict[str, Any] = {}
    name = company.name or ""

    # 4. LinkedIn company page
    data.update(_run_lookup(
        db, company.company_id, "linkedin",
        lambda: firmographics_provider.get_firmographics_provider().lookup_linkedin(
            name, company.website
        ),
        ["linkedin_employee_count", "linkedin_follower_count"],
    ))

    # 6. Hiring signal — public Naukri / LinkedIn job-search pages
    data.update(_run_lookup(
        db, company.company_id, "naukri",
        lambda: hiring_signal_provider.get_hiring_signal_provider().lookup(name),
        ["active_job_postings_count"],
    ))

    # 7. GeM portal public tender/bid-history search
    data.update(_run_lookup(
        db, company.company_id, "gem",
        lambda: gem_tender_provider.get_gem_tender_provider().lookup(name),
        ["gem_tender_count", "gem_total_tender_value"],
    ))

    # 8. Volza/ImportGenius public teaser numbers
    data.update(_run_lookup(
        db, company.company_id, "volza",
        lambda: trade_data_provider.get_trade_data_provider().lookup(name),
        ["import_export_activity", "shipment_count_last_12m"],
    ))

    # 9. Google News public RSS feed
    data.update(_run_lookup(
        db, company.company_id, "google_news",
        lambda: news_signal_provider.get_news_signal_provider().lookup(name),
        ["recent_news_signals"],
    ))

    # 10. Google Maps public search results
    data.update(_run_lookup(
        db, company.company_id, "google_maps",
        lambda: local_presence_provider.get_local_presence_provider().lookup_places(
            name, company.hq_city
        ),
        ["google_rating", "google_review_count"],
    ))

    # 11. IndiaMART/TradeIndia/JustDial public directory listing
    data.update(_run_lookup(
        db, company.company_id, "indiamart",
        lambda: local_presence_provider.get_local_presence_provider().lookup_marketplace(
            name, email_domain, website or company.website, products_offered, address
        ),
        ["marketplace_vintage_years", "marketplace_verified_badge",
         "marketplace_located_in_industrial_area", "catalog_url"],
    ))

    # 12. IndiaMART supplier-profile page (a second, distinct Apify actor) —
    # only when lookup #11 found a catalog_url in this same run; never spend
    # this second billed call chasing a URL we don't have.
    if data.get("catalog_url"):
        data.update(_run_lookup(
            db, company.company_id, "indiamart_supplier_profile",
            lambda: local_presence_provider.get_local_presence_provider().lookup_supplier_profile(
                data["catalog_url"]
            ),
            _SUPPLIER_PROFILE_FIELDS,
        ))

    # 13. Combined AI summary of multiple full news articles, feeding
    # lead-scoring v2's expansion_signal_score/revenue_growth_score only
    # (see .claude/specs/10-lead-scoring.md "v2 rework only"). Lookup #9's
    # recent_news_signals write above is untouched, still serving this
    # module's own general enrichment/display purposes. distress_detected
    # is captured locally, not merged into `data` directly (no such
    # CompanySignals column) — combined with the share-price check below.
    # `tags` is persisted as news_tags so scoring.py can read Claude's own
    # classification directly, instead of re-deriving it via a second,
    # independently-maintained keyword scan of news_summary's plain text.
    news_summary_fields = _run_lookup(
        db, company.company_id, "news_summary",
        lambda: news_summary_provider.get_news_summary_provider().summarize(name, company.hq_city),
        ["news_summary", "tags", "distress_detected"],
    )
    news_distress_from_articles = bool(news_summary_fields.get("distress_detected"))
    if news_summary_fields.get("news_summary"):
        data["news_summary"] = news_summary_fields["news_summary"]
        data["news_summary_generated_at"] = datetime.now(timezone.utc)
        data["news_tags"] = list(news_summary_fields.get("tags") or [])

    # 14. Share-price QOQ lookup — only meaningful for a publicly-listed
    # company; contributes nothing extra for the common unlisted case.
    share_price_fields = _run_lookup(
        db, company.company_id, "share_price",
        lambda: share_price_provider.get_share_price_provider().lookup(name, company.hq_city),
        ["is_publicly_listed", "qoq_growth_pct"],
    )
    qoq_growth_pct = share_price_fields.get("qoq_growth_pct")
    if share_price_fields.get("is_publicly_listed"):
        data["share_price_qoq_growth_pct"] = qoq_growth_pct

    any_signal_found = any(value is not None for value in data.values())

    # Shared distress override (spec "v2 criteria" #5/#6): computed here,
    # not inside either provider, since it depends on both lookups' results
    # together — can't go through the generic per-field _run_lookup
    # mechanism cleanly.
    significant_decline = (
        qoq_growth_pct is not None and qoq_growth_pct <= _SIGNIFICANT_DECLINE_THRESHOLD_PCT
    )
    data["news_distress_detected"] = news_distress_from_articles or significant_decline

    return data, any_signal_found


def run_all_signal_lookups(
    db: Session,
    company: Company,
    gst_number: str | None,
    email_domain: str | None = None,
    website: str | None = None,
    products_offered: str | None = None,
    address: str | None = None,
    refresh_tiers: list[str] | None = None,
) -> tuple[CompanySignals, bool]:
    """Runs the public-source fan-out for one Company, gated by
    refresh_tiers: None means a first-ever run (both tiers, unconditional —
    the original one-shot behavior); an explicit list (possibly empty, when
    only the per-lead cooldown reason unlocked the call — see
    lead_cooldown_service.py) means a refresh, re-fetching only the tier(s)
    named. any_signal_found is only meaningful for a first-ever run — a
    refresh caller (enrich_company_task) never re-derives
    Company.enrichment_status from it.

    The org-linkage match always runs first, regardless of refresh_tiers —
    cheap and DB-only, and CLAUDE.md requires it run "before falling back to
    third-party firmographics providers" so a newly-linked org's declared
    product lines can be preferred on this same call.
    """
    tiers = {"factual", "dynamic"} if refresh_tiers is None else set(refresh_tiers)

    if company.linked_org_id is None:
        matched_org = match_linked_org(db, company)
        if matched_org is not None:
            company.linked_org_id = matched_org.org_id

    signals = db.get(CompanySignals, company.company_id)
    if signals is None:
        signals = CompanySignals(company_id=company.company_id)
        db.add(signals)

    now = datetime.now(timezone.utc)
    factual_data: dict[str, Any] = {}
    dynamic_data: dict[str, Any] = {}
    any_signal_found = False

    if "factual" in tiers:
        factual_data = _run_factual_lookups(db, company, gst_number)
        any_signal_found = any_signal_found or any(v is not None for v in factual_data.values())

    if "dynamic" in tiers:
        dynamic_data, dynamic_any_signal_found = _run_dynamic_lookups(
            db, company, email_domain, website, products_offered, address
        )
        any_signal_found = any_signal_found or dynamic_any_signal_found

    for key, value in {**factual_data, **dynamic_data}.items():
        setattr(signals, key, value)

    if "factual" in tiers:
        signals.estimated_revenue_band = classify_revenue_band(
            signals.udyam_category, signals.paid_up_capital
        )
        signals.factual_fetched_at = now

    if "dynamic" in tiers:
        signals.hiring_signal = classify_hiring_signal(signals.active_job_postings_count)
        _apply_marketplace_vintage_years(signals)
        signals.dynamic_fetched_at = now

    return signals, any_signal_found


def rerun_supplier_profile_lookup(
    db: Session, company: Company, catalog_url: str
) -> CompanySignals:
    """Re-runs lookup #12 (IndiaMART supplier-profile Apify call, see
    run_all_signal_lookups above) against a user-corrected catalog_url,
    sharing its exact field list/audit-row behavior, without re-running
    lookups #1-11. Used by a `FieldCorrection` to catalog_url — see
    .claude/specs/20-field-correction.md. Does not commit; mirrors
    run_all_signal_lookups' caller-commits convention.

    Bumps dynamic_fetched_at (the supplier-profile fields are dynamic-tier
    data) even though only this one lookup ran, not the full dynamic group —
    a reasonable simplification: this data genuinely was just re-fetched, so
    the dynamic tier is fresh as of now, even though nothing else in that
    tier was touched on this correction-triggered call."""
    signals = db.get(CompanySignals, company.company_id)
    if signals is None:
        signals = CompanySignals(company_id=company.company_id)
        db.add(signals)
    signals.catalog_url = catalog_url  # idempotent with card_service's own synchronous set

    data = _run_lookup(
        db, company.company_id, "indiamart_supplier_profile",
        lambda: local_presence_provider.get_local_presence_provider().lookup_supplier_profile(
            catalog_url
        ),
        _SUPPLIER_PROFILE_FIELDS,
    )
    for key, value in data.items():
        setattr(signals, key, value)

    _apply_marketplace_vintage_years(signals)
    signals.dynamic_fetched_at = datetime.now(timezone.utc)

    return signals
