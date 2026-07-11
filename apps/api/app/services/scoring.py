"""Pure, explainable lead-scoring logic — no DB/Celery imports here, only
reads off already-loaded ORM objects. Weights, bands, and keyword lists are
module-level data (same rationale as CLAUDE.md's scoring-weights rule and
enrichment_service.py's threshold constants), never inline in the caller.

calculate_score() is called by workers/scoring_processing.py, which is the
only place that persists its result.
"""
from app.models.company import Company
from app.models.company_signals import CompanySignals
from app.models.seller_profile import SellerProfile
from app.models.visiting_card import VisitingCard

_SCORE_VERSION = "v1"

# --- designation_score (max 30) — reads VisitingCard.designation_level,
# already classified by designation.classify() at extraction time; never
# re-derived here. ---
_DESIGNATION_LEVEL_SCORES: dict[str, int] = {
    "c_level": 30,
    "director": 22,
    "manager": 14,
    "individual_contributor": 6,
}

# --- company_size_score (max 25) — banded off CompanySignals.linkedin_employee_count,
# falling back to udyam_category only when no employee count was found. ---
_EMPLOYEE_COUNT_BAND_THRESHOLDS: tuple[tuple[int, int], ...] = (
    (500, 25),
    (100, 18),
    (20, 10),
    (1, 4),
)
_UDYAM_CATEGORY_FALLBACK_SCORES: dict[str, int] = {"medium": 15, "small": 8}

# --- industry_fit_score (max 25) — keyword-overlap proxy; no NAICS/SIC
# classification exists anywhere in this codebase yet (see 10-lead-scoring.md). ---
_INDUSTRY_FIT_OVERLAP_SCORES: tuple[tuple[int, int], ...] = (
    (3, 25),
    (2, 15),
    (1, 8),
)

# --- momentum_signal_score (max 10) — sums to exactly 10 by construction. ---
_HIRING_SIGNAL_EXPANDING_POINTS = 4
_GEM_TENDER_POINTS = 2
_IMPORT_EXPORT_POINTS = 2
_MARKETPLACE_VERIFIED_POINTS = 2

# --- remark_signal_score (max 10) — static positive-intent keyword scan. ---
_POSITIVE_INTENT_KEYWORDS: tuple[str, ...] = (
    "follow up",
    "urgent",
    "interested",
    "budget",
    "decision",
    "next week",
)
_REMARK_MATCH_SCORE = 10
_REMARK_NO_MATCH_SCORE = 3


def _designation_score(designation_level: str | None) -> int:
    if designation_level is None:
        return 0
    return _DESIGNATION_LEVEL_SCORES.get(designation_level, 0)


def _company_size_score(signals: CompanySignals | None) -> int:
    if signals is None:
        return 0
    if signals.linkedin_employee_count is not None:
        for min_employees, score in _EMPLOYEE_COUNT_BAND_THRESHOLDS:
            if signals.linkedin_employee_count >= min_employees:
                return score
        return 0
    if signals.udyam_category:
        return _UDYAM_CATEGORY_FALLBACK_SCORES.get(signals.udyam_category, 0)
    return 0


def _keyword_set(text: str | None) -> set[str]:
    if not text:
        return set()
    normalized = text.lower().replace(",", " ").replace("/", " ")
    return {word.strip() for word in normalized.split() if len(word.strip()) > 2}


def _industry_fit_score(
    seller_profile: SellerProfile,
    company: Company | None,
    signals: CompanySignals | None,
    card: VisitingCard,
) -> int:
    if company is None:
        return 0

    seller_keywords = _keyword_set(seller_profile.industry) | _keyword_set(
        seller_profile.product_lines
    )
    if not seller_keywords:
        return 0

    haystack = " ".join(
        part
        for part in (
            company.name,
            signals.product_lines_summary if signals else None,
            card.products_offered,
        )
        if part
    ).lower()
    if not haystack:
        return 0

    match_count = sum(1 for keyword in seller_keywords if keyword in haystack)
    for min_matches, score in _INDUSTRY_FIT_OVERLAP_SCORES:
        if match_count >= min_matches:
            return score
    return 0


def _momentum_signal_score(signals: CompanySignals | None) -> int:
    if signals is None:
        return 0
    score = 0
    if signals.hiring_signal == "expanding":
        score += _HIRING_SIGNAL_EXPANDING_POINTS
    if signals.gem_tender_count is not None and signals.gem_tender_count > 0:
        score += _GEM_TENDER_POINTS
    if signals.import_export_activity:
        score += _IMPORT_EXPORT_POINTS
    if signals.marketplace_verified_badge:
        score += _MARKETPLACE_VERIFIED_POINTS
    return score


def _remark_signal_score(special_remark: str | None) -> int:
    if not special_remark or not special_remark.strip():
        return 0
    normalized = special_remark.lower()
    if any(keyword in normalized for keyword in _POSITIVE_INTENT_KEYWORDS):
        return _REMARK_MATCH_SCORE
    return _REMARK_NO_MATCH_SCORE


def calculate_score(
    card: VisitingCard,
    company: Company | None,
    signals: CompanySignals | None,
    seller_profile: SellerProfile,
) -> dict:
    """Returns the score_breakdown JSONB shape: the 5 weighted components,
    their total (0-100), and a "version" tag so historical rows survive
    future scoring-logic changes. Pure — no DB reads/writes."""
    designation_score = _designation_score(card.designation_level)
    company_size_score = _company_size_score(signals)
    industry_fit_score = _industry_fit_score(seller_profile, company, signals, card)
    momentum_signal_score = _momentum_signal_score(signals)
    remark_signal_score = _remark_signal_score(card.special_remark)

    return {
        "designation_score": designation_score,
        "company_size_score": company_size_score,
        "industry_fit_score": industry_fit_score,
        "momentum_signal_score": momentum_signal_score,
        "remark_signal_score": remark_signal_score,
        "total": (
            designation_score
            + company_size_score
            + industry_fit_score
            + momentum_signal_score
            + remark_signal_score
        ),
        "version": _SCORE_VERSION,
    }
