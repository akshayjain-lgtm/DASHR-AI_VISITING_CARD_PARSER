"""Small, dependency-free query helper over `FieldCorrection` shared by
`card_service.py` (the correction endpoint + rescore eligibility check) and
`workers/scoring_processing.py` (score_card_task's own independent
re-check). Deliberately a standalone leaf module rather than living inside
`card_service.py`: `card_service.py` already imports `score_card_task` from
`workers/scoring_processing.py`, so a `scoring_processing -> card_service`
import would be circular. This module depends on nothing but the
`FieldCorrection` model, so both call sites can import it safely.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.field_correction import FieldCorrection
from app.models.visiting_card import VisitingCard


def has_correction_since_score(db: Session, card: VisitingCard) -> bool:
    """True when at least one FieldCorrection was written for this card after
    its last scoring run — the signal that a free rescore should be offered/
    allowed, since the AI's original score may no longer reflect corrected
    data. False when the card has never been scored (scored_at is None) or
    nothing has changed since."""
    if card.scored_at is None:
        return False
    return (
        db.scalar(
            select(FieldCorrection.correction_id)
            .where(
                FieldCorrection.card_id == card.card_id,
                FieldCorrection.created_at > card.scored_at,
            )
            .limit(1)
        )
        is not None
    )
