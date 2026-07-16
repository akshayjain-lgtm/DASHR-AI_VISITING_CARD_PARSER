import logging
import uuid
from datetime import datetime, timezone

from celery.exceptions import MaxRetriesExceededError

from app.db.session import SessionLocal
from app.models.visiting_card import VisitingCard
from app.services import billing, extraction_service
from app.services.exceptions import ExtractionValidationError, VisionApiError
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

_MAX_VISION_RETRIES = 3


@celery_app.task(
    name="app.workers.card_processing.process_card",
    bind=True,
    max_retries=_MAX_VISION_RETRIES,
)
def process_card(self, card_id: str, billed: bool = False) -> None:
    """Runs extraction for one card and maps the outcome to status/timestamps.

    `billed` is whatever the enqueuing card_service call determined this
    card's charge to be (True if paid from the wallet, False if free) —
    passed through so a permanent failure here can call
    billing.refund_action for the same card/action_type, reversing that
    charge since the work was attempted but never completed. Never used to
    charge here — the charge already happened synchronously before this
    task was enqueued (CLAUDE.md: check/charge ahead of the OCR call, not
    after). Defaults to False only for direct/manual task invocations
    outside the normal enqueue path (e.g. this repo's own tests bypassing
    .delay()), where there is no charge to reverse.

    Idempotency note: `self.request.retries` distinguishes a fresh Celery
    delivery (retries == 0, where the `status != 'new'` guard applies) from
    one of our own retry redeliveries (retries > 0). Without that
    distinction, a literal "if status != 'new': return" guard would make
    every retry after the first attempt a silent no-op — the first attempt
    already flips status to 'processing' and commits before calling
    extract_card, so every retry would see status='processing' (not 'new')
    and bail out, stranding the card in 'processing' forever instead of
    eventually reaching 'failed'.

    Retry mechanism note: retries are driven by a manual `self.retry()` call
    inside `except VisionApiError` rather than the declarative
    `autoretry_for=(VisionApiError,)` option, because Celery's autoretry
    wrapper re-raises past this function once retries are exhausted with no
    hook to intercept that — we'd have no way to run our own
    status='failed' finalization. Calling `self.retry()` ourselves and
    catching `MaxRetriesExceededError` keeps that finalization in our
    control while still retrying ~3x with exponential backoff.
    """
    db = SessionLocal()
    try:
        card = db.get(VisitingCard, uuid.UUID(card_id))
        if card is None:
            logger.warning("process_card: card_id %s not found", card_id)
            return

        is_retry = self.request.retries > 0
        if not is_retry:
            if card.status != "new":
                logger.info(
                    "process_card: card_id %s already status=%s, skipping", card_id, card.status
                )
                return
            card.status = "processing"
            db.commit()
        elif card.status != "processing":
            # Something else moved this card past 'processing' between our
            # own retry attempts (e.g. a concurrent reprocess) — abandon
            # this stale retry rather than clobber it.
            logger.info(
                "process_card retry: card_id %s status=%s, skipping", card_id, card.status
            )
            return

        try:
            outcome = extraction_service.extract_card(db, card)
        except VisionApiError as exc:
            countdown = 2**self.request.retries
            try:
                # Deliberately NOT passing exc= here: Celery's retry() only
                # raises MaxRetriesExceededError when exc is None — if exc is
                # provided, it re-raises that exact exception once retries
                # are exhausted instead, which would bypass this except
                # clause entirely and crash the task unhandled.
                self.retry(countdown=countdown, max_retries=_MAX_VISION_RETRIES)
            except MaxRetriesExceededError:
                logger.error(
                    "process_card: vision API exhausted retries for card_id=%s: %s",
                    card_id, exc,
                )
                db.rollback()
                card.status = "failed"
                card.extraction_error = "Vision extraction failed after multiple attempts. You can retry."
                card.processed_at = datetime.now(timezone.utc)
                db.commit()
                # Permanently failed, not just transiently retrying — the
                # user paid for a parse that never completed, so reverse it.
                # A later reprocess is a brand-new, separately-charged action.
                billing.refund_action(db, card.user_id, "parse", billed=billed, reference_id=card.card_id)
            return
        except ExtractionValidationError as exc:
            db.rollback()
            card.status = "failed"
            card.extraction_error = str(exc)
            card.processed_at = datetime.now(timezone.utc)
            db.commit()
            billing.refund_action(db, card.user_id, "parse", billed=billed, reference_id=card.card_id)
            return
        except Exception:
            # Anything else (a DB error mid-merge, a malformed vision
            # response shape, a Pillow decode failure, ...) must still
            # finalize the card to 'failed' — otherwise it stays stuck in
            # 'processing' forever with no recorded error and no way back
            # via POST /cards/{id}/reprocess, which only accepts 'failed'.
            logger.exception(
                "process_card: unexpected error extracting card_id=%s", card_id
            )
            db.rollback()
            card.status = "failed"
            card.extraction_error = "Unexpected error during extraction."
            card.processed_at = datetime.now(timezone.utc)
            db.commit()
            billing.refund_action(db, card.user_id, "parse", billed=billed, reference_id=card.card_id)
            return

        card.status = outcome
        card.processed_at = datetime.now(timezone.utc)
        db.commit()
        # Enrichment is a separate, explicit action — POST
        # /cards/{card_id}/enrich-company — never auto-triggered here.
        # Mirrors this codebase's existing convention that every pipeline
        # stage (upload -> parse -> enrich) is a distinct user-initiated
        # action, not an invisible side effect of the previous one.
    finally:
        db.close()
