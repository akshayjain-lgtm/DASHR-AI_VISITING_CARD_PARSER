import logging
import uuid
from datetime import datetime, timezone

from celery.exceptions import MaxRetriesExceededError

from app.db.session import SessionLocal
from app.models.company import Company
from app.models.visiting_card import VisitingCard
from app.services import billing, enrichment_service, enrichment_summary
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

_MAX_ENRICHMENT_RETRIES = 3


@celery_app.task(
    name="app.workers.enrichment_processing.enrich_company_task",
    bind=True,
    max_retries=_MAX_ENRICHMENT_RETRIES,
)
def enrich_company_task(
    self, company_id: str, source_card_id: str | None = None, billed: bool = False
) -> None:
    """Runs the full public-source fan-out for one `Company` and maps the
    outcome onto `enrichment_status`/`summary`.

    `source_card_id` is the id of whichever card triggered this run — never
    the card's raw GSTIN. The GSTIN is re-loaded from that card here,
    inside the worker process, so a sensitive tax identifier never crosses
    the Celery broker or shows up in default task-received log lines the
    way a plain string argument would. It's also the only way this task
    knows which user to refund on a permanent failure below — Company has
    no user_id of its own (enrichment data is shared across orgs).

    `billed` is whatever the enqueuing card_service call determined this
    charge to be (True if paid, False if free) — see process_card's
    docstring for the full rationale; same refund-on-permanent-failure
    pattern here via billing.refund_action.

    Idempotency note: mirrors `process_card`'s `self.request.retries`
    fresh-delivery-vs-own-retry distinction exactly, swapping
    card.status/"new"/"processing" for company.enrichment_status/"pending"/
    "enriching".

    Unlike `process_card`, there is only one except clause here (not a
    permanent-vs-transient split) because every per-source failure is
    already caught and swallowed inside `enrichment_service.
    run_all_signal_lookups` — nothing that reaches this task's own
    try/except is a "this source had no data" case, only a genuine
    infra-level failure (e.g. a DB error mid-upsert).
    """
    db = SessionLocal()
    try:
        company = db.get(Company, uuid.UUID(company_id))
        if company is None:
            logger.warning("enrich_company_task: company_id %s not found", company_id)
            return

        gst_number = None
        refund_user_id = None
        if source_card_id is not None:
            source_card = db.get(VisitingCard, uuid.UUID(source_card_id))
            if source_card is not None:
                gst_number = source_card.gst_number
                refund_user_id = source_card.user_id

        is_retry = self.request.retries > 0
        if not is_retry:
            if company.enrichment_status != "pending":
                logger.info(
                    "enrich_company_task: company_id %s already status=%s, skipping",
                    company_id, company.enrichment_status,
                )
                return
            company.enrichment_status = "enriching"
            db.commit()
        elif company.enrichment_status != "enriching":
            logger.info(
                "enrich_company_task retry: company_id %s status=%s, skipping",
                company_id, company.enrichment_status,
            )
            return

        try:
            signals, any_signal_found = enrichment_service.run_all_signal_lookups(
                db, company, gst_number
            )
            summary = enrichment_summary.generate_summary(company, signals)
        except Exception as exc:
            countdown = 2**self.request.retries
            try:
                # Deliberately NOT passing exc= here: Celery's retry() only
                # raises MaxRetriesExceededError when exc is None — if exc is
                # provided, it re-raises that exact exception once retries
                # are exhausted instead, which would bypass this except
                # clause entirely and crash the task unhandled.
                self.retry(countdown=countdown, max_retries=_MAX_ENRICHMENT_RETRIES)
            except MaxRetriesExceededError:
                logger.error(
                    "enrich_company_task: exhausted retries for company_id=%s: %s",
                    company_id, exc,
                )
                db.rollback()
                company.enrichment_status = "failed"
                db.commit()
                if refund_user_id is not None:
                    billing.refund_action(
                        db,
                        refund_user_id,
                        "enrichment",
                        billed=billed,
                        reference_id=uuid.UUID(source_card_id) if source_card_id else None,
                    )
                else:
                    logger.warning(
                        "enrich_company_task: no source_card_id for company_id=%s, "
                        "cannot refund a permanently failed enrichment charge",
                        company_id,
                    )
            return

        company.summary = summary
        company.summary_generated_at = datetime.now(timezone.utc)
        company.enrichment_status = "enriched" if any_signal_found else "not_found"
        company.enriched_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
