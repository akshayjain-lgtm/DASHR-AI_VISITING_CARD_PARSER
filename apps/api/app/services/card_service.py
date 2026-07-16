import logging
import uuid
from typing import Callable

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.card_email import CardEmail
from app.models.card_phone import CardPhone
from app.models.company import Company
from app.models.company_signals import CompanySignals
from app.models.exhibition import Exhibition
from app.models.user import User
from app.models.visiting_card import VisitingCard
from app.services import billing, exhibition_service, storage_service
from app.services.exceptions import (
    BatchTooLargeError,
    CardAlreadyScoredError,
    CardHasMergedChildrenError,
    CardHasNoCompanyError,
    CardNotEligibleForScoringError,
    CardNotFoundError,
    CardStateChangedError,
    CompanyNotEligibleForEnrichmentError,
    EmptyBatchError,
    FileTooLargeError,
    InvalidReprocessStateError,
    UnsupportedFileTypeError,
)
from app.services.file_reading import read_limited, verify_image_content
from app.services.visibility import scope_to_visible_users
from app.workers.card_processing import process_card
from app.workers.enrichment_processing import enrich_company_task
from app.workers.scoring_processing import score_card_task

logger = logging.getLogger(__name__)


def _card_scoring_fields(card: VisitingCard) -> dict:
    """Shared by to_card_out/list_cards/get_card_detail so the
    lead_score/score_breakdown/scored_at triple is only ever read off the
    ORM row in one place."""
    return {
        "lead_score": card.lead_score,
        "score_breakdown": card.score_breakdown,
        "scored_at": card.scored_at,
    }


def _read_and_validate_batch(files: list[UploadFile]) -> list[tuple[UploadFile, bytes]]:
    """Validates every file's content-type, actual content, and size, and
    the batch's file count, before any file is touched by storage or the
    database — so the whole request can be rejected without ever writing a
    partial batch."""
    if not files:
        raise EmptyBatchError("At least one file is required")
    if len(files) > settings.max_bulk_upload_files:
        raise BatchTooLargeError(
            f"Batch of {len(files)} files exceeds the max of "
            f"{settings.max_bulk_upload_files}"
        )

    validated: list[tuple[UploadFile, bytes]] = []
    for f in files:
        if f.content_type not in settings.allowed_card_image_content_types:
            raise UnsupportedFileTypeError(
                f"Unsupported file type for {f.filename}: {f.content_type}"
            )
        data = read_limited(f.file, settings.max_upload_file_size_bytes)
        if len(data) > settings.max_upload_file_size_bytes:
            raise FileTooLargeError(
                f"{f.filename} exceeds the max size of "
                f"{settings.max_upload_file_size_mb}MB"
            )
        verify_image_content(data, f.content_type, f.filename)
        validated.append((f, data))
    return validated


def bulk_upload_cards(
    db: Session,
    current_user: User,
    exhibition_id: uuid.UUID | None,
    files: list[UploadFile],
) -> list[VisitingCard]:
    if exhibition_id is not None:
        # Raises ExhibitionNotFoundError before any file I/O if the
        # exhibition doesn't exist or isn't visible to this caller.
        exhibition_service.get_visible_exhibition(db, current_user, exhibition_id)

    validated = _read_and_validate_batch(files)

    # card_id is generated client-side (rather than relying on the DB's
    # gen_random_uuid() default) so every file can be uploaded to storage
    # *before* any database row is created — keeping the DB transaction to
    # a single add_all()+commit() instead of holding it open across up to
    # max_bulk_upload_files sequential, synchronous S3 calls.
    cards: list[VisitingCard] = []
    uploaded_keys: list[str] = []
    # One id shared by every card in this request, so extraction can later
    # correlate photos uploaded together (e.g. to detect a back-of-card scan
    # immediately following its front) via batch_sequence order.
    upload_batch_id = uuid.uuid4()
    try:
        for i, (f, data) in enumerate(validated):
            card_id = uuid.uuid4()
            ext = "." + f.content_type.split("/")[-1]
            key = f"cards/{current_user.user_id}/{card_id}{ext}"
            storage_service.upload_file(key, data, f.content_type)
            uploaded_keys.append(key)
            cards.append(
                VisitingCard(
                    card_id=card_id,
                    user_id=current_user.user_id,
                    exhibition_id=exhibition_id,
                    original_filename=f.filename,
                    image_url=key,
                    status="new",
                    upload_batch_id=upload_batch_id,
                    batch_sequence=i,
                )
            )
    except Exception:
        for key in uploaded_keys:
            storage_service.delete_file(key)
        raise

    try:
        db.add_all(cards)
        db.commit()
    except Exception:
        db.rollback()
        for key in uploaded_keys:
            storage_service.delete_file(key)
        raise

    # Deliberately NOT enqueuing process_card here — parsing is a separate,
    # explicit user action (POST /cards/process) rather than something that
    # happens automatically the instant a batch finishes uploading. Cards sit
    # at status='new' until the caller triggers extraction.
    return cards


