import io
import logging
import uuid

from fastapi import UploadFile
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.user import User
from app.models.visiting_card import VisitingCard
from app.services import exhibition_service, storage_service
from app.services.exceptions import (
    BatchTooLargeError,
    EmptyBatchError,
    FileTooLargeError,
    UnsupportedFileTypeError,
)
from app.services.visibility import scope_to_visible_users
from app.workers.card_processing import process_card

logger = logging.getLogger(__name__)

_READ_CHUNK_SIZE = 65536

# Only used to verify that a file's actual decoded bytes match its declared
# content-type — NOT the source of truth for which types are allowed
# (settings.allowed_card_image_content_types is). A type missing from this
# map simply fails verification (safe default), it never raises a KeyError.
EXPECTED_IMAGE_FORMATS = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
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
    try:
        for f, data in validated:
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

    for card in cards:
        # A broker outage here must not turn N already-committed cards into
        # a 500 response — log and move on rather than raising, since the
        # rows are real and just won't have been queued for processing yet.
        try:
            process_card.delay(str(card.card_id))
        except Exception:
            logger.exception(
                "Failed to enqueue process_card for card_id=%s", card.card_id
            )

    return cards


def list_cards(
    db: Session,
    current_user: User,
    exhibition_id: uuid.UUID | None,
    status: str | None,
    limit: int,
    offset: int,
) -> list[dict]:
    stmt = scope_to_visible_users(select(VisitingCard), current_user, VisitingCard.user_id)
    if exhibition_id is not None:
        stmt = stmt.where(VisitingCard.exhibition_id == exhibition_id)
    if status is not None:
        stmt = stmt.where(VisitingCard.status == status)
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
            "created_at": c.created_at,
        }
        for c in cards
    ]
