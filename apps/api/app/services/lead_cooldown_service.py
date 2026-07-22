"""Small, dependency-free helper over VisitingCard.company_enriched_at,
shared by card_service.py and workers/scoring_processing.py — deliberately a
standalone leaf module (mirroring field_correction_service.py's exact
rationale) rather than living inside card_service.py: card_service.py
already imports score_card_task from workers/scoring_processing.py, so a
scoring_processing -> card_service import would be circular. This module
depends on nothing but VisitingCard, so both call sites can import it
safely.

workers/enrichment_processing.py writes the same VisitingCard.
company_enriched_at column directly via a bulk SQLAlchemy `update()` (it
never needs `cooldown_elapsed`'s comparison, only a plain timestamp write
across every sibling card sharing a company), so it does not import this
module — only card_service.py and scoring_processing.py call
cooldown_elapsed() itself.

This is a per-lead billed cooldown, distinct from and independent of
CompanySignals.factual_fetched_at/dynamic_fetched_at (the shared,
cross-org cache freshness clocks enrichment_service owns) — see
.claude/specs/24-company-linkage-tiered-expiry.md. It exists to rate-limit
repeat spend on one lead, not to protect the shared cache, so eligibility
here never by itself decides whether a real re-fetch happens.
"""
from datetime import datetime, timedelta, timezone

from app.models.visiting_card import VisitingCard

_LEAD_LEVEL_ACTION_COOLDOWN_DAYS = 30


def cooldown_elapsed(card: VisitingCard) -> bool:
    """True once at least _LEAD_LEVEL_ACTION_COOLDOWN_DAYS have passed since
    this specific card's own company_enriched_at. False when the card has no
    company_enriched_at yet (never linked to a settled company, or linked
    while its company was still mid-enrichment) — there's no anchor to
    measure a cooldown from."""
    if card.company_enriched_at is None:
        return False
    return datetime.now(timezone.utc) - card.company_enriched_at >= timedelta(
        days=_LEAD_LEVEL_ACTION_COOLDOWN_DAYS
    )
