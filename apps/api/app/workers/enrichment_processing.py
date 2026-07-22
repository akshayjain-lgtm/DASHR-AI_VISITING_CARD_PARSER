import logging
import uuid
from datetime import datetime, timezone

from celery.exceptions import MaxRetriesExceededError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.card_email import CardEmail
from app.models.company import Company
from app.models.company_signals import CompanySignals
from app.models.visiting_card import VisitingCard
from app.services import billing, enrichment_service, enrichment_summary
from app.services.industry_classification import classify_industry, fetch_website_text
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

_MAX_ENRICHMENT_RETRIES = 3

# Personal/free email providers excluded from the email-domain IndiaMART
# fallback search — "gmail.com IndiaMart" would never correspond to any one
# company's storefront, so a domain from one of these is never worth a
# second Apify call.
_GENERIC_EMAIL_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com",
    "rediffmail.com", "icloud.com", "live.com", "aol.com", "protonmail.com",
})


def _card_email_domain(db: Session, card_id: uuid.UUID) -> str | None:
    """Best-effort domain off this card's primary (or first available)
    email, for the IndiaMART catalog_url lookup's fallback search — never
    raises, and returns None for a personal/free-provider domain since that
    would never identify a specific company's storefront."""
    email = db.scalar(
        select(CardEmail.email)
        .where(CardEmail.card_id == card_id, CardEmail.email.isnot(None))
        .order_by(CardEmail.is_primary.desc())
        .limit(1)
    )
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower()
    return domain if domain and domain not in _GENERIC_EMAIL_DOMAINS else None


