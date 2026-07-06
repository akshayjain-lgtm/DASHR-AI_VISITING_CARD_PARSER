import logging
import uuid

from app.db.session import SessionLocal
from app.models.visiting_card import VisitingCard
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.card_processing.process_card")
def process_card(card_id: str) -> None:
    """Placeholder — loads the card and logs. Real vision-LLM extraction
    lands in 05-card-extraction; this task exists now so bulk-upload can
    enqueue async work without blocking the request, per CLAUDE.md."""
    db = SessionLocal()
    try:
        card = db.get(VisitingCard, uuid.UUID(card_id))
        if card is None:
            logger.warning("process_card: card_id %s not found", card_id)
            return
        logger.info("process_card placeholder invoked for card_id=%s", card_id)
    finally:
        db.close()
