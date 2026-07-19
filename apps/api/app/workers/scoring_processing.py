import logging
import uuid
from datetime import datetime, timezone

from celery.exceptions import MaxRetriesExceededError

from app.db.session import SessionLocal
from app.models.company import Company
from app.models.company_signals import CompanySignals
from app.models.user import User
from app.models.visiting_card import VisitingCard
from app.services import billing, field_correction_service, profile_service, scoring
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

_MAX_SCORING_RETRIES = 3


@celery_app.task(
    name="app.workers.scoring_processing.score_card_task",
    bind=True,
    max_retries=_MAX_SCORING_RETRIES,
)
def score_card_task(self, card_id: str, billed: bool = False) -> None:
    """Computes and persists lead_score/score_breakdown/scored_at for one
    card. Never changes card.status — scored_at being non-null is the only
    signal that a card has been scored, exactly as Company.enrichment_status
    (not VisitingCard.status) is the signal for enrichment completion.

    `billed` is whatever the enqueuing card_service call determined this
    charge to be (True if paid, False if free) — see process_card's
    docstring for the full rationale; refunded via billing.refund_action if
    retries are exhausted below. Unlike process_card/enrich_company_task,
    there is no persistent "failed" status for scoring (see the idempotency
    note below) — the refund is the only durable trace a permanent scoring
    failure leaves in the data model today.

    Idempotency note: unlike process_card/enrich_company_task, scoring does
    no external I/O and so has no in-flight status to transition through —
    there is no fresh-delivery-vs-retry branch on self.request.retries.
    Instead the eligibility checks (card.status == "extracted", card not
    already scored unless a correction postdates its last score) are re-run
    identically on every attempt, fresh or retried; if the card's status
    moved out of "extracted" underneath a stuck/retried task, or it was
    already scored (with no correction since) by a duplicate/concurrent
    enqueue, the task just logs and returns rather than clobbering a card
    that's no longer eligible. This is the same rule enforced in
    card_service.score_card_now/enqueue_scoring, re-checked here too since
    two enqueues racing past the service-layer check could otherwise both
    reach this task.

    `is_rescore` (whether lead_score was already set when this run started)
    is derived fresh from the card here, not passed in — it doubles as the
    signal for whether `billed`/refund-on-failure applies at all: a free
    rescore (see .claude/specs/20-field-correction.md) never went through
    billing.charge_for_action, so refund_action must never be called for
    it on retry exhaustion — that would incorrectly decrement the user's
    free-allowance count for an action they never actually used it on.

    Seller calibration is loaded from the card's own owner (card.user_id),
    not whoever triggered this task — matters when an org admin scores a
    member's card via scope_to_visible_users, since the score must reflect
    that member's target-customer profile, not the admin's own.
    """
    db = SessionLocal()
    try:
        card = db.get(VisitingCard, uuid.UUID(card_id))
        if card is None:
            logger.warning("score_card_task: card_id %s not found", card_id)
            return
        if card.status != "extracted":
            logger.info(
                "score_card_task: card_id %s status=%s, not eligible, skipping",
                card_id, card.status,
            )
            return
        is_rescore = card.lead_score is not None
        if is_rescore and not field_correction_service.has_correction_since_score(db, card):
            logger.info(
                "score_card_task: card_id %s already scored, no correction since, skipping "
                "(one-shot rule)",
                card_id,
            )
            return

        try:
            company = db.get(Company, card.company_id) if card.company_id else None
            signals = (
                db.get(CompanySignals, company.company_id) if company else None
            )
            owner = db.get(User, card.user_id)
            seller_profile = profile_service.get_or_empty_profile(db, owner)
            breakdown = scoring.calculate_score(card, company, signals, seller_profile)
        except Exception as exc:
            countdown = 2**self.request.retries
            try:
                # Deliberately NOT passing exc= here: Celery's retry() only
                # raises MaxRetriesExceededError when exc is None — if exc is
                # provided, it re-raises that exact exception once retries
                # are exhausted instead, which would bypass this except
                # clause entirely and crash the task unhandled.
                self.retry(countdown=countdown, max_retries=_MAX_SCORING_RETRIES)
            except MaxRetriesExceededError:
                logger.error(
                    "score_card_task: exhausted retries for card_id=%s: %s", card_id, exc
                )
                if not is_rescore:
                    billing.refund_action(
                        db, card.user_id, "scoring", billed=billed, reference_id=card.card_id
                    )
            return

        card.lead_score = breakdown["total"]
        card.score_breakdown = breakdown
        card.scored_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