def list_cards(
    db: Session,
    current_user: User,
    exhibition_id: uuid.UUID | None,
    status: str | None,
    limit: int,
    offset: int,
    include_folded: bool = False,
    unassigned: bool = False,
) -> list[dict]:
    stmt = scope_to_visible_users(
        select(VisitingCard, Company.name, Company.enrichment_status).outerjoin(
            Company, VisitingCard.company_id == Company.company_id
        ),
        current_user,
        VisitingCard.user_id,
    )
    # unassigned=True (the "General capture" filter) takes priority over an
    # exhibition_id, which the caller never sends alongside it anyway — this
    # mirrors the "no exhibition" bucket in the upload page's exhibition
    # picker, distinct from omitting the filter entirely (which returns
    # cards across every exhibition, the picker's separate "All" option).
    if unassigned:
        stmt = stmt.where(VisitingCard.exhibition_id.is_(None))
    elif exhibition_id is not None:
        stmt = stmt.where(VisitingCard.exhibition_id == exhibition_id)
    if status is not None:
        stmt = stmt.where(VisitingCard.status == status)
    elif not include_folded:
        # A back-of-card scan or a re-scan of an already-captured contact
        # isn't a separate lead — hide it from the default list. Still
        # reachable via an explicit ?status=merged/duplicate for audit, or
        # ?include_folded=true to see everything (e.g. the upload review
        # screen, where silently dropping a row is confusing).
        stmt = stmt.where(VisitingCard.status.notin_(("merged", "duplicate")))
    stmt = stmt.order_by(VisitingCard.created_at.desc()).limit(limit).offset(offset)

    rows = db.execute(stmt).all()
    return [
        {
            "card_id": c.card_id,
            "user_id": c.user_id,
            "exhibition_id": c.exhibition_id,
            "original_filename": c.original_filename,
            "image_url": storage_service.generate_presigned_url(c.image_url)
            if c.image_url
            else "",
            "status": c.status,
            "full_name": c.full_name,
            "job_title": c.job_title,
            "merged_into_card_id": c.merged_into_card_id,
            "created_at": c.created_at,
            "company_id": c.company_id,
            "company_name": company_name,
            "company_enrichment_status": company_enrichment_status,
            **_card_scoring_fields(c),
        }
        for c, company_name, company_enrichment_status in rows
    ]


