"""
Tests for the `12-archive-upload` feature (spec:
`.claude/specs/12-archive-upload.md`).

These tests are written directly against the spec's documented contract for
`POST /archive-uploads`, `GET /archive-uploads/{archive_id}`, and the
`expand_archive_upload` Celery task — not against the implementation of
`services/archive_upload_service.py` or `workers/archive_processing.py`. The
spec's "Rules for implementation" and "Files to create" sections were read
only to identify real module/attribute paths to import and monkeypatch (e.g.
`app.services.archive_upload_service.expand_archive_upload.delay`,
`app.services.storage_service.upload_file`), never to infer what a test
should assert.

A handful of hardening behaviors that landed alongside the original spec
(read from the current state of `app/workers/archive_processing.py` and
`app/services/archive_upload_service.py`, per explicit task instruction that
these are now part of the intended contract even though not spelled out in
the spec's prose) are covered explicitly, each test docstring says so:
  - a per-entry (zip) or per-page (pdf) creation failure counts as
    `skipped`, not an archive-level failure — some good + some bad entries
    yields `completed_with_errors`, not `failed`, and the good cards exist
  - an unexpected archive-level failure sets a generic, client-safe
    `error_message` — never raw exception text
  - the archive's raw storage object is deleted (best-effort) after both
    the success and the failure path

Mocking / real-infra strategy (mirrors `test_04_visiting_card_bulk_upload.py`
and `test_09_bulk_select_parse_enrich.py`, and follows this suite's task
instruction to check those files first): this repo's established convention
is *real* local infra over mocks — happy-path tests exercise the real
`storage_service` (a real boto3 client against the local `minio` service from
`docker-compose.yml`), not a mock, and a `monkeypatch`-based
`_forbid_storage_upload` guard is used on every *rejected* request to assert
storage is never touched, exactly like `test_04`'s identically-named helper.
No test ever talks to a real Celery broker: `expand_archive_upload.delay(...)`
is exercised by monkeypatching `.delay` to record invocations (never a live
Redis broker) — the exact pattern `test_04`/`test_09` already use for
`process_card.delay`/`enrich_company_task.delay` — and the
`expand_archive_upload` task body itself is called directly as a plain
function against a real archive row + real storage object, per the project's
rule to test Celery task logic independent of the broker. The one test that
drives an archive-created card through `process_card` mocks
`vision_client.extract_card_fields` exactly as `test_05`/`test_09` do — the
sole external boundary that pipeline itself has.

Judgment calls made in the absence of explicit spec text (documented inline
at point of use too):
  1. **Empty PDF (0 pages)** is not tested — Pillow's PDF writer requires at
     least one image to produce a document at all, so there's no
     straightforward way to construct a *genuinely valid* zero-page PDF
     fixture. The zip "no readable images" case already covers the
     `EmptyBatchError` contract for the empty/no-entries case.
  2. **Per-page (not just per-entry) corruption for PDFs** is not tested —
     doing so would require corrupting exactly one page's raster stream
     inside an otherwise well-formed multi-page PDF without any
     PDF-authoring library in this repo's dependencies, which would mean
     reverse-engineering pypdfium2/PDF internals rather than testing a
     documented contract. The zip per-entry-skip test already covers the
     "some entries fail, archive still completes with errors" hardening
     behavior for the sibling container type.
  3. **Admin-sees-org-members visibility** for `GET /archive-uploads/{id}` is
     not tested, for the same reason `test_04` documents and skips it: no
     conftest helper currently exists to put a user through an org-invite/
     admin-role setup, and fabricating one via direct ORM manipulation would
     encode assumptions about an unimplemented feature. See the trailing
     `pytest.mark.skip` placeholder.
  4. **Batch-sequence exact values** are only asserted as "distinct,
     non-null integers assigned in sorted-filename order" for the happy path
     (where the spec text itself promises "sorted for deterministic
     batch_sequence"), never as exact numeric values when some entries are
     skipped — the precise numbering scheme when skips occur isn't
     spelled out in the spec, and asserting one would mean reverse-engineering
     the implementation.
"""

from __future__ import annotations

import io
import os
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.core.config import settings
from app.main import app as fastapi_app
from app.models.archive_upload import ArchiveUpload
from app.models.visiting_card import VisitingCard
from app.services import storage_service
from app.workers.archive_processing import expand_archive_upload
from app.workers.card_processing import process_card
from conftest import create_verified_user

# --------------------------------------------------------------------------
# Image / archive byte helpers — real, decodable content per this repo's
# convention (never placeholder/random bytes for a "valid" fixture).
# --------------------------------------------------------------------------


def _image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (20, 20), color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format=fmt)
    return buf.getvalue()


