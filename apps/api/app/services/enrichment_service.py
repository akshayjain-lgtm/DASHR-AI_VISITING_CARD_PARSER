"""Orchestrates the fan-out over every public-source lookup for one
`Company`, flattens the results onto `CompanySignals`, and writes one
`CompanyEnrichment` audit row per source that actually returned data.

Every lookup below is wrapped (via `_run_lookup`) in its own try/except:
one source being down, blocked, or erroring must never block the others
from populating their columns (see `.claude/specs/07-data-enrichment.md`).
"""
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.company_enrichment import CompanyEnrichment
from app.models.company_signals import CompanySignals
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


def run_all_signal_lookups(
    db: Session,
    company: Company,
    gst_number: str | None,
    email_domain: str | None = None,
    website: str | None = None,
    products_offered: str | None = None,
    address: str | None = None,
) -> tuple[CompanySignals, bool]:
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

    # 4. LinkedIn company page
    data.update(_run_lookup(
        db, company.company_id, "linkedin",
        lambda: firmographics_provider.get_firmographics_provider().lookup_linkedin(
            name, company.website
        ),
        ["linkedin_employee_count", "linkedin_follower_count"],
    ))

    # 5. Company website — only when there's a website to fetch
    if company.website:
        data.update(_run_lookup(
            db, company.company_id, "website",
            lambda: website_signal_provider.get_website_signal_provider().lookup(company.website),
            ["product_lines_summary", "plant_size_signal"],
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

    # any_signal_found must be computed before news_distress_detected is
    # added below — that field is always a real bool (True or False, never
    # None), so including it here would make any_signal_found permanently
    # True regardless of whether any lookup actually found anything.
    any_signal_found = any(value is not None for value in data.values())

    # Shared distress override (spec "v2 criteria" #5/#6): computed here,
    # not inside either provider, since it depends on both lookups' results
    # together — can't go through the generic per-field _run_lookup
    # mechanism cleanly.
    significant_decline = (
        qoq_growth_pct is not None and qoq_growth_pct <= _SIGNIFICANT_DECLINE_THRESHOLD_PCT
    )
    data["news_distress_detected"] = news_distress_from_articles or significant_decline

    signals = db.get(CompanySignals, company.company_id)
    if signals is None:
        signals = CompanySignals(company_id=company.company_id)
        db.add(signals)
    for key, value in data.items():
        setattr(signals, key, value)
    signals.hiring_signal = classify_hiring_signal(signals.active_job_postings_count)
    signals.estimated_revenue_band = classify_revenue_band(
        signals.udyam_category, signals.paid_up_capital
    )
    _apply_marketplace_vintage_years(signals)
    signals.updated_at = datetime.now(timezone.utc)

    return signals, any_signal_found


def rerun_supplier_profile_lookup(
    db: Session, company: Company, catalog_url: str
) -> CompanySignals:
    """Re-runs lookup #12 (IndiaMART supplier-profile Apify call, see
    run_all_signal_lookups above) against a user-corrected catalog_url,
    sharing its exact field list/audit-row behavior, without re-running
    lookups #1-11. Used by a `FieldCorrection` to catalog_url — see
    .claude/specs/20-field-correction.md. Does not commit; mirrors
    run_all_signal_lookups' caller-commits convention."""
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
    signals.updated_at = datetime.now(timezone.utc)

    return signals
