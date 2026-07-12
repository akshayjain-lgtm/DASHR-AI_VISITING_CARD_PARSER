import io
import uuid
import zipfile

import pypdfium2
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.archive_upload import ArchiveUpload
from app.models.user import User
from app.services import exhibition_service, storage_service
from app.services.archive_reading import list_zip_image_entries, sniff_container_type
from app.services.exceptions import (
    ArchiveNotFoundError,
    BatchTooLargeError,
    CorruptArchiveError,
    EmptyBatchError,
    FileTooLargeError,
)
from app.services.file_reading import read_limited
from app.services.visibility import scope_to_visible_users
from app.workers.archive_processing import expand_archive_upload


def create_archive_upload(
    db: Session,
    current_user: User,
    exhibition_id: uuid.UUID | None,
    file: UploadFile,
) -> ArchiveUpload:
    """Deliberately does only cheap structural validation synchronously —
    can we open this container, and roughly how many images would it
    produce. Verifying/rendering the actual images is real, possibly slow
    work (CLAUDE.md: bulk processing must never block a request), so that's
    all deferred to expand_archive_upload, enqueued at the end of this
    function."""
    if exhibition_id is not None:
        # Raises ExhibitionNotFoundError before any file I/O if the
        # exhibition doesn't exist or isn't visible to this caller.
        exhibition_service.get_visible_exhibition(db, current_user, exhibition_id)

    data = read_limited(file.file, settings.max_archive_file_size_bytes)
    if len(data) > settings.max_archive_file_size_bytes:
        raise FileTooLargeError(
            f"File exceeds the max size of {settings.max_archive_file_size_mb}MB"
        )

    container_type = sniff_container_type(data)

    if container_type == "zip":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                entries = list_zip_image_entries(zf)
        except zipfile.BadZipFile:
            raise CorruptArchiveError("File is not a valid ZIP archive")
        if not entries:
            raise EmptyBatchError("Zip contains no readable images")
        if len(entries) > settings.max_bulk_upload_files:
            raise BatchTooLargeError(
                f"Zip contains {len(entries)} images, exceeding the max of "
                f"{settings.max_bulk_upload_files}"
            )
    else:
        try:
            pdf = pypdfium2.PdfDocument(data)
            page_count = len(pdf)
        except Exception:
            raise CorruptArchiveError("File is not a valid PDF")
        if page_count == 0:
            raise EmptyBatchError("PDF has no pages")
        if page_count > settings.max_bulk_upload_files:
            raise BatchTooLargeError(
                f"PDF has {page_count} pages, exceeding the max of "
                f"{settings.max_bulk_upload_files}"
            )

    archive_id = uuid.uuid4()
    ext = ".zip" if container_type == "zip" else ".pdf"
    key = f"archives/{current_user.user_id}/{archive_id}{ext}"
    # Stored Content-Type is derived from the already-sniffed container_type,
    # never the client-declared file.content_type — that header is
    # attacker-controlled and, if this object were ever served back (a
    # presigned URL, a future debug/download route), an untrusted value
    # there could make a browser render it as something other than inert
    # archive bytes.
    storage_content_type = "application/zip" if container_type == "zip" else "application/pdf"
    storage_service.upload_file(key, data, storage_content_type)

    archive = ArchiveUpload(
        archive_id=archive_id,
        user_id=current_user.user_id,
        exhibition_id=exhibition_id,
        original_filename=file.filename,
        container_type=container_type,
        storage_key=key,
        status="processing",
    )
    # Unlike a per-card enqueue failure elsewhere (enqueue_processing,
    # reprocess_card — which just logs and leaves the card individually
    # recoverable via a "reprocess" retry), an archive_uploads row has no
    # equivalent per-row retry path. A failure anywhere in this block —
    # committing the row itself, or enqueueing the Celery task once it
    # exists — would otherwise leave the just-uploaded archive object
    # orphaned in storage (row never created, or stuck at
    # status="processing" forever with zero cards ever made), so both
    # failure points roll back the storage upload the same way.
    try:
        db.add(archive)
        db.commit()
        db.refresh(archive)
        expand_archive_upload.delay(str(archive.archive_id))
    except Exception:
        db.rollback()
        # A rollback discards an uncommitted add(), but not a commit that
        # already succeeded — re-querying tells us which failure this was:
        # if the row is there, the commit succeeded and only delay() failed,
        # so the row itself still needs deleting.
        persisted = db.get(ArchiveUpload, archive_id)
        if persisted is not None:
            db.delete(persisted)
            db.commit()
        storage_service.delete_file(key)
        raise

    return archive


def get_visible_archive_upload(
    db: Session, current_user: User, archive_id: uuid.UUID
) -> ArchiveUpload:
    stmt = scope_to_visible_users(select(ArchiveUpload), current_user, ArchiveUpload.user_id)
    stmt = stmt.where(ArchiveUpload.archive_id == archive_id)
    archive = db.scalar(stmt)
    if archive is None:
        raise ArchiveNotFoundError()
    return archive