def _charge_and_enqueue(
    db: Session,
    user_id: uuid.UUID,
    action_type: str,
    eligible: list[VisitingCard],
    enqueue_one: Callable[[VisitingCard, bool], None],
) -> tuple[int, int]:
    """Shared charge-then-enqueue-subset sequence for enqueue_processing/
    enqueue_enrichment/enqueue_scoring: charges the whole eligible batch as
    one collective WalletTransaction (billing.charge_for_bulk_action, not
    one row per card), then calls enqueue_one(card, billed) for only the
    first `chargeable` of them, in list order — the remainder is left
    untouched (still retryable) and reported as wallet-blocked.

    charge_for_bulk_action returns (free_used, paid_used): the first
    free_used of the chargeable subset were free, the rest were billed —
    that per-card billed flag is passed into enqueue_one so the Celery task
    itself can call billing.refund_action if the actual parse/enrich/score
    work later fails (see process_card/enrich_company_task/score_card_task).

    enqueue_one failures (the .delay() call itself raising, never reaching
    the broker) are logged and moved past rather than failing the rest of
    the batch; unlike a task that WAS enqueued and later fails, that rare
    case is not refunded here — the card is still eligible for a future
    bulk retry with a fresh charge, whereas refunding immediately would risk
    a double-refund if the message actually did reach the broker despite
    the raised exception (Celery's own docs note this ambiguity for some
    broker transports).

    Returns (enqueued_count, wallet_blocked_count).
    """
    if not eligible:
        return 0, 0

    free_used, paid_used = billing.charge_for_bulk_action(
        db, user_id, action_type, len(eligible), reference_id=eligible[0].card_id
    )
    chargeable = free_used + paid_used

    enqueued = 0
    for index, card in enumerate(eligible[:chargeable]):
        billed = index >= free_used
        try:
            enqueue_one(card, billed)
        except Exception:
            logger.exception(
                "Failed to enqueue %s action for card_id=%s", action_type, card.card_id
            )
        enqueued += 1

    return enqueued, len(eligible) - chargeable


def enqueue_processing(
    db: Session,
    current_user: User,
    exhibition_id: uuid.UUID | None,
    card_ids: list[uuid.UUID] | None = None,
) -> tuple[int, int]:
    """The explicit "Parse Cards" CTA action — enqueues process_card for
    every status='new' card visible to current_user (own cards, or every org
    member's if admin), optionally narrowed to one exhibition and/or to a
    specific set of card_ids (a client-picked selection — still re-validated
    here for visibility and status, never trusted as-is; a duplicate id in
    the caller's own list is naturally collapsed by the SQL IN clause below,
    never double-counted). Returns (enqueued_count, wallet_blocked_count):
    cards this batch can't afford are left status='new' and skipped rather
    than enqueued, so they stay retryable once the user is funded."""
    stmt = scope_to_visible_users(select(VisitingCard), current_user, VisitingCard.user_id).where(
        VisitingCard.status == "new"
    )
    if exhibition_id is not None:
        stmt = stmt.where(VisitingCard.exhibition_id == exhibition_id)
    if card_ids is not None:
        stmt = stmt.where(VisitingCard.card_id.in_(card_ids))

    cards = list(db.scalars(stmt).all())
    return _charge_and_enqueue(
        db,
        current_user.user_id,
        "parse",
        cards,
        lambda card, billed: process_card.delay(str(card.card_id), billed=billed),
    )


def to_card_out(db: Session, card: VisitingCard) -> dict:
    """Builds the dict CardOut expects from a single just-mutated VisitingCard
    row (reprocess, enrich-company) — these routes only have one ORM object
    in hand, not list_cards' joined query, so company_enrichment_status is
    looked up here instead. image_url is left as the raw storage key (not
    presigned), matching this pair of routes' existing behavior."""
    company_name = None
    company_enrichment_status = None
    if card.company_id is not None:
        company = db.get(Company, card.company_id)
        company_name = company.name if company else None
        company_enrichment_status = company.enrichment_status if company else None
    return {
        "card_id": card.card_id,
        "user_id": card.user_id,
        "exhibition_id": card.exhibition_id,
        "original_filename": card.original_filename,
        "image_url": card.image_url,
        "status": card.status,
        "full_name": card.full_name,
        "job_title": card.job_title,
        "merged_into_card_id": card.merged_into_card_id,
        "created_at": card.created_at,
        "company_id": card.company_id,
        "company_name": company_name,
        "company_enrichment_status": company_enrichment_status,
        **_card_scoring_fields(card),
    }