@celery_app.task(
    name="app.workers.enrichment_processing.enrich_company_task",
    bind=True,
    max_retries=_MAX_ENRICHMENT_RETRIES,
)
def enrich_company_task(
    self,
    company_id: str,
    source_card_id: str | None = None,
    billed: bool = False,
    refresh_tiers: list[str] | None = None,
) -> None:
    """Runs the public-source fan-out for one `Company` and maps the outcome
    onto `enrichment_status`/`summary`.

    `refresh_tiers` distinguishes a first-ever run from a refresh of an
    already-`"enriched"` company (see
    .claude/specs/24-company-linkage-tiered-expiry.md): `None` (the default)
    is a first-ever run — unchanged pending/enriching status-machine below.
    A list (possibly empty — a lead-cooldown-only trigger with a fully fresh
    cache) means a refresh: the company never transitions through
    `"enriching"` and its `enrichment_status`/`enriched_at`/`industry` are
    left untouched, only `CompanySignals`/`summary` are updated. Before
    calling `run_all_signal_lookups`, the caller-provided tiers are
    intersected with a freshly-read `enrichment_service.stale_tiers` so two
    concurrent refreshes can't both re-hit the same already-being-refreshed
    tier's third-party providers.

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
    "enriching" — but only for a first-ever run; a refresh has no in-flight
    status of its own to protect (see above), so its retry handling only
    ever affects whether `enrichment_status` is left alone (refresh) or
    marked `"failed"` (first-ever run) on exhausted retries.

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
        products_offered = None
        email_domain = None
        website = None
        address = None
        if source_card_id is not None:
            source_card = db.get(VisitingCard, uuid.UUID(source_card_id))
            if source_card is not None:
                gst_number = source_card.gst_number
                refund_user_id = source_card.user_id
                products_offered = source_card.products_offered
                email_domain = _card_email_domain(db, source_card.card_id)
                website = source_card.website
                address = source_card.address

        is_refresh = refresh_tiers is not None
        is_retry = self.request.retries > 0
        effective_tiers: list[str] | None = None

        if is_refresh:
            if company.enrichment_status != "enriched":
                logger.info(
                    "enrich_company_task refresh: company_id %s status=%s, skipping",
                    company_id, company.enrichment_status,
                )
                return
            existing_signals = db.get(CompanySignals, company.company_id)
            effective_tiers = [
                t for t in refresh_tiers if t in enrichment_service.stale_tiers(existing_signals)
            ]
        elif not is_retry:
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
                db, company, gst_number, email_domain, website, products_offered, address,
                refresh_tiers=effective_tiers,
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
                if not is_refresh:
                    # A first-ever run that never completes has no usable
                    # data at all — mark it failed. A refresh that fails
                    # leaves enrichment_status exactly as it was
                    # ("enriched") — the existing (if stale) cached data is
                    # still perfectly usable, so there's nothing to demote.
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

        if not is_refresh and company.industry is None:
            # Never re-classify an already-classified company — same
            # caching principle as the rest of enrichment. Classification
            # failures (a dead/unreachable website, no keyword match
            # anywhere) must never fail this task — fetch_website_text
            # and classify_industry are both already fail-safe (never
            # raise), so no extra try/except is needed here.
            website_text = fetch_website_text(company.website) if company.website else None
            industry = classify_industry(
                products_offered=products_offered,
                website_text=website_text,
                company_name=company.name,
            )
            if industry is not None:
                company.industry = industry

        now = datetime.now(timezone.utc)
        company.summary = summary
        company.summary_generated_at = now
        if not is_refresh:
            company.enrichment_status = "enriched" if any_signal_found else "not_found"
            company.enriched_at = now

        # company_enriched_at stamping (the per-lead cooldown anchor, see
        # lead_cooldown_service.py) — every sibling card riding on this
        # company that doesn't have one yet gets filled first, then the
        # triggering card's own timestamp is unconditionally overwritten
        # (restarting its cooldown on a refresh; redundant but harmless on a
        # first-ever run). Order matters: filling nulls first, then
        # overwriting the triggering card specifically, so the bulk update's
        # WHERE ... IS NULL clause can't accidentally skip it.
        db.execute(
            update(VisitingCard)
            .where(
                VisitingCard.company_id == company.company_id,
                VisitingCard.company_enriched_at.is_(None),
            )
            .values(company_enriched_at=now)
        )
        if source_card_id is not None:
            db.execute(
                update(VisitingCard)
                .where(VisitingCard.card_id == uuid.UUID(source_card_id))
                .values(company_enriched_at=now)
            )

        db.commit()
    finally:
        db.close()


@celery_app.task(
    name="app.workers.enrichment_processing.rerun_indiamart_supplier_profile_task",
    bind=True,
    max_retries=_MAX_ENRICHMENT_RETRIES,
)
def rerun_indiamart_supplier_profile_task(
    self, company_id: str, catalog_url: str, source_card_id: str
) -> None:
    """Re-runs the IndiaMART supplier-profile Apify lookup against a
    user-corrected catalog_url — see .claude/specs/20-field-correction.md.
    Mirrors enrich_company_task's fresh-SessionLocal/retry shape, but has no
    pending/enriching status-machine guard to protect: CompanySignals
    carries no such state, and re-running against the same catalog_url is
    naturally idempotent.

    Never billed (see the spec's billing amendment — correcting a URL fixes
    a mistake in an already-paid-for enrichment, not a new billable action),
    so unlike enrich_company_task there is no `billed` flag and nothing to
    refund on a permanent failure — just log it.
    """
    db = SessionLocal()
    try:
        company = db.get(Company, uuid.UUID(company_id))
        if company is None:
            logger.warning(
                "rerun_indiamart_supplier_profile_task: company_id %s not found", company_id
            )
            return

        try:
            enrichment_service.rerun_supplier_profile_lookup(db, company, catalog_url)
        except Exception as exc:
            countdown = 2**self.request.retries
            try:
                self.retry(countdown=countdown, max_retries=_MAX_ENRICHMENT_RETRIES)
            except MaxRetriesExceededError:
                logger.error(
                    "rerun_indiamart_supplier_profile_task: exhausted retries for "
                    "company_id=%s, source_card_id=%s: %s",
                    company_id, source_card_id, exc,
                )
                db.rollback()
            return

        db.commit()
    finally:
        db.close()