def _zip_bytes(entries: list[tuple[str, bytes]], compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def _zip_of_images(count: int, prefix: str = "card") -> bytes:
    """Zero-padded, numerically-sortable filenames so alphabetical sort
    (which `list_zip_image_entries` uses for deterministic batch_sequence
    per the spec) matches numeric/capture order."""
    entries = [(f"{prefix}_{i:03d}.jpg", _image_bytes(color="red")) for i in range(count)]
    return _zip_bytes(entries)


def _pdf_bytes(pages: int, size: tuple[int, int] = (50, 50)) -> bytes:
    """A real, pypdfium2-readable multi-page PDF built with Pillow's own PDF
    writer (no extra dependency needed) — never placeholder bytes."""
    images = [Image.new("RGB", size, color=("red" if i % 2 == 0 else "blue")) for i in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, format="PDF", save_all=True, append_images=images[1:])
    return buf.getvalue()


def _oversized_zip_bytes(min_bytes: int) -> bytes:
    """A genuine, valid ZIP_STORED archive (no compression, so its own byte
    size is a deterministic, easily-grown function of its single entry's
    size) containing one real, Pillow-decodable PNG (compress_level=0),
    grown until the whole archive's byte size exceeds `min_bytes` — mirrors
    `test_04_visiting_card_bulk_upload.py::_oversized_png_bytes`."""
    side = 64
    while True:
        img_buf = io.BytesIO()
        Image.new("RGB", (side, side), color="blue").save(img_buf, format="PNG", compress_level=0)
        archive = _zip_bytes([("huge.png", img_buf.getvalue())], compression=zipfile.ZIP_STORED)
        if len(archive) > min_bytes:
            return archive
        side = int(side * 1.5) + 1


CORRUPT_ZIP_BYTES = b"PK\x03\x04" + os.urandom(64)  # zip magic bytes, garbage after
CORRUPT_PDF_BYTES = b"%PDF-1.4\n" + os.urandom(64)  # pdf magic bytes, garbage after
NON_IMAGE_BYTES = b"this is definitely not image bytes, just plain text content"


# --------------------------------------------------------------------------
# Auth / upload helpers.
# --------------------------------------------------------------------------


def _login(client: TestClient, user: dict) -> None:
    resp = client.post("/auth/login", json={"email": user["email"], "password": user["password"]})
    assert resp.status_code == 200, resp.text


def _authenticated_user(client: TestClient, fake_otp_provider, **overrides) -> dict:
    user = create_verified_user(client, fake_otp_provider, **overrides)
    _login(client, user)
    return user


def _upload_archive(
    client: TestClient,
    filename: str,
    data: bytes,
    content_type: str,
    exhibition_id: str | None = None,
):
    form = {}
    if exhibition_id is not None:
        form["exhibition_id"] = exhibition_id
    return client.post(
        "/archive-uploads",
        data=form,
        files={"file": (filename, data, content_type)},
    )


def _forbid_storage_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails loudly if a rejected archive-upload request touches storage at
    all. Spec: cheap structural validation happens synchronously '...all
    before any Celery work is enqueued' — this makes 'and before any
    storage write' an enforced assertion, matching test_04's identically
    named helper and rationale."""

    def _boom(*args, **kwargs):
        raise AssertionError(
            "storage_service.upload_file must never be called for an archive-upload "
            "request that is ultimately rejected with 400/404/422"
        )

    monkeypatch.setattr("app.services.storage_service.upload_file", _boom)


def _patch_expand_delay(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: list[str] = []
    monkeypatch.setattr(
        "app.services.archive_upload_service.expand_archive_upload.delay",
        lambda archive_id: captured.append(archive_id),
    )
    return captured


def _archive_rows_for_user(db_session, user_id: str) -> list[ArchiveUpload]:
    return (
        db_session.execute(select(ArchiveUpload).where(ArchiveUpload.user_id == uuid.UUID(user_id)))
        .scalars()
        .all()
    )


def _cards_for_archive(db_session, archive_id: str) -> list[VisitingCard]:
    return (
        db_session.execute(
            select(VisitingCard).where(VisitingCard.upload_batch_id == uuid.UUID(archive_id))
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------------------
# Vision-model mocking for the "downstream pipeline unmodified" test —
# copied from test_09_bulk_select_parse_enrich.py's identically-named
# helpers, the established pattern for exercising process_card without a
# real vision API call.
# --------------------------------------------------------------------------


def _patch_vision(monkeypatch: pytest.MonkeyPatch, *responses) -> None:
    queue = list(responses)

    def _fake(image_bytes: bytes, media_type: str):
        if not queue:
            raise AssertionError("extract_card_fields called more times than this test scripted")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("app.services.vision_client.extract_card_fields", _fake)


def _fields(*, full_name: str | None = "Extracted Contact", company_name: str | None = None) -> dict:
    return {
        "is_back_of_card": False,
        "full_name": full_name,
        "job_title": None,
        "company_name": company_name,
        "website": None,
        "address": None,
        "products_offered": None,
        "special_remark": None,
        "raw_ocr_text": "verbatim card text",
        "emails": [],
        "phones": [],
        "gst_number": None,
    }


def _unique_company_name(label: str) -> str:
    return f"{label} {uuid.uuid4().hex[:10]} Pvt Ltd"


# ==========================================================================
# 1. Auth guard — both endpoints require a session.
# ==========================================================================


@pytest.mark.parametrize(
    "method, path",
    [
        ("post", "/archive-uploads"),
        ("get", f"/archive-uploads/{uuid.uuid4()}"),
    ],
)
def test_archive_endpoints_without_session_return_401(client, method: str, path: str):
    resp = getattr(client, method)(path)
    assert resp.status_code == 401, (
        f"{method.upper()} {path} without a session must return 401, got "
        f"{resp.status_code}: {resp.text}"
    )


# ==========================================================================
# 2. POST /archive-uploads — happy path (zip / pdf).
# ==========================================================================


def test_create_archive_upload_valid_zip_returns_201_processing_and_enqueues_task(
    client, fake_otp_provider, db_session, monkeypatch
):
    """DoD: 'POST /archive-uploads with a valid ZIP of card images returns
    201 with status="processing"' and enqueues expand_archive_upload after
    the row + storage object are created."""
    user = _authenticated_user(client, fake_otp_provider)
    captured = _patch_expand_delay(monkeypatch)

    zip_bytes = _zip_of_images(3)
    resp = _upload_archive(client, "cards.zip", zip_bytes, "application/zip")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body.keys()) == {
        "archive_id",
        "exhibition_id",
        "original_filename",
        "container_type",
        "status",
        "error_message",
        "created_at",
    }, "ArchiveUploadOut must expose exactly the documented public fields (no storage_key/user_id leak)"
    assert body["status"] == "processing"
    assert body["container_type"] == "zip"
    assert body["original_filename"] == "cards.zip"
    assert body["exhibition_id"] is None
    assert body["error_message"] is None
    assert body.get("archive_id"), "must return a generated archive_id"

    archive_id = body["archive_id"]
    assert captured == [archive_id], (
        f"expand_archive_upload.delay must be enqueued exactly once with the new archive_id, "
        f"got {captured!r}"
    )

    rows = _archive_rows_for_user(db_session, user["user_id"])
    assert len(rows) == 1, "exactly one archive_uploads row must be created"
    row = rows[0]
    assert str(row.archive_id) == archive_id
    assert row.container_type == "zip"
    assert row.status == "processing"
    assert row.storage_key, "a storage_key must be persisted for the uploaded raw archive"


def test_create_archive_upload_valid_pdf_returns_201_processing_and_enqueues_task(
    client, fake_otp_provider, db_session, monkeypatch
):
    """DoD: 'POST /archive-uploads with a valid multi-page PDF returns 201'
    (same pattern as the zip case)."""
    user = _authenticated_user(client, fake_otp_provider)
    captured = _patch_expand_delay(monkeypatch)

    pdf_bytes = _pdf_bytes(pages=3)
    resp = _upload_archive(client, "cards.pdf", pdf_bytes, "application/pdf")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "processing"
    assert body["container_type"] == "pdf"
    assert body["original_filename"] == "cards.pdf"

    archive_id = body["archive_id"]
    assert captured == [archive_id]

    rows = _archive_rows_for_user(db_session, user["user_id"])
    assert len(rows) == 1
    assert rows[0].container_type == "pdf"


def test_create_archive_upload_attaches_to_own_exhibition_id(client, fake_otp_provider, monkeypatch):
    _authenticated_user(client, fake_otp_provider)
    _patch_expand_delay(monkeypatch)
    exhibition = client.post(
        "/exhibitions", json={"name": "Archive Attach Show", "start_date": "2026-05-01"}
    )
    assert exhibition.status_code == 201, exhibition.text
    exhibition_id = exhibition.json()["exhibition_id"]

    resp = _upload_archive(
        client, "cards.zip", _zip_of_images(1), "application/zip", exhibition_id=exhibition_id
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["exhibition_id"] == exhibition_id

    archive_id = resp.json()["archive_id"]
    fetched = client.get(f"/archive-uploads/{archive_id}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["exhibition_id"] == exhibition_id


def test_create_archive_upload_sniffs_container_type_from_bytes_ignoring_lying_content_type_header(
    client, fake_otp_provider, monkeypatch
):
    """Rule: 'Container type is determined from the file's actual magic
    bytes ... never trusted from the client-declared Content-Type header
    alone.' A real zip labeled with a Content-Type browsers commonly send
    for zips (per archive_reading.py's own docstring) must still be
    recognized as a zip."""
    _authenticated_user(client, fake_otp_provider)
    _patch_expand_delay(monkeypatch)

    resp = _upload_archive(
        client, "cards.zip", _zip_of_images(1), content_type="application/octet-stream"
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["container_type"] == "zip", (
        "container type must be sniffed from magic bytes, not trusted from a generic "
        "octet-stream Content-Type header"
    )


# ==========================================================================
# 3. POST /archive-uploads — structural validation rejections, all before
#    any Celery enqueue or storage write.
# ==========================================================================


def test_create_archive_upload_with_other_users_exhibition_id_returns_404_and_touches_nothing(
    client, fake_otp_provider, db_session, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        theirs = other_client.post(
            "/exhibitions", json={"name": "Not Yours", "start_date": "2026-05-01"}
        )
        assert theirs.status_code == 201, theirs.text
        their_exhibition_id = theirs.json()["exhibition_id"]

    _forbid_storage_upload(monkeypatch)
    captured = _patch_expand_delay(monkeypatch)

    resp = _upload_archive(
        client, "cards.zip", _zip_of_images(1), "application/zip", exhibition_id=their_exhibition_id
    )

    assert resp.status_code == 404, resp.text
    assert captured == [], "no Celery task may be enqueued when exhibition_id isn't visible"
    assert _archive_rows_for_user(db_session, user["user_id"]) == [], (
        "no archive_uploads row may be created when exhibition_id doesn't belong to the caller"
    )


def test_create_archive_upload_with_malformed_exhibition_id_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)

    resp = _upload_archive(
        client, "cards.zip", _zip_of_images(1), "application/zip", exhibition_id="not-a-uuid"
    )

    assert resp.status_code == 422, resp.text


def test_create_archive_upload_corrupt_zip_returns_400_and_touches_nothing(
    client, fake_otp_provider, db_session, monkeypatch
):
    """DoD: 'A corrupt ... archive ... is rejected with 400 before any
    Celery task is enqueued.'"""
    user = _authenticated_user(client, fake_otp_provider)
    _forbid_storage_upload(monkeypatch)
    captured = _patch_expand_delay(monkeypatch)

    resp = _upload_archive(client, "corrupt.zip", CORRUPT_ZIP_BYTES, "application/zip")

    assert resp.status_code == 400, resp.text
    assert captured == []
    assert _archive_rows_for_user(db_session, user["user_id"]) == []


def test_create_archive_upload_corrupt_pdf_returns_400_and_touches_nothing(
    client, fake_otp_provider, db_session, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    _forbid_storage_upload(monkeypatch)
    captured = _patch_expand_delay(monkeypatch)

    resp = _upload_archive(client, "corrupt.pdf", CORRUPT_PDF_BYTES, "application/pdf")

    assert resp.status_code == 400, resp.text
    assert captured == []
    assert _archive_rows_for_user(db_session, user["user_id"]) == []


def test_create_archive_upload_neither_zip_nor_pdf_returns_400_and_touches_nothing(
    client, fake_otp_provider, db_session, monkeypatch
):
    """A genuine JPEG whose Content-Type header lies and claims it's a zip
    must still be rejected — proves sniffing wins over the client-declared
    header in the rejection direction too, not just the acceptance
    direction covered by the octet-stream test above."""
    user = _authenticated_user(client, fake_otp_provider)
    _forbid_storage_upload(monkeypatch)
    captured = _patch_expand_delay(monkeypatch)

    resp = _upload_archive(client, "not-an-archive.jpg", _image_bytes(), "application/zip")

    assert resp.status_code == 400, resp.text
    assert captured == []
    assert _archive_rows_for_user(db_session, user["user_id"]) == []


def test_create_archive_upload_empty_zip_returns_400_and_touches_nothing(
    client, fake_otp_provider, db_session, monkeypatch
):
    """DoD: 'An ... empty ... archive ... is rejected with 400' — a zip with
    entries but none of them image-like."""
    user = _authenticated_user(client, fake_otp_provider)
    _forbid_storage_upload(monkeypatch)
    captured = _patch_expand_delay(monkeypatch)

    zip_bytes = _zip_bytes([("readme.txt", b"no images in here")])
    resp = _upload_archive(client, "empty.zip", zip_bytes, "application/zip")

    assert resp.status_code == 400, resp.text
    assert captured == []
    assert _archive_rows_for_user(db_session, user["user_id"]) == []


def test_create_archive_upload_oversized_file_returns_400_and_touches_nothing(
    client, fake_otp_provider, db_session, monkeypatch
):
    """DoD: 'An ... oversized ... archive ... is rejected with 400.'"""
    user = _authenticated_user(client, fake_otp_provider)
    monkeypatch.setattr(settings, "max_archive_file_size_mb", 1)
    oversized = _oversized_zip_bytes(settings.max_archive_file_size_bytes)
    assert len(oversized) > settings.max_archive_file_size_bytes, (
        "fixture bug: oversized archive must actually exceed the configured limit"
    )
    _forbid_storage_upload(monkeypatch)
    captured = _patch_expand_delay(monkeypatch)

    resp = _upload_archive(client, "huge.zip", oversized, "application/zip")

    assert resp.status_code == 400, resp.text
    assert captured == []
    assert _archive_rows_for_user(db_session, user["user_id"]) == []


def test_create_archive_upload_zip_over_max_raw_entry_count_returns_400_and_touches_nothing(
    client, fake_otp_provider, db_session, monkeypatch
):
    """DoD: '... one exceeding the max entry ... count is rejected with 400.'
    Rule: raw entry count is checked BEFORE image-name filtering, so
    non-image entries alone can trip this."""
    user = _authenticated_user(client, fake_otp_provider)
    monkeypatch.setattr(settings, "max_archive_raw_entry_count", 3)
    _forbid_storage_upload(monkeypatch)
    captured = _patch_expand_delay(monkeypatch)

    zip_bytes = _zip_bytes([(f"f{i}.txt", b"x") for i in range(5)])
    resp = _upload_archive(client, "too_many_entries.zip", zip_bytes, "application/zip")

    assert resp.status_code == 400, resp.text
    assert captured == []
    assert _archive_rows_for_user(db_session, user["user_id"]) == []


def test_create_archive_upload_zip_over_max_bulk_upload_files_returns_400_and_touches_nothing(
    client, fake_otp_provider, db_session, monkeypatch
):
    """DoD: '... one exceeding the max ... page count is rejected with 400'
    — the zip-image-entry-count analog."""
    user = _authenticated_user(client, fake_otp_provider)
    monkeypatch.setattr(settings, "max_bulk_upload_files", 2)
    _forbid_storage_upload(monkeypatch)
    captured = _patch_expand_delay(monkeypatch)

    zip_bytes = _zip_of_images(3)
    resp = _upload_archive(client, "too_many_images.zip", zip_bytes, "application/zip")

    assert resp.status_code == 400, resp.text
    assert captured == []
    assert _archive_rows_for_user(db_session, user["user_id"]) == []


def test_create_archive_upload_pdf_over_max_bulk_upload_files_returns_400_and_touches_nothing(
    client, fake_otp_provider, db_session, monkeypatch
):
    """DoD: '... one exceeding the max entry/page count is rejected with
    400' — the PDF page-count case."""
    user = _authenticated_user(client, fake_otp_provider)
    monkeypatch.setattr(settings, "max_bulk_upload_files", 2)
    _forbid_storage_upload(monkeypatch)
    captured = _patch_expand_delay(monkeypatch)

    pdf_bytes = _pdf_bytes(pages=3)
    resp = _upload_archive(client, "too_many_pages.pdf", pdf_bytes, "application/pdf")

    assert resp.status_code == 400, resp.text
    assert captured == []
    assert _archive_rows_for_user(db_session, user["user_id"]) == []


# ==========================================================================
# 4. GET /archive-uploads/{id} — status transitions + tenant isolation.
# ==========================================================================


def test_get_archive_upload_reflects_transition_from_processing_to_completed(
    client, fake_otp_provider, monkeypatch
):
    """DoD: 'GET /archive-uploads/{id} reflects processing -> completed/
    completed_with_errors/failed as the Celery task runs.' The task is run
    directly (no broker) to simulate that progression deterministically."""
    _authenticated_user(client, fake_otp_provider)
    _patch_expand_delay(monkeypatch)  # prevent the real Celery enqueue

    resp = _upload_archive(client, "cards.zip", _zip_of_images(2), "application/zip")
    assert resp.status_code == 201, resp.text
    archive_id = resp.json()["archive_id"]

    before = client.get(f"/archive-uploads/{archive_id}")
    assert before.status_code == 200, before.text
    assert before.json()["status"] == "processing"

    expand_archive_upload(archive_id)  # direct call — simulates the worker running

    after = client.get(f"/archive-uploads/{archive_id}")
    assert after.status_code == 200, after.text
    assert after.json()["status"] == "completed"
    assert after.json()["error_message"] is None


def test_get_archive_upload_for_other_users_archive_returns_404(client, fake_otp_provider, monkeypatch):
    """DoD: '... is not visible to a different user/org' — same
    scope_to_visible_users rule as GET /cards. A wrong-org GET must be a
    404, never a 403 leaking existence, exactly like get_visible_exhibition/
    get_visible_card elsewhere in this codebase."""
    with TestClient(fastapi_app) as owner_client:
        _authenticated_user(owner_client, fake_otp_provider)
        _patch_expand_delay(monkeypatch)
        created = _upload_archive(owner_client, "cards.zip", _zip_of_images(1), "application/zip")
        assert created.status_code == 201, created.text
        archive_id = created.json()["archive_id"]

    _authenticated_user(client, fake_otp_provider)
    resp = client.get(f"/archive-uploads/{archive_id}")

    assert resp.status_code == 404, (
        f"a different user must never be able to see another user's archive upload, "
        f"got {resp.status_code}: {resp.text}"
    )


def test_get_archive_upload_nonexistent_id_returns_404(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)

    resp = client.get(f"/archive-uploads/{uuid.uuid4()}")

    assert resp.status_code == 404, resp.text


# ==========================================================================
# 5. `expand_archive_upload` Celery task — tested directly, no live broker.
# ==========================================================================


def _create_processing_archive(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, filename: str, data: bytes, content_type: str
) -> str:
    """Seeds a real archive_uploads row + real storage object via the real
    HTTP endpoint (delay mocked so the real Celery broker is never touched),
    returning the archive_id for a direct expand_archive_upload(archive_id)
    call — mirrors how test_09 drives process_card via a real upload then a
    direct task call."""
    _patch_expand_delay(monkeypatch)
    resp = _upload_archive(client, filename, data, content_type)
    assert resp.status_code == 201, resp.text
    return resp.json()["archive_id"]


def test_expand_archive_upload_zip_creates_one_card_per_image_and_marks_completed(
    client, fake_otp_provider, db_session, monkeypatch
):
    """DoD: '... every image in the archive eventually becomes its own
    VisitingCard with status="new"'."""
    _authenticated_user(client, fake_otp_provider)
    archive_id = _create_processing_archive(
        client, monkeypatch, "cards.zip", _zip_of_images(3), "application/zip"
    )

    expand_archive_upload(archive_id)

    db_session.expire_all()
    archive = db_session.get(ArchiveUpload, uuid.UUID(archive_id))
    assert archive.status == "completed"
    assert archive.error_message is None

    cards = _cards_for_archive(db_session, archive_id)
    assert len(cards) == 3, "one VisitingCard must be created per image entry in the zip"
    for card in cards:
        assert card.status == "new"
        assert card.user_id == archive.user_id
        assert card.upload_batch_id == archive.archive_id
        assert card.batch_sequence is not None

    # Spec: entries are sorted for deterministic batch_sequence.
    sequences = sorted(card.batch_sequence for card in cards)
    assert sequences == [0, 1, 2], (
        "with zero skipped entries, batch_sequence must be assigned contiguously in "
        f"sorted-filename order, got {sequences!r}"
    )


def test_expand_archive_upload_pdf_creates_one_card_per_page_and_marks_completed(
    client, fake_otp_provider, db_session, monkeypatch
):
    """DoD: '... every page eventually becomes its own VisitingCard.'"""
    _authenticated_user(client, fake_otp_provider)
    archive_id = _create_processing_archive(
        client, monkeypatch, "cards.pdf", _pdf_bytes(pages=3), "application/pdf"
    )

    expand_archive_upload(archive_id)

    db_session.expire_all()
    archive = db_session.get(ArchiveUpload, uuid.UUID(archive_id))
    assert archive.status == "completed"

    cards = _cards_for_archive(db_session, archive_id)
    assert len(cards) == 3, "one VisitingCard must be created per PDF page"
    for card in cards:
        assert card.status == "new"
        assert card.original_filename, "each card created from a PDF page must have a filename"


def test_expand_archive_upload_attaches_created_cards_to_the_archives_exhibition(
    client, fake_otp_provider, db_session, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    exhibition = client.post(
        "/exhibitions", json={"name": "Expand Attach Show", "start_date": "2026-06-01"}
    )
    assert exhibition.status_code == 201, exhibition.text
    exhibition_id = exhibition.json()["exhibition_id"]

    _patch_expand_delay(monkeypatch)
    resp = _upload_archive(
        client, "cards.zip", _zip_of_images(2), "application/zip", exhibition_id=exhibition_id
    )
    assert resp.status_code == 201, resp.text
    archive_id = resp.json()["archive_id"]

    expand_archive_upload(archive_id)

    db_session.expire_all()
    cards = _cards_for_archive(db_session, archive_id)
    assert len(cards) == 2
    for card in cards:
        assert str(card.exhibition_id) == exhibition_id, (
            "cards created from an archive attached to an exhibition must inherit that "
            "exhibition_id"
        )


def test_expand_archive_upload_partial_bad_entries_skips_them_and_marks_completed_with_errors(
    client, fake_otp_provider, db_session, monkeypatch
):
    """Hardening fix (not in original spec prose, confirmed as intended
    contract by reading the current worker): a per-entry creation failure is
    counted as `skipped`, not an archive-level failure — with 2 good entries
    and 1 unreadable one, the archive ends up completed_with_errors (never
    failed), and the 2 good cards genuinely exist."""
    _authenticated_user(client, fake_otp_provider)

    zip_bytes = _zip_bytes(
        [
            ("good1.jpg", _image_bytes(color="red")),
            ("good2.jpg", _image_bytes(color="blue")),
            # Valid image extension, garbage content — passes the sync
            # name-based filter, fails verify_image_content in the worker.
            ("bad.jpg", NON_IMAGE_BYTES),
        ]
    )
    archive_id = _create_processing_archive(client, monkeypatch, "mixed.zip", zip_bytes, "application/zip")

    expand_archive_upload(archive_id)

    db_session.expire_all()
    archive = db_session.get(ArchiveUpload, uuid.UUID(archive_id))
    assert archive.status == "completed_with_errors", (
        "some-good-some-bad entries must yield completed_with_errors, never failed"
    )
    assert archive.error_message, "a completed_with_errors archive must carry an explanatory message"

    cards = _cards_for_archive(db_session, archive_id)
    assert len(cards) == 2, "the two good entries must still become real VisitingCard rows"
    filenames = {card.original_filename for card in cards}
    assert filenames == {"good1.jpg", "good2.jpg"}, (
        "only the two decodable entries may produce cards; the corrupt entry must be skipped, "
        f"got {filenames!r}"
    )


def test_expand_archive_upload_all_entries_unreadable_creates_nothing_and_marks_failed(
    client, fake_otp_provider, db_session, monkeypatch
):
    """Hardening fix: zero cards created (every entry unreadable) is the
    only case that marks the archive `failed`."""
    _authenticated_user(client, fake_otp_provider)

    zip_bytes = _zip_bytes(
        [
            ("bad1.jpg", NON_IMAGE_BYTES),
            ("bad2.png", NON_IMAGE_BYTES),
        ]
    )
    archive_id = _create_processing_archive(
        client, monkeypatch, "all_bad.zip", zip_bytes, "application/zip"
    )

    delete_calls: list[str] = []
    original_delete = storage_service.delete_file

    def _capture_and_delete(key: str) -> None:
        delete_calls.append(key)
        original_delete(key)

    monkeypatch.setattr("app.services.storage_service.delete_file", _capture_and_delete)

    expand_archive_upload(archive_id)

    db_session.expire_all()
    archive = db_session.get(ArchiveUpload, uuid.UUID(archive_id))
    assert archive.status == "failed", "zero cards created from a non-empty archive must mark it failed"
    assert archive.error_message, "a failed archive must carry an explanatory message"
    assert _cards_for_archive(db_session, archive_id) == []
    assert delete_calls == [archive.storage_key], (
        "the raw archive object must still be deleted (best-effort) on the failed path"
    )


def test_expand_archive_upload_unexpected_failure_sets_generic_client_safe_error_message(
    client, fake_otp_provider, db_session, monkeypatch
):
    """Hardening fix: an unexpected archive-level failure (container can't
    even be downloaded/opened) sets a generic, client-safe error_message —
    raw exception text (which could leak internal paths/bucket names) must
    never reach it."""
    _authenticated_user(client, fake_otp_provider)
    archive_id = _create_processing_archive(
        client, monkeypatch, "cards.zip", _zip_of_images(1), "application/zip"
    )
    archive_before = db_session.get(ArchiveUpload, uuid.UUID(archive_id))
    storage_key = archive_before.storage_key

    secret_detail = "s3://internal-bucket/should-never-leak-to-a-client dsn=postgres://leak"

    def _boom(key: str) -> bytes:
        raise RuntimeError(secret_detail)

    delete_calls: list[str] = []
    monkeypatch.setattr("app.services.storage_service.download_file", _boom)
    monkeypatch.setattr("app.services.storage_service.delete_file", lambda key: delete_calls.append(key))

    expand_archive_upload(archive_id)

    db_session.expire_all()
    archive = db_session.get(ArchiveUpload, uuid.UUID(archive_id))
    assert archive.status == "failed"
    assert archive.error_message, "an archive-level failure must still set a user-facing error_message"
    assert secret_detail not in archive.error_message, (
        "raw exception text must never leak into the client-facing error_message"
    )
    assert delete_calls == [storage_key], (
        "the raw archive object must still be deleted (best-effort) even on an unexpected "
        "archive-level failure"
    )
    assert _cards_for_archive(db_session, archive_id) == []


def test_expand_archive_upload_deletes_storage_object_after_success(
    client, fake_otp_provider, db_session, monkeypatch
):
    """Hardening fix: the archive's raw storage object is deleted
    (best-effort) after the success path too, not just on failure."""
    _authenticated_user(client, fake_otp_provider)
    archive_id = _create_processing_archive(
        client, monkeypatch, "cards.zip", _zip_of_images(1), "application/zip"
    )
    storage_key = db_session.get(ArchiveUpload, uuid.UUID(archive_id)).storage_key

    delete_calls: list[str] = []
    original_delete = storage_service.delete_file

    def _capture_and_delete(key: str) -> None:
        delete_calls.append(key)
        original_delete(key)

    monkeypatch.setattr("app.services.storage_service.delete_file", _capture_and_delete)

    expand_archive_upload(archive_id)

    db_session.expire_all()
    assert db_session.get(ArchiveUpload, uuid.UUID(archive_id)).status == "completed"
    assert delete_calls == [storage_key], (
        "the raw archive object must be deleted (best-effort) once expansion succeeds"
    )


def test_expand_archive_upload_skips_archives_not_in_processing_status(
    client, fake_otp_provider, db_session, monkeypatch
):
    """Idempotency guard: an archive that isn't (or is no longer)
    status="processing" must never be (re-)expanded — protects against a
    duplicate/redelivered task double-creating cards."""
    _authenticated_user(client, fake_otp_provider)
    archive_id = _create_processing_archive(
        client, monkeypatch, "cards.zip", _zip_of_images(1), "application/zip"
    )
    archive = db_session.get(ArchiveUpload, uuid.UUID(archive_id))
    archive.status = "completed"
    archive.error_message = None
    db_session.commit()

    expand_archive_upload(archive_id)  # must no-op

    db_session.expire_all()
    assert db_session.get(ArchiveUpload, uuid.UUID(archive_id)).status == "completed", (
        "status must remain untouched when the task no-ops on a non-'processing' archive"
    )
    assert _cards_for_archive(db_session, archive_id) == [], (
        "a non-'processing' archive must never be (re-)expanded into cards"
    )


def test_expand_archive_upload_unknown_archive_id_does_not_raise():
    """Edge case: a stale/deleted archive_id reaching a worker must not
    crash it — mirrors test_04's identical guard for process_card."""
    expand_archive_upload(str(uuid.uuid4()))  # direct call — must not raise


# ==========================================================================
# 6. Cards created from an archive are ordinary VisitingCard rows,
#    indistinguishable from directly-uploaded ones.
# ==========================================================================


def test_cards_created_from_archive_appear_in_card_list_indistinguishable_from_direct_upload(
    client, fake_otp_provider, monkeypatch
):
    """DoD: 'Cards created from an archive appear in the existing card list
    and are indistinguishable from directly-uploaded cards.'"""
    _authenticated_user(client, fake_otp_provider)

    direct = client.post(
        "/cards/bulk-upload", data={}, files=[("files", ("direct.jpg", _image_bytes(), "image/jpeg"))]
    )
    assert direct.status_code == 201, direct.text
    direct_card_id = direct.json()["cards"][0]["card_id"]

    archive_id = _create_processing_archive(
        client, monkeypatch, "cards.zip", _zip_of_images(1), "application/zip"
    )
    expand_archive_upload(archive_id)

    listing = client.get("/cards")
    assert listing.status_code == 200, listing.text
    by_id = {c["card_id"]: c for c in listing.json()}
    assert len(by_id) == 2, "both the directly-uploaded and archive-created card must be listed"

    direct_card = by_id[direct_card_id]
    archive_card = next(c for c in by_id.values() if c["card_id"] != direct_card_id)

    assert set(direct_card.keys()) == set(archive_card.keys()), (
        "an archive-created card must expose exactly the same CardOut fields as a "
        "directly-uploaded one"
    )
    assert archive_card["status"] == "new"
    assert direct_card["status"] == "new"
    assert archive_card["company_name"] is None, "a freshly-created card has no linked company yet"
    assert direct_card["company_name"] is None
    assert isinstance(archive_card["image_url"], str) and archive_card["image_url"], (
        "an archive-created card must expose a real (presigned) image_url just like a "
        "directly-uploaded one"
    )


def test_card_created_from_archive_can_be_parsed_via_process_card_like_a_direct_upload(
    client, fake_otp_provider, db_session, monkeypatch
):
    """DoD: 'indistinguishable ... for every downstream step (parse,
    enrich, score, export)' — spec's "Depends on" section: 'cards created
    from an archive are picked up by the existing Parse Cards action
    exactly like directly-uploaded cards; no changes to extraction itself.'
    Exercised by calling process_card directly (no broker), with only the
    vision boundary mocked, exactly like test_09 does for direct uploads."""
    _authenticated_user(client, fake_otp_provider)
    archive_id = _create_processing_archive(
        client, monkeypatch, "cards.zip", _zip_of_images(1), "application/zip"
    )
    expand_archive_upload(archive_id)

    db_session.expire_all()
    card = _cards_for_archive(db_session, archive_id)[0]
    assert card.status == "new", "fixture setup: the archive-created card must start status='new'"

    _patch_vision(
        monkeypatch,
        _fields(full_name="Archive Contact", company_name=_unique_company_name("Archive Sourced Co")),
    )
    process_card(str(card.card_id))

    db_session.expire_all()
    processed = db_session.get(VisitingCard, card.card_id)
    assert processed.status == "extracted", (
        "an archive-created card must flow through the existing extraction pipeline exactly "
        "like a directly-uploaded card, with no changes to extraction itself"
    )
    assert processed.full_name == "Archive Contact"
    assert processed.company_id is not None


# ==========================================================================
# Out of scope for this file (documented, not silently skipped) — mirrors
# test_04_visiting_card_bulk_upload.py's identical gap and rationale.
# ==========================================================================


@pytest.mark.skip(
    reason=(
        "Admin-sees-org-members visibility for GET /archive-uploads/{id} requires putting a "
        "user through an org + admin/member setup that no conftest helper currently supports "
        "(02-user-registration only ever produces org_id=NULL, role=NULL accounts). Per task "
        "instructions, this is documented as a deliberate gap rather than fabricated via direct "
        "ORM row manipulation that would encode assumptions about a future org-invite feature's "
        "implementation. scope_to_visible_users itself (the shared helper both GET /cards and "
        "GET /archive-uploads/{id} use) already has this same gap in test_04."
    )
)
def test_admin_sees_org_members_archive_uploads():
    pass