def get_visible_card(
    db: Session, current_user: User, card_id: uuid.UUID, *, for_update: bool = False
) -> VisitingCard:
    """Mirrors exhibition_service.get_visible_exhibition — raises
    CardNotFoundError if the card doesn't exist or isn't visible to
    current_user under the admin-sees-org-member rule.

    Pass for_update=True to lock the row (SELECT ... FOR UPDATE) for the
    rest of this transaction — used by reprocess_card so a concurrent
    duplicate reprocess request for the same card blocks until the first
    one commits, then correctly re-reads the (now "new", not "failed")
    status and is rejected, instead of both requests racing past the
    eligibility check and both getting charged for one reprocess."""
    stmt = scope_to_visible_users(select(VisitingCard), current_user, VisitingCard.user_id)
    stmt = stmt.where(VisitingCard.card_id == card_id)
    if for_update:
        stmt = stmt.with_for_update()
    card = db.scalar(stmt)
    if card is None:
        raise CardNotFoundError()
    return card


def _load_emails_and_phones(
    db: Session, card_id: uuid.UUID
) -> tuple[list[CardEmail], list[CardPhone]]:
    """Shared by get_card_detail/_export_row so the "ordered primary-first"
    CardEmail/CardPhone query lives in exactly one place."""
    emails = db.scalars(
        select(CardEmail)
        .where(CardEmail.card_id == card_id)
        .order_by(CardEmail.is_primary.desc())
    ).all()
    phones = db.scalars(
        select(CardPhone)
        .where(CardPhone.card_id == card_id)
        .order_by(CardPhone.is_primary.desc())
    ).all()
    return emails, phones


def get_card_detail(db: Session, current_user: User, card_id: uuid.UUID) -> dict:
    card = get_visible_card(db, current_user, card_id)
    company = db.get(Company, card.company_id) if card.company_id else None
    signals = db.get(CompanySignals, company.company_id) if company else None
    emails, phones = _load_emails_and_phones(db, card.card_id)

    return {
        "card_id": card.card_id,
        "user_id": card.user_id,
        "exhibition_id": card.exhibition_id,
        "original_filename": card.original_filename,
        "image_url": storage_service.generate_presigned_url(card.image_url)
        if card.image_url
        else "",
        "status": card.status,
        "full_name": card.full_name,
        "job_title": card.job_title,
        "designation_level": card.designation_level,
        "special_remark": card.special_remark,
        "website": card.website,
        "address": card.address,
        "products_offered": card.products_offered,
        "gst_number": card.gst_number,
        "raw_ocr_text": card.raw_ocr_text,
        "extraction_error": card.extraction_error,
        "merged_into_card_id": card.merged_into_card_id,
        "created_at": card.created_at,
        **_card_scoring_fields(card),
        "company": {
            "company_id": company.company_id,
            "name": company.name,
            "domain": company.domain,
            "website": company.website,
            "enrichment_status": company.enrichment_status,
            "summary": company.summary,
            "summary_generated_at": company.summary_generated_at,
            "linkedin_employee_count": signals.linkedin_employee_count if signals else None,
            "estimated_revenue_band": signals.estimated_revenue_band if signals else None,
            "gstin_verified": signals.gstin_verified if signals else None,
            "udyam_registered": signals.udyam_registered if signals else None,
            "hiring_signal": signals.hiring_signal if signals else None,
            "google_rating": signals.google_rating if signals else None,
        }
        if company
        else None,
        "emails": [
            {"email": e.email, "email_type": e.email_type, "is_primary": e.is_primary}
            for e in emails
        ],
        "phones": [
            {
                "phone_e164": p.phone_e164,
                "phone_raw": p.phone_raw,
                "phone_type": p.phone_type,
                "is_primary": p.is_primary,
            }
            for p in phones
        ],
    }


