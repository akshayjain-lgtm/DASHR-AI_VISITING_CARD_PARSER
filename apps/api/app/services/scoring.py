"""Pure, explainable lead-scoring logic — no DB/Celery imports here, only
reads off already-loaded ORM objects. Weights, bands, and keyword lists are
module-level data (same rationale as CLAUDE.md's scoring-weights rule and
enrichment_service.py's threshold constants), never inline in the caller.

calculate_score() is called by workers/scoring_processing.py, which is the
only place that persists its result.

Versioning: SCORING_VERSIONS is a permanent registry of every scoring
version ever shipped. Once any card has been scored under a given key, that
key's function body is frozen forever — a change to its logic is always a
new registry entry, never an in-place edit, since editing it in place would
silently change the meaning of every already-persisted score_breakdown row
carrying that version string. select_scoring_version() deterministically
assigns a fresh score to one of the currently-rolled-out versions; a rescore
never calls it again — the caller (scoring_processing.py) pins a rescore to
whatever version the card was originally scored under.
"""
import hashlib
import re
import uuid
from typing import Callable

import phonenumbers

from app.models.company import Company
from app.models.company_signals import CompanySignals
from app.models.seller_profile import SellerProfile
from app.models.visiting_card import VisitingCard

# =====================================================================
# v1 (frozen) — replaced by v2 as the default for new scores, but never
# edited in place: cards already scored under "v1" must keep scoring
# exactly this way forever.
# =====================================================================

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


