import logging
import uuid
from datetime import datetime, timezone

from celery.exceptions import MaxRetriesExceededError
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.card_phone import CardPhone
from app.models.company import Company
from app.models.company_signals import CompanySignals
from app.models.seller_profile import SellerProfile
from app.models.user import User
from app.models.visiting_card import VisitingCard
from app.services import (
    billing,
    field_correction_service,
    geocode_service,
    lead_cooldown_service,
    product_fit_service,
    profile_service,
    scoring,
)
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

_MAX_SCORING_RETRIES = 3

# Cost-avoidance: only versions that actually read product_fit_verdict/
# distance_km trigger the Claude/geocoding I/O below — v1 ignores both
# params anyway (see scoring.py's _calculate_score_v1_adapter), so doing
# the real network/Claude work only to have it discarded would be wasted.
_VERSIONS_NEEDING_PRODUCT_FIT_AND_GEOCODING = frozenset({"v2"})


def _build_product_signature(seller_profile: SellerProfile) -> str | None:
    parts = [p for p in (seller_profile.product_lines, seller_profile.industry) if p and p.strip()]
    return " ".join(parts) if parts else None


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
    already scored unless a correction postdates its last score or its
    lead-cooldown has elapsed — see lead_cooldown_service.py,
    .claude/specs/24-company-linkage-tiered-expiry.md) are re-run
    identically on every attempt, fresh or retried; if the card's status
    moved out of "extracted" underneath a stuck/retried task, or it was
    already scored (with neither rescore reason true) by a
    duplicate/concurrent enqueue, the task just logs and returns rather than
    clobbering a card that's no longer eligible. This is the same rule
    enforced in card_service.score_card_now/enqueue_scoring, re-checked here
    too since two enqueues racing past the service-layer check could
    otherwise both reach this task.

    `is_rescore` (whether lead_score was already set when this run started)
    is derived fresh from the card here, not passed in, and gates scoring-
    version resolution: a rescore is always pinned to the version already
    stored on the card's score_breakdown (see
    .claude/specs/10-lead-scoring.md "Scoring versioning & A/B
    experimentation") rather than re-rolling the experiment assignment, so
    a correction/cooldown-triggered rescore can change a card's score but
    never its scoring version — this applies to both rescore reasons alike.

    `free_rescore` (a strict subset of `is_rescore`: true only when the
    correction-triggered reason applied) is the actual signal for whether
    `billed`/refund-on-failure applies at all: a free rescore (see
    .claude/specs/20-field-correction.md) never went through
    billing.charge_for_action, so refund_action must never be called for it
    on retry exhaustion — that would incorrectly decrement the user's
    free-allowance count for an action they never actually used it on. A
    cooldown-triggered rescore (is_rescore True, free_rescore False) *was*
    billed via charge_for_action in card_service, exactly like a first-ever
    score, so it must be refunded on a permanent failure the same way.

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
        # free_rescore mirrors card_service.score_card_now's own priority
        # rule: the correction-triggered reason is checked first and, when
        # true, is what made this a free (never billed) rescore. A
        # cooldown-triggered rescore is billed exactly like a first-ever
        # score — see the refund branch below, which must only skip
        # refunding the free case.
        free_rescore = is_rescore and field_correction_service.has_correction_since_score(db, card)
        if is_rescore and not free_rescore and not lead_cooldown_service.cooldown_elapsed(card):
            logger.info(
                "score_card_task: card_id %s already scored, no correction since and "
                "lead-cooldown not elapsed, skipping (one-shot rule)",
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

            phones = db.execute(
                select(CardPhone).where(CardPhone.card_id == card.card_id)
            ).scalars().all()
            existing_phones = [p.phone_e164 for p in phones if p.phone_e164] + [
                p.phone_raw for p in phones if p.phone_raw
            ]

            if is_rescore:
                # Never re-roll the experiment assignment on a rescore — a
                # correction can change a card's score but never its version.
                version = card.score_breakdown["version"]
            else:
                version = scoring.select_scoring_version(card.user_id)

            product_fit_verdict: str | None = None
            distance_km: float | None = None
            if version in _VERSIONS_NEEDING_PRODUCT_FIT_AND_GEOCODING:
                product_signature = _build_product_signature(seller_profile)
                if company is not None and product_signature:
                    product_fit_verdict = product_fit_service.get_or_judge_fit(
                        db, product_signature, company.industry,
                        signals.indiamart_business_type if signals else None,
                    )

                buyer_address = card.address or (company.hq_city if company else None)
                buyer_point = geocode_service.get_or_geocode(db, buyer_address)
                seller_point = geocode_service.get_or_geocode(db, seller_profile.billing_address)
                if buyer_point is not None and seller_point is not None:
                    distance_km = geocode_service.haversine_km(buyer_point, seller_point)

            breakdown = scoring.calculate_score(
                card, company, signals, seller_profile, existing_phones,
                product_fit_verdict, distance_km, version=version,
            )
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
                if not free_rescore:
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