def export_cards(db: Session, current_user: User, card_ids: list[uuid.UUID]) -> list[dict]:
    """Best-effort batch read for POST /cards/export. Ids not visible to
    current_user (wrong owner, different org, or nonexistent) are silently
    dropped rather than raising.

    The visibility check and the Company/CompanySignals/Exhibition lookups
    are all batched into one query each — none of those are 1:many off
    VisitingCard, so batching them carries no row-fan-out risk regardless of
    how many ids are requested. Only CardEmail/CardPhone stay a per-card
    query (via _load_emails_and_phones, shared with get_card_detail): a
    joined 1:many query there would fan the row count out by every
    email/phone combination per card, which is far more error-prone to
    de-duplicate correctly than the handful of extra queries this costs at
    the 200-id cap CardExportRequest already enforces.
    """
    cards = db.scalars(
        scope_to_visible_users(select(VisitingCard), current_user, VisitingCard.user_id).where(
            VisitingCard.card_id.in_(card_ids)
        )
    ).all()
    cards_by_id = {c.card_id: c for c in cards}
    # Preserves the caller's requested order (the DB's return order for an
    # IN(...) query is unspecified) and silently drops any id that wasn't
    # visible/found — the "best-effort" contract POST /cards/export promises.
    ordered_cards = [cards_by_id[cid] for cid in card_ids if cid in cards_by_id]
    if not ordered_cards:
        return []

    company_ids = {c.company_id for c in ordered_cards if c.company_id}
    exhibition_ids = {c.exhibition_id for c in ordered_cards if c.exhibition_id}

    companies_by_id = (
        {c.company_id: c for c in db.scalars(select(Company).where(Company.company_id.in_(company_ids))).all()}
        if company_ids
        else {}
    )
    signals_by_company_id = (
        {
            s.company_id: s
            for s in db.scalars(
                select(CompanySignals).where(CompanySignals.company_id.in_(company_ids))
            ).all()
        }
        if company_ids
        else {}
    )
    exhibitions_by_id = (
        {
            e.exhibition_id: e
            for e in db.scalars(
                select(Exhibition).where(Exhibition.exhibition_id.in_(exhibition_ids))
            ).all()
        }
        if exhibition_ids
        else {}
    )

    rows: list[dict] = []
    for card in ordered_cards:
        company = companies_by_id.get(card.company_id) if card.company_id else None
        signals = signals_by_company_id.get(company.company_id) if company else None
        exhibition = exhibitions_by_id.get(card.exhibition_id) if card.exhibition_id else None
        emails, phones = _load_emails_and_phones(db, card.card_id)
        rows.append(_export_row(card, company, signals, exhibition, emails, phones))
    return rows


def _export_row(
    card: VisitingCard,
    company: Company | None,
    signals: CompanySignals | None,
    exhibition: Exhibition | None,
    emails: list[CardEmail],
    phones: list[CardPhone],
) -> dict:
    """Pure assembler — turns already-loaded rows into the dict shape
    export_service.build_csv expects. No DB access itself; export_cards does
    all the loading (batched wherever the data shape allows it)."""
    return {
        "full_name": card.full_name,
        "job_title": card.job_title,
        "company_name": company.name if company else None,
        "industry": company.industry if company else None,
        "employee_count": signals.linkedin_employee_count if signals else None,
        "revenue_band": signals.estimated_revenue_band if signals else None,
        "emails": [{"email": e.email, "is_primary": e.is_primary} for e in emails],
        "phones": [
            {"phone": p.phone_e164 or p.phone_raw, "is_primary": p.is_primary}
            for p in phones
        ],
        "website": card.website,
        "address": card.address,
        "gst_number": card.gst_number,
        "products_offered": card.products_offered,
        "designation_level": card.designation_level,
        "lead_score": float(card.lead_score) if card.lead_score is not None else None,
        "special_remark": card.special_remark,
        "exhibition_name": exhibition.name if exhibition else None,
        "status": card.status,
        "scanned_on": card.created_at,
    }


