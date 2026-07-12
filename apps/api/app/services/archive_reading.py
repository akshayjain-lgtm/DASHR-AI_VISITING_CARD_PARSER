import zipfile

from app.core.config import settings
from app.services.exceptions import BatchTooLargeError, UnsupportedFileTypeError

# Maps a zip entry's extension to the content-type card_service's image
# validation expects. Keyed off the entry's *name* only — never a guarantee
# on its own, the caller still runs verify_image_content on the actual bytes
# before treating an entry as a real card image.
ARCHIVE_IMAGE_EXTENSIONS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


def _looks_like_image_entry(name: str) -> bool:
    if name.endswith("/"):
        return False
    basename = name.rsplit("/", 1)[-1]
    if not basename or basename.startswith(".") or name.startswith("__MACOSX/"):
        return False
    if "." not in basename:
        return False
    ext = "." + basename.rsplit(".", 1)[-1].lower()
    return ext in ARCHIVE_IMAGE_EXTENSIONS


def content_type_for_entry(name: str) -> str:
    basename = name.rsplit("/", 1)[-1]
    ext = "." + basename.rsplit(".", 1)[-1].lower()
    return ARCHIVE_IMAGE_EXTENSIONS[ext]


def list_zip_image_entries(zf: zipfile.ZipFile) -> list[str]:
    """Raw entry count is checked BEFORE filtering — zipfile has no built-in
    zip-bomb protection, and enumerating a maliciously entry-heavy zip's
    central directory is itself costly even at a tiny file size (the check
    below only reads directory metadata, never decompresses entry bodies).
    Filtered entries are sorted by filename — zip central-directory order is
    NOT guaranteed to match capture order — so batch_sequence lines up with
    capture order for the back-of-card merge logic in extraction_service,
    which depends on batch_sequence being sequential within a batch."""
    if len(zf.infolist()) > settings.max_archive_raw_entry_count:
        raise BatchTooLargeError(
            f"Zip contains too many entries (max {settings.max_archive_raw_entry_count})"
        )
    return sorted(n for n in zf.namelist() if _looks_like_image_entry(n))


def sniff_container_type(data: bytes) -> str:
    """Trusts actual bytes over the client-declared Content-Type header —
    zip/pdf Content-Type strings vary across browsers/OS (e.g. zip is sent
    as application/zip, application/x-zip-compressed, or even
    application/octet-stream depending on the client), so the header is only
    ever used as a cheap pre-filter, never the authoritative check."""
    if data.startswith(b"PK\x03\x04"):
        return "zip"
    if data.startswith(b"%PDF-"):
        return "pdf"
    raise UnsupportedFileTypeError("File is not a valid ZIP or PDF")