def _calculate_score_v1(
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


# =====================================================================
# v2 — 8 categories, strictly priority-ordered by max-point budget
# (24/20/16/12/10/8/6/4 = 100). A category whose underlying signal is
# genuinely unavailable scores that category's average (max // 2), never
# 0 — 0 (or another explicit low tier) is reserved for a signal that was
# evaluated and came back negative/no-match/weak, with two deliberate
# exceptions (remark's blank case, role/designation's unknown case — see
# their helpers below). See .claude/specs/10-lead-scoring.md "v2 criteria"
# for the full spec this implements. v2 is still pre-launch product
# definition — its logic is revised directly in place here, not split into
# a new registry entry, since no real card has been scored under it yet
# (see spec Overview's "v2's definition below already includes a second
# round of refinements" amendment).
# =====================================================================

# --- remark_signal_score (max 24, priority 1) ---
_V2_POSITIVE_INTENT_KEYWORDS: tuple[str, ...] = (
    "interested",
    "important",
    "urgent",
    "budget",
    "decision",
    "follow up",
    "next week",
)
_V2_REMARK_TIER_1_SCORE = 24
_V2_REMARK_TIER_2_SCORE = 16
_V2_REMARK_TIER_3_SCORE = 8
_V2_REMARK_BLANK_SCORE = 0  # deliberate exception to the avg-fallback rule — see helper docstring
_PHONE_MATCHER_REGION = "IN"

# --- product_fit_score (max 20, priority 2) — AI-judged, not keyword-overlap ---
_PRODUCT_FIT_VERDICT_SCORES: dict[str, int] = {"needs": 20, "partial": 12, "no_need": 0}
_PRODUCT_FIT_AVG_SCORE = 10  # 20 // 2

# --- role_designation_score (max 16, priority 3) ---
_PURCHASE_FUNCTION_KEYWORDS: tuple[str, ...] = (
    "purchase",
    "procurement",
    "buying",
    "buyer",
    "commercial",
    "sourcing",
    "vendor development",
)
_ROLE_FUNCTION_MATCH_SCORE = 16
_V2_DESIGNATION_LEVEL_SCORES: dict[str, int] = {
    "c_level": 13,
    "director": 10,
    "manager": 6,
    "individual_contributor": 0,  # deliberate exception — see helper docstring
}
_ROLE_DESIGNATION_UNKNOWN_SCORE = 0  # deliberate exception to the avg-fallback rule

# --- proximity_score (max 12, priority 4) — real aerial distance, not a text bucket ---
_PROXIMITY_TIER_THRESHOLDS_KM: tuple[tuple[float, int], ...] = ((50, 12), (200, 9), (500, 5))
_PROXIMITY_BEYOND_SCORE = 1
_PROXIMITY_AVG_SCORE = 6  # 12 // 2

# --- expansion_signal_score (max 10, priority 5) / revenue_growth_score
# (max 8, priority 6) — both read CompanySignals.news_tags/
# news_distress_detected, Claude's own classification of the AI-summarized,
# identity-verified news pipeline (see
# enrichment_providers/news_summary_provider.py). Deliberately NOT a second,
# independent keyword scan of news_summary's plain text — that would
# re-derive (and risk drifting from) the same classification Claude already
# did more reliably, including resolving directionality a bare keyword
# match can't (e.g. "acquires" vs. "acquired by"). ---
_EXPANSION_TAG = "expansion"
_EXPANSION_MATCH_SCORE = 10
_EXPANSION_OTHER_NEWS_SCORE = 3
_EXPANSION_AVG_SCORE = 5  # 10 // 2

_REVENUE_GROWTH_TAG = "revenue_growth"
_REVENUE_GROWTH_MATCH_SCORE = 8
_REVENUE_GROWTH_OTHER_NEWS_SCORE = 2
_REVENUE_GROWTH_AVG_SCORE = 4  # 8 // 2

# --- company_size_score (max 6, priority 7) ---
_V2_EMPLOYEE_COUNT_BAND_THRESHOLDS: tuple[tuple[int, int], ...] = (
    (500, 4),
    (100, 3),
    (20, 2),
    (1, 1),
)
_V2_UDYAM_CATEGORY_BAND_SCORES: dict[str, int] = {"medium": 3, "small": 2, "micro": 1}
_V2_TURNOVER_CRORE_THRESHOLDS: tuple[tuple[float, int], ...] = (
    (100, 4),
    (25, 3),
    (5, 2),
    (0.01, 1),
)
_COMPANY_SIZE_IMPORT_EXPORT_BONUS = 1
_COMPANY_SIZE_GEM_TENDER_BONUS = 1
_COMPANY_SIZE_MAX = 6
_COMPANY_SIZE_AVG_SCORE = 3  # 6 // 2 — only when no CompanySignals row exists at all

# --- marketplace_rating_score (max 4, priority 8 — lowest) ---
_MARKETPLACE_RATING_SCALE_FACTOR = 4 / 5  # rescale 0-5 -> 0-4
_MARKETPLACE_RATING_AVG_SCORE = 2  # 4 // 2


def _normalize_phone_digits(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return digits[-10:] if len(digits) >= 10 else digits


def _remark_contains_new_phone_number(remark: str, existing_phones: list[str]) -> bool:
    existing = {_normalize_phone_digits(p) for p in existing_phones if p}
    for match in phonenumbers.PhoneNumberMatcher(remark, _PHONE_MATCHER_REGION):
        candidate = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
        if _normalize_phone_digits(candidate) not in existing:
            return True
    return False


def _remark_mentions_product_keyword(remark: str, seller_profile: SellerProfile) -> bool:
    """Checked only against the seller's OWN product_lines — not unioned
    with the buyer's/enrichment's product text. Matching the buyer's own
    text there measured "the remark mentions something like what the buyer
    already sells," not "the remark mentions the seller's product"."""
    reference = _keyword_set(seller_profile.product_lines)
    if not reference:
        return False
    return bool(reference & _keyword_set(remark))


def _remark_signal_score_v2(
    card: VisitingCard, seller_profile: SellerProfile, existing_phones: list[str]
) -> int:
    """A blank remark scores 0, not the category average — a sales rep
    only writes a handwritten note when a lead seems worth flagging, so
    "no note" is itself mild negative evidence, not missing data. This is
    a deliberate exception to the general avg-fallback rule."""
    remark = card.special_remark
    if not remark or not remark.strip():
        return _V2_REMARK_BLANK_SCORE
    normalized = remark.lower()
    if any(keyword in normalized for keyword in _V2_POSITIVE_INTENT_KEYWORDS) or (
        _remark_mentions_product_keyword(remark, seller_profile)
    ):
        return _V2_REMARK_TIER_1_SCORE
    if _remark_contains_new_phone_number(remark, existing_phones):
        return _V2_REMARK_TIER_2_SCORE
    return _V2_REMARK_TIER_3_SCORE


def _product_fit_score(product_fit_verdict: str | None) -> int:
    """product_fit_verdict is an AI judgment of whether the buyer's
    industry/business-type would use the seller's product as an
    operational input, resolved by scoring_processing.py via
    product_fit_service.get_or_judge_fit before calculate_score is called
    — this function itself does no I/O, just a dict lookup."""
    if product_fit_verdict is None:
        return _PRODUCT_FIT_AVG_SCORE
    return _PRODUCT_FIT_VERDICT_SCORES.get(product_fit_verdict, _PRODUCT_FIT_AVG_SCORE)


def _role_designation_score(card: VisitingCard) -> int:
    """individual_contributor and "no designation known at all" both score
    0, not 3/the average — no meaningful purchasing influence, not just a
    weak signal. Deliberate exception to the general avg-fallback rule."""
    job_title = (card.job_title or "").lower()
    if any(keyword in job_title for keyword in _PURCHASE_FUNCTION_KEYWORDS):
        return _ROLE_FUNCTION_MATCH_SCORE
    if card.designation_level:
        return _V2_DESIGNATION_LEVEL_SCORES.get(
            card.designation_level, _ROLE_DESIGNATION_UNKNOWN_SCORE
        )
    return _ROLE_DESIGNATION_UNKNOWN_SCORE


def _proximity_score(distance_km: float | None) -> int:
    """distance_km is the real aerial (haversine) distance between the
    buyer's and seller's geocoded addresses, resolved by
    scoring_processing.py via geocode_service before calculate_score is
    called — this function itself does no I/O."""
    if distance_km is None:
        return _PROXIMITY_AVG_SCORE
    for max_km, score in _PROXIMITY_TIER_THRESHOLDS_KM:
        if distance_km <= max_km:
            return score
    return _PROXIMITY_BEYOND_SCORE


def _expansion_signal_score(signals: CompanySignals | None) -> int:
    """Reads CompanySignals.news_tags/news_distress_detected, Claude's own
    classification from the AI-summarized, identity-verified news pipeline
    (see enrichment_providers/news_summary_provider.py) — never re-derives
    the classification itself. The distress override is checked first and
    is scoped to exactly this category and revenue_growth_score below —
    it never affects any other category."""
    if signals is not None and signals.news_distress_detected:
        return 0
    if signals is None or not signals.news_summary:
        return _EXPANSION_AVG_SCORE
    if _EXPANSION_TAG in (signals.news_tags or []):
        return _EXPANSION_MATCH_SCORE
    return _EXPANSION_OTHER_NEWS_SCORE


def _revenue_growth_score(signals: CompanySignals | None) -> int:
    """Same news_tags/news_distress_detected source and shared distress
    override as _expansion_signal_score above."""
    if signals is not None and signals.news_distress_detected:
        return 0
    if signals is None or not signals.news_summary:
        return _REVENUE_GROWTH_AVG_SCORE
    if _REVENUE_GROWTH_TAG in (signals.news_tags or []):
        return _REVENUE_GROWTH_MATCH_SCORE
    return _REVENUE_GROWTH_OTHER_NEWS_SCORE


def _tier_score(value: float, thresholds: tuple[tuple[float, int], ...]) -> int:
    for min_value, score in thresholds:
        if value >= min_value:
            return score
    return 0


def _parse_max_number(band: str) -> int | None:
    numbers = [int(n) for n in re.findall(r"\d+", band)]
    return max(numbers) if numbers else None


def _parse_amount_in_crores(band: str) -> float | None:
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*(crore|cr|lakh|lac)", band.lower())
    if not matches:
        return None
    values = [float(n) if unit in ("crore", "cr") else float(n) / 100 for n, unit in matches]
    return max(values)


def _employee_band_score_v2(signals: CompanySignals) -> int | None:
    if signals.indiamart_employee_count_band:
        count = _parse_max_number(signals.indiamart_employee_count_band)
        if count is not None:
            return _tier_score(count, _V2_EMPLOYEE_COUNT_BAND_THRESHOLDS)
    if signals.linkedin_employee_count is not None:
        return _tier_score(signals.linkedin_employee_count, _V2_EMPLOYEE_COUNT_BAND_THRESHOLDS)
    if signals.udyam_category:
        return _V2_UDYAM_CATEGORY_BAND_SCORES.get(signals.udyam_category)
    return None


def _turnover_band_score_v2(signals: CompanySignals) -> int | None:
    band = signals.indiamart_annual_turnover_band or signals.estimated_revenue_band
    if not band:
        return None
    crores = _parse_amount_in_crores(band)
    return _tier_score(crores, _V2_TURNOVER_CRORE_THRESHOLDS) if crores is not None else None


def _company_size_score_v2(signals: CompanySignals | None) -> int:
    if signals is None:
        return _COMPANY_SIZE_AVG_SCORE
    employee_score = _employee_band_score_v2(signals)
    turnover_score = _turnover_band_score_v2(signals)
    candidates = [score for score in (employee_score, turnover_score) if score is not None]
    base = max(candidates) if candidates else 0
    bonus = 0
    if signals.import_export_activity:
        bonus += _COMPANY_SIZE_IMPORT_EXPORT_BONUS
    if signals.gem_tender_count is not None and signals.gem_tender_count > 0:
        bonus += _COMPANY_SIZE_GEM_TENDER_BONUS
    return min(base + bonus, _COMPANY_SIZE_MAX)


def _marketplace_rating_score(signals: CompanySignals | None) -> int:
    if signals is None or signals.indiamart_rating is None:
        return _MARKETPLACE_RATING_AVG_SCORE
    return round(float(signals.indiamart_rating) * _MARKETPLACE_RATING_SCALE_FACTOR)


def _calculate_score_v2(
    card: VisitingCard,
    company: Company | None,
    signals: CompanySignals | None,
    seller_profile: SellerProfile,
    existing_phones: list[str],
    product_fit_verdict: str | None,
    distance_km: float | None,
) -> dict:
    """Returns the v2 score_breakdown JSONB shape: 8 priority-ordered
    components, their total (0-100), and version: "v2". Pure — no DB
    reads/writes; product_fit_verdict/distance_km are already resolved by
    scoring_processing.py before this is called. Every branch, including
    every avg fallback and the two explicit-zero exceptions (remark,
    role/designation), stays within its category's max, so total sums to
    0-100 by construction. `company` is accepted (for the dispatcher's
    uniform calling convention) but not read internally — its Company-
    derived inputs (hq_city fallback, industry) are pre-resolved by the
    worker into product_fit_verdict/distance_km."""
    remark_signal_score = _remark_signal_score_v2(card, seller_profile, existing_phones)
    product_fit_score = _product_fit_score(product_fit_verdict)
    role_designation_score = _role_designation_score(card)
    proximity_score = _proximity_score(distance_km)
    expansion_signal_score = _expansion_signal_score(signals)
    revenue_growth_score = _revenue_growth_score(signals)
    company_size_score = _company_size_score_v2(signals)
    marketplace_rating_score = _marketplace_rating_score(signals)

    return {
        "remark_signal_score": remark_signal_score,
        "product_fit_score": product_fit_score,
        "role_designation_score": role_designation_score,
        "proximity_score": proximity_score,
        "expansion_signal_score": expansion_signal_score,
        "revenue_growth_score": revenue_growth_score,
        "company_size_score": company_size_score,
        "marketplace_rating_score": marketplace_rating_score,
        "total": (
            remark_signal_score
            + product_fit_score
            + role_designation_score
            + proximity_score
            + expansion_signal_score
            + revenue_growth_score
            + company_size_score
            + marketplace_rating_score
        ),
        "version": "v2",
    }


# =====================================================================
# Scoring version registry & A/B experimentation
# =====================================================================


def _calculate_score_v1_adapter(
    card: VisitingCard,
    company: Company | None,
    signals: CompanySignals | None,
    seller_profile: SellerProfile,
    existing_phones: list[str],
    product_fit_verdict: str | None,
    distance_km: float | None,
) -> dict:
    """v1's frozen body takes 4 args and knows nothing about product-fit
    verdicts or geocoded distances; this adapter accepts-and-ignores the
    two new params so every registry entry exposes one uniform
    7-positional-arg calling convention to the dispatcher — it is
    infrastructure, not scoring logic, and is never itself "frozen" the
    way a real registry entry is."""
    return _calculate_score_v1(card, company, signals, seller_profile)


# Every version ever shipped, keyed by the exact string written into
# score_breakdown["version"]. Once any card has been scored under a key,
# that key's function body is frozen forever — a fix or tweak is always a
# new key, never an edit to an existing one.
SCORING_VERSIONS: dict[str, Callable[..., dict]] = {
    "v1": _calculate_score_v1_adapter,
    "v2": _calculate_score_v2,
}

# Percentages of *fresh* scores assigned to each version, must sum to 100
# across every currently-assignable key. A version can remain in
# SCORING_VERSIONS (for historical cards) after being dropped from this
# dict once retired from new assignments.
_SCORING_VERSION_ROLLOUT: dict[str, int] = {
    "v1": 0,
    "v2": 100,
}


def select_scoring_version(user_id: uuid.UUID) -> str:
    """Deterministic, stable-hash bucketing: the same user_id always maps
    to the same version for as long as _SCORING_VERSION_ROLLOUT is
    unchanged, so a seller isn't flipped between versions from one card to
    the next mid-experiment. Pure function, no DB access."""
    bucket = int(hashlib.sha256(str(user_id).encode()).hexdigest(), 16) % 100
    cumulative = 0
    for version, percentage in _SCORING_VERSION_ROLLOUT.items():
        cumulative += percentage
        if bucket < cumulative:
            return version
    # Unreachable if _SCORING_VERSION_ROLLOUT sums to 100 as required.
    return next(iter(_SCORING_VERSION_ROLLOUT))


def calculate_score(
    card: VisitingCard,
    company: Company | None,
    signals: CompanySignals | None,
    seller_profile: SellerProfile,
    existing_phones: list[str],
    product_fit_verdict: str | None,
    distance_km: float | None,
    *,
    version: str | None = None,
) -> dict:
    """Dispatches to whichever SCORING_VERSIONS entry applies. Pass
    `version` explicitly to pin a rescore to the card's original version —
    leave it None only for a fresh, first-time score, where it's resolved
    via select_scoring_version(). product_fit_verdict/distance_km are
    resolved by the caller (scoring_processing.py) before this call and
    simply ignored by any registry entry (like v1) that doesn't need them.
    The dispatcher has no knowledge of any version's internal
    signature/shape beyond the uniform 7-positional-arg calling convention
    every registry entry implements."""
    resolved_version = version if version is not None else select_scoring_version(card.user_id)
    return SCORING_VERSIONS[resolved_version](
        card, company, signals, seller_profile, existing_phones,
        product_fit_verdict, distance_km,
    )