def reprocess_card(db: Session, current_user: User, card_id: uuid.UUID) -> VisitingCard:
    # Locked for the rest of this transaction (see get_visible_card's
    # for_update docstring) — closes the race where two concurrent
    # reprocess requests for the same failed card could otherwise both pass
    # the status check below and both get charged for one reprocess.
    card = get_visible_card(db, current_user, card_id, for_update=True)
    if card.status != "failed":
        raise InvalidReprocessStateError()

    # Stage the flip without committing yet — charge_for_action's own
    # commit (below) persists this together with the charge as one atomic
    # transaction, so a failed charge (InsufficientBalanceError, which rolls
    # back before raising) also rolls back this staged change, leaving the
    # card exactly as it was (still "failed"), never half-flipped to "new".
    card.status = "new"
    card.extraction_error = None
    billed = billing.charge_for_action(db, current_user.user_id, "parse", reference_id=card.card_id)
    db.refresh(card)

    try:
        process_card.delay(str(card.card_id), billed=billed)
    except Exception:
        logger.exception("Failed to enqueue reprocess for card_id=%s", card.card_id)
        # The enqueue itself never reached the broker — this work was never
        # even attempted, unlike a task that ran and failed (e.g. bad OCR),
        # which stays non-refundable. Reverse the charge so the user isn't
        # billed for an action that will never run; the card stays "new"
        # and remains retryable via a later bulk "Parse Cards" action.
        billing.refund_action(db, current_user.user_id, "parse", billed=billed, reference_id=card.card_id)

    return card


def enrich_company_now(db: Session, current_user: User, card_id: uuid.UUID) -> VisitingCard:
    """The explicit "Enrich Company" CTA — POST /cards/{card_id}/enrich-company.

    Enrichment never runs automatically after parsing; a seller has to ask for
    it, mirroring how "Parse Cards" is itself a separate explicit action from
    upload rather than an automatic side effect.
    """
    card = get_visible_card(db, current_user, card_id)
    if card.company_id is None:
        raise CardHasNoCompanyError()

    company = db.get(Company, card.company_id)
    if company is None or company.enrichment_status != "pending":
        raise CompanyNotEligibleForEnrichmentError()

    billed = billing.charge_for_action(
        db, current_user.user_id, "enrichment", reference_id=card.card_id
    )

    try:
        enrich_company_task.delay(str(card.company_id), str(card.card_id), billed=billed)
    except Exception:
        logger.exception(
            "Failed to enqueue enrich_company_task for company_id=%s", card.company_id
        )
        # Never even queued — reverse the charge (see reprocess_card's
        # identical rationale).
        billing.refund_action(
            db, current_user.user_id, "enrichment", billed=billed, reference_id=card.card_id
        )

    return card


def enqueue_enrichment(
    db: Session, current_user: User, card_ids: list[uuid.UUID]
) -> tuple[int, int, int]:
    """Bulk counterpart to enrich_company_now — the "Enrich Selected" CTA.

    Unlike enrich_company_now, an ineligible card_id (not visible, no linked
    company, company not "pending", or a company already mapped to an
    earlier id in this same call) is silently skipped and counted in
    skipped_count rather than raising: this is a best-effort batch over a
    user-picked selection, not the single guarded action enrich_company_now
    already is. Never enqueues the same company twice in one call — two
    selected cards can legitimately share one still-pending Company row,
    and this same dedup also naturally absorbs an exact-duplicate card_id in
    the caller's own list (the second occurrence maps to an already-seen
    company_id and is skipped, never charged/enqueued twice).

    The whole eligible batch is charged as one collective WalletTransaction
    (billing.charge_for_bulk_action) covering however many of it the wallet
    can afford, not one ledger row per card; any remainder is wallet-blocked
    and left untouched (still "pending", still retryable).
    """
    skipped = 0
    eligible: list[VisitingCard] = []
    seen_company_ids: set[uuid.UUID] = set()
    for card_id in card_ids:
        try:
            card = get_visible_card(db, current_user, card_id)
        except CardNotFoundError:
            skipped += 1
            continue

        if card.company_id is None or card.company_id in seen_company_ids:
            skipped += 1
            continue

        company = db.get(Company, card.company_id)
        if company is None or company.enrichment_status != "pending":
            skipped += 1
            continue

        seen_company_ids.add(card.company_id)
        eligible.append(card)

    enqueued, wallet_blocked = _charge_and_enqueue(
        db,
        current_user.user_id,
        "enrichment",
        eligible,
        lambda card, billed: enrich_company_task.delay(
            str(card.company_id), str(card.card_id), billed=billed
        ),
    )
    return enqueued, skipped, wallet_blocked


