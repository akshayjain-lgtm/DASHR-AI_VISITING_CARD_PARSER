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
    registry_provider,
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


def run_all_signal_lookups(
    db: Session,
    company: Company,
    gst_number: str | None,
    email_domain: str | None = None,
    website: str | None = None,
    products_offered: str | None = None,
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
            name, email_domain, website or company.website, products_offered
        ),
        ["marketplace_vintage_years", "marketplace_verified_badge",
         "marketplace_located_in_industrial_area", "catalog_url"],
    ))

    any_signal_found = any(value is not None for value in data.values())

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
    signals.updated_at = datetime.now(timezone.utc)

    return signals, any_signal_found
