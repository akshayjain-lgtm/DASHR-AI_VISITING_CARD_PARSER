import io
import logging
import uuid
import zipfile

import pypdfium2

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.archive_upload import ArchiveUpload
from app.models.visiting_card import VisitingCard
from app.services import storage_service
from app.services.archive_reading import content_type_for_entry, list_zip_image_entries
from app.services.file_reading import read_limited, verify_image_content
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.workers.archive_processing.expand_archive_upload",
    soft_time_limit=600,
    time_limit=660,
)
def expand_archive_upload(archive_id: str) -> None:
    """Unpacks a zip/pdf into individual VisitingCard rows, one commit per
    card so cards appear progressively as this works — the frontend's
    existing card-list polling picks them up with no new polling logic of
    its own. No Celery-level retry: a corrupt zip/pdf won't fix itself on
    redelivery, unlike process_card's transient vision-API failures.
    """
    db = SessionLocal()
    try:
        archive = db.get(ArchiveUpload, uuid.UUID(archive_id))
        if archive is None or archive.status != "processing":
            return

        data = storage_service.download_file(archive.storage_key)
        created = 0
        skipped = 0

        if archive.container_type == "zip":
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = list_zip_image_entries(zf)  # same filter+sort as the sync check
                for i, name in enumerate(names):
                    try:
                        entry_bytes = read_limited(
                            zf.open(name), settings.max_upload_file_size_bytes
                        )
                        if len(entry_bytes) > settings.max_upload_file_size_bytes:
                            skipped += 1
                            continue
                        content_type = content_type_for_entry(name)
                        verify_image_content(entry_bytes, content_type, name)
                    except Exception:
                        logger.warning(
                            "expand_archive_upload: skipping unreadable zip entry "
                            "%r in archive_id=%s",
                            name,
                            archive_id,
                        )
                        skipped += 1
                        continue
                    _create_card_from_bytes(
                        db, archive, entry_bytes, content_type, name.rsplit("/", 1)[-1], i
                    )
                    created += 1
        else:  # pdf
            pdf = pypdfium2.PdfDocument(data)
            for i in range(len(pdf)):
                try:
                    page = pdf[i]
                    width_pt, height_pt = page.get_size()
                    scale = settings.pdf_render_dpi / 72
                    # Clamp BEFORE rendering — the raster buffer is only
                    # allocated once scale is already bounded, so a
                    # maliciously huge page's pixel size never gets a chance
                    # to spike worker memory.
                    longest_edge_pt = max(width_pt, height_pt)
                    if longest_edge_pt * scale > settings.max_pdf_page_edge_px:
                        scale = settings.max_pdf_page_edge_px / longest_edge_pt
                    bitmap = page.render(scale=scale)
                    pil_image = bitmap.to_pil()
                    try:
                        buf = io.BytesIO()
                        pil_image.save(buf, format="JPEG", quality=90)
                        entry_bytes = buf.getvalue()
                    finally:
                        pil_image.close()  # don't keep up to 200 rasters resident
                    if len(entry_bytes) > settings.max_upload_file_size_bytes:
                        skipped += 1
                        continue
                except Exception:
                    logger.warning(
                        "expand_archive_upload: skipping unrenderable PDF page %d "
                        "in archive_id=%s",
                        i + 1,
                        archive_id,
                    )
                    skipped += 1
                    continue
                page_name = f"{archive.original_filename or 'card'} — page {i + 1}"
                _create_card_from_bytes(db, archive, entry_bytes, "image/jpeg", page_name, i)
                created += 1

        if created == 0:
            archive.status = "failed"
            archive.error_message = "No valid card images could be extracted"
        elif skipped > 0:
            archive.status = "completed_with_errors"
            archive.error_message = (
                f"{skipped} entr{'y' if skipped == 1 else 'ies'} could not be read"
            )
        else:
            archive.status = "completed"
        db.commit()
        storage_service.delete_file(archive.storage_key)  # best-effort, never raises
    except Exception as exc:
        db.rollback()
        archive = db.get(ArchiveUpload, uuid.UUID(archive_id))
        if archive is not None:
            archive.status = "failed"
            archive.error_message = str(exc)[:500]
            db.commit()
        logger.exception("expand_archive_upload failed for archive_id=%s", archive_id)
    finally:
        db.close()


def _create_card_from_bytes(
    db, archive: ArchiveUpload, data: bytes, content_type: str, original_filename: str, batch_sequence: int
) -> None:
    card_id = uuid.uuid4()
    ext = "." + content_type.split("/")[-1]
    key = f"cards/{archive.user_id}/{card_id}{ext}"
    storage_service.upload_file(key, data, content_type)
    card = VisitingCard(
        card_id=card_id,
        user_id=archive.user_id,
        exhibition_id=archive.exhibition_id,
        original_filename=original_filename,
        image_url=key,
        status="new",
        upload_batch_id=archive.archive_id,
        batch_sequence=batch_sequence,
    )
    db.add(card)
    db.commit()