def score_card_now(db: Session, current_user: User, card_id: uuid.UUID) -> VisitingCard:
    """The explicit, one-shot "Score Card" CTA — POST /cards/{card_id}/score.

    Scoring is one-shot per card: once lead_score is set, re-scoring is
    rejected (CardAlreadyScoredError) rather than allowed, so a card's score
    can't drift after a seller has already acted on it. Enforced here, not
    just hidden in the UI, so a direct API call can't bypass the rule.
    """
    card = get_visible_card(db, current_user, card_id)
    if card.status != "extracted":
        raise CardNotEligibleForScoringError()
    if card.lead_score is not None:
        raise CardAlreadyScoredError()

    billed = billing.charge_for_action(db, current_user.user_id, "scoring", reference_id=card.card_id)

    try:
        score_card_task.delay(str(card.card_id), billed=billed)
    except Exception:
        logger.exception("Failed to enqueue score_card_task for card_id=%s", card.card_id)
        # Never even queued — reverse the charge (see reprocess_card's
        # identical rationale).
        billing.refund_action(db, current_user.user_id, "scoring", billed=billed, reference_id=card.card_id)

    return card


def enqueue_scoring(
    db: Session, current_user: User, card_ids: list[uuid.UUID]
) -> tuple[int, int, int]:
    """Bulk counterpart to score_card_now — the "Score" bulk CTA.

    Unlike enqueue_enrichment, there's no company-level dedupe needed here:
    scoring is purely per-card, so every eligible id enqueues its own task —
    which is exactly why an exact-duplicate card_id in the caller's own list
    must be explicitly de-duplicated below (unlike enqueue_enrichment, there
    is no company-level grouping to accidentally absorb a repeated id):
    without it, one card_id repeated N times would be charged and enqueued
    N times over for what is really one card. Same one-shot rule as
    score_card_now: already-scored cards are skipped (counted in
    skipped_count) rather than re-enqueued.

    The whole eligible batch is charged as one collective WalletTransaction
    (billing.charge_for_bulk_action) covering however many of it the wallet
    can afford, not one ledger row per card.
    """
    skipped = 0
    eligible: list[VisitingCard] = []
    seen_card_ids: set[uuid.UUID] = set()
    for card_id in card_ids:
        if card_id in seen_card_ids:
            skipped += 1
            continue
        seen_card_ids.add(card_id)

        try:
            card = get_visible_card(db, current_user, card_id)
        except CardNotFoundError:
            skipped += 1
            continue

        if card.status != "extracted" or card.lead_score is not None:
            skipped += 1
            continue

        eligible.append(card)

    enqueued, wallet_blocked = _charge_and_enqueue(
        db,
        current_user.user_id,
        "scoring",
        eligible,
        lambda card, billed: score_card_task.delay(str(card.card_id), billed=billed),
    )
    return enqueued, skipped, wallet_blocked


