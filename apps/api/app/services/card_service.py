import io
import logging
import uuid

import pillow_heif
from fastapi import UploadFile
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.card_email import CardEmail
from app.models.card_phone import CardPhone
from app.models.company import Company
from app.models.user import User
from app.models.visiting_card import VisitingCard
from app.services import exhibition_service, storage_service
from app.services.exceptions import (
    BatchTooLargeError,
    CardNotFoundError,
    EmptyBatchError,
    FileTooLargeError,
    InvalidReprocessStateError,
    UnsupportedFileTypeError,
)
from app.services.visibility import scope_to_visible_users
from app.workers.card_processing import process_card

logger = logging.getLogger(__name__)

# Registers HEIC/HEIF as a Pillow-openable format (Pillow has no built-in
# decoder for it). Must run in every process that opens uploaded images with
# Pillow, not just this one — the Celery worker (extraction_service.py) opens
# the same stored bytes in a separate process and needs its own registration.
pillow_heif.register_heif_opener()

_READ_CHUNK_SIZE = 65536

# Only used to verify that a file's actual decoded bytes match its declared
# content-type — NOT the source of truth for which types are allowed
# (settings.allowed_card_image_content_types is). A type missing from this
# map simply fails verification (safe default), it never raises a KeyError.
# HEIC and HEIF both decode to Pillow's "HEIF" format name (pillow-heif
# doesn't distinguish the two containers), so both content-types map to it.
EXPECTED_IMAGE_FORMATS = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
    "image/heic": "HEIF",
    "image/heif": "HEIF",
}


def _read_limited(file_obj, max_bytes: int) -> bytes:
    """Reads at most ~max_bytes + one chunk before giving up, so an
    oversized upload doesn't get fully buffered into memory before its size
    is checked."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = file_obj.read(_READ_CHUNK_SIZE)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            break
    return b"".join(chunks)


def _verify_image_content(data: bytes, content_type: str, filename: str | None) -> None:
    """Confirms `data` actually decodes as an image matching the declared
    content-type, rather than trusting the client-supplied header alone."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            actual_format = img.format
    except Exception:
        raise UnsupportedFileTypeError(f"{filename or 'file'} is not a valid image")

    if actual_format != EXPECTED_IMAGE_FORMATS.get(content_type):
        raise UnsupportedFileTypeError(
            f"{filename or 'file'} content does not match its declared type {content_type}"
        )


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
        data = _read_limited(f.file, settings.max_upload_file_size_bytes)
        if len(data) > settings.max_upload_file_size_bytes:
            raise FileTooLargeError(
                f"{f.filename} exceeds the max size of "
                f"{settings.max_upload_file_size_mb}MB"
            )
        _verify_image_content(data, f.content_type, f.filename)
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
) -> list[dict]:
    stmt = scope_to_visible_users(select(VisitingCard), current_user, VisitingCard.user_id)
    if exhibition_id is not None:
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

    cards = db.scalars(stmt).all()
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
        }
        for c in cards
    ]


def enqueue_processing(
    db: Session, current_user: User, exhibition_id: uuid.UUID | None
) -> int:
    """The explicit "Parse Cards" CTA action — enqueues process_card for
    every status='new' card visible to current_user (own cards, or every org
    member's if admin), optionally narrowed to one exhibition. Returns the
    number of cards matched/attempted, mirroring reprocess_card's
    log-and-move-on handling of broker failures rather than letting one
    enqueue failure fail the whole batch."""
    stmt = scope_to_visible_users(select(VisitingCard), current_user, VisitingCard.user_id).where(
        VisitingCard.status == "new"
    )
    if exhibition_id is not None:
        stmt = stmt.where(VisitingCard.exhibition_id == exhibition_id)

    cards = db.scalars(stmt).all()
    for card in cards:
        try:
            process_card.delay(str(card.card_id))
        except Exception:
            logger.exception(
                "Failed to enqueue process_card for card_id=%s", card.card_id
            )

    return len(cards)


def get_visible_card(db: Session, current_user: User, card_id: uuid.UUID) -> VisitingCard:
    """Mirrors exhibition_service.get_visible_exhibition — raises
    CardNotFoundError if the card doesn't exist or isn't visible to
    current_user under the admin-sees-org-member rule."""
    stmt = scope_to_visible_users(select(VisitingCard), current_user, VisitingCard.user_id)
    stmt = stmt.where(VisitingCard.card_id == card_id)
    card = db.scalar(stmt)
    if card is None:
        raise CardNotFoundError()
    return card


def get_card_detail(db: Session, current_user: User, card_id: uuid.UUID) -> dict:
    card = get_visible_card(db, current_user, card_id)
    company = db.get(Company, card.company_id) if card.company_id else None
    emails = db.scalars(
        select(CardEmail)
        .where(CardEmail.card_id == card.card_id)
        .order_by(CardEmail.is_primary.desc())
    ).all()
    phones = db.scalars(
        select(CardPhone)
        .where(CardPhone.card_id == card.card_id)
        .order_by(CardPhone.is_primary.desc())
    ).all()

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
        "company": {
            "company_id": company.company_id,
            "name": company.name,
            "domain": company.domain,
            "website": company.website,
            "enrichment_status": company.enrichment_status,
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


def reprocess_card(db: Session, current_user: User, card_id: uuid.UUID) -> VisitingCard:
    card = get_visible_card(db, current_user, card_id)
    if card.status != "failed":
        raise InvalidReprocessStateError()

    card.status = "new"
    card.extraction_error = None
    db.commit()
    db.refresh(card)

    try:
        process_card.delay(str(card.card_id))
    except Exception:
        logger.exception("Failed to enqueue reprocess for card_id=%s", card.card_id)

    return card