def delete_card(
    db: Session, current_user: User, card_id: uuid.UUID, confirm_cascade: bool
) -> None:
    """Permanently deletes a card (hard delete — no undo). If other cards were
    merged into this one (back-of-card scans or duplicates, merged_into_card_id
    pointing at card_id), those children are cascade-deleted too, but only once
    confirm_cascade=True — otherwise this raises CardHasMergedChildrenError so
    the caller can get explicit confirmation before anything is removed.
    """
    card = get_visible_card(db, current_user, card_id)

    # Scoped by merged_into_card_id alone, NOT by current_user's own
    # visibility (scope_to_visible_users): get_visible_card above already
    # proved current_user may see `card`, and a child's authorization
    # derives from having merged into an already-authorized card, not from
    # sharing the deleting user's user_id. A duplicate/back-of-card match
    # can legitimately span owners within the same org — extraction_service's
    # duplicate search is scoped to the *uploader's* own visibility, which is
    # org-wide for an admin — so re-scoping this query to current_user would
    # under-count children a non-admin owns the parent of but not the child
    # of, letting the parent get deleted while a child still FK-references it.
    children = db.scalars(
        select(VisitingCard).where(VisitingCard.merged_into_card_id == card_id)
    ).all()

    if children and not confirm_cascade:
        raise CardHasMergedChildrenError(child_count=len(children))

    keys = [c.image_url for c in [card, *children] if c.image_url]

    # Children deleted (and flushed) before the parent — merged_into_card_id
    # is a self-referencing FK with no ON DELETE rule, so the parent row
    # can't be removed first while a child still points at it. card_emails/
    # card_phones cascade at the DB level (ON DELETE CASCADE) already.
    # Company/CompanySignals are never touched here — shared reference data,
    # not owned by any one card.
    for child in children:
        db.delete(child)
    db.flush()
    db.delete(card)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent request merged a new child onto this card between our
        # SELECT above and this commit — surface a clean, retryable error
        # instead of a raw 500.
        db.rollback()
        raise CardStateChangedError()

    for key in keys:
        storage_service.delete_file(key)


def bulk_delete_cards(
    db: Session, current_user: User, card_ids: list[uuid.UUID], confirm_cascade: bool
) -> tuple[int, int]:
    """Bulk counterpart to delete_card — the "Delete Selected" CTA. Deletes
    every requested card_id visible to current_user; ids that aren't visible
    (wrong owner, different org, or nonexistent) are silently skipped and
    counted in skipped_count, same best-effort contract as
    enqueue_enrichment/enqueue_scoring/export_cards over a client-picked
    selection.

    Cascade confirmation is aggregated across the whole batch rather than
    per-card: if any selected card has merged children that *aren't
    themselves part of the selection*, this raises CardHasMergedChildrenError
    once with the total extra count, and nothing is deleted until the caller
    resends with confirm_cascade=True. A child that's already in the
    requested selection needs no extra confirmation — the user explicitly
    picked it.
    """
    requested_ids = set(card_ids)
    cards = db.scalars(
        scope_to_visible_users(select(VisitingCard), current_user, VisitingCard.user_id).where(
            VisitingCard.card_id.in_(requested_ids)
        )
    ).all()
    selected_ids = {c.card_id for c in cards}
    skipped_count = len(requested_ids) - len(selected_ids)

    # Not scoped by current_user's own visibility — same reasoning as
    # delete_card's single-card children lookup: a child's authorization
    # derives from having merged into an already-authorized card, not from
    # sharing the deleting user's user_id.
    children = db.scalars(
        select(VisitingCard).where(VisitingCard.merged_into_card_id.in_(selected_ids))
    ).all()
    extra_children = [c for c in children if c.card_id not in selected_ids]

    if extra_children and not confirm_cascade:
        raise CardHasMergedChildrenError(child_count=len(extra_children))

    to_delete = [*cards, *extra_children]
    keys = [c.image_url for c in to_delete if c.image_url]

    # Children (merged_into_card_id set) deleted and flushed before parents/
    # standalones, same ordering reason as delete_card: merged_into_card_id
    # has no ON DELETE rule, so a parent can't be removed while a child still
    # points at it.
    child_objs = [c for c in to_delete if c.merged_into_card_id is not None]
    parent_or_standalone_objs = [c for c in to_delete if c.merged_into_card_id is None]
    for child in child_objs:
        db.delete(child)
    db.flush()
    for card in parent_or_standalone_objs:
        db.delete(card)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise CardStateChangedError()

    for key in keys:
        storage_service.delete_file(key)

    return len(to_delete), skipped_count
