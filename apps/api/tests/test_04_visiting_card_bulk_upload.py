"""
Tests for the `04-visiting-card-bulk-upload` feature (spec:
`.claude/specs/04-visiting-card-bulk-upload.md`).

These tests are written directly against the spec's documented contract, not
against the implementation of `routers/cards.py`, `routers/exhibitions.py`,
`services/card_service.py`, `services/exhibition_service.py`, or
`services/storage_service.py`:

- `POST /exhibitions` / `GET /exhibitions` — org-authenticated; the spec's
  "Depends on" section documents that `exhibitions`/`visiting_cards` have no
  `org_id` column at all (scoped by `user_id` only), with admin-sees-org-member
  visibility implemented as an API-layer join rather than a stored column.
  `02-user-registration` only ever produces `org_id=NULL, role=NULL` accounts,
  and no conftest helper exists yet to put a user through an org-invite/admin
  flow, so per the task's explicit instruction the admin-sees-org-members case
  is covered only by one `pytest.mark.skip` placeholder (see bottom of file)
  documenting *why* rather than silently omitting it — the primary, fully
  covered case is a `member`/org-less user only ever seeing their own rows.
- `POST /cards/bulk-upload` — validates content-type + size + batch file count
  BEFORE uploading or inserting anything; any failure anywhere in the batch
  rejects the *whole* request with `400` and creates zero rows (no partial
  batches). An `exhibition_id` that doesn't belong to the caller returns `404`
  and also creates zero rows. On success, returns `201` with a `card_id` per
  uploaded file, `status="new"`, and `exhibition_id` echoed back (or `null`).
- `GET /cards` — same user/admin visibility rule as `GET /exhibitions`;
  filters on `exhibition_id`/`status`; returns a presigned `image_url`
  computed at *read* time, never the persisted storage key.

Judgment calls made in the absence of explicit spec text (documented inline
at point of use too):
  1. **Empty batch** (`files` field entirely absent/empty): the spec never
     enumerates this case in its Definition of Done. Since `files` is
     documented as required ("one or more image files"), this is treated as
     *some* form of client-side rejection — the test accepts either `422`
     (FastAPI's own required-field validation) or `400` (a service-level
     `BatchTooLargeError`-style check with a lower bound), without insisting
     on one specific code.
  2. **Oversized-file bytes**: rather than shipping a real multi-megabyte
     photo fixture, the "file over max size" test builds a *genuinely valid,
     Pillow-decodable* PNG using `compress_level=0` (no deflate compression),
     so its on-disk byte size is a deterministic, computable function of
     pixel count. The helper grows the image until it exceeds the configured
     `settings.max_upload_file_size_bytes`, guaranteeing a real decodable
     image whose size only genuinely exceeds the limit — not a shortcut
     around the Pillow `img.verify()` content check the router now performs.
  3. **`image_url` reachability**: per task instructions, this suite does not
     attempt to fetch the presigned URL over the network (MinIO reachability
     from the test run isn't guaranteed); it only asserts the URL is a
     non-empty, well-formed absolute HTTP(S) URL.
  4. **FK-cascade isolation**: `apps/api/migrations/versions/0001_initial_schema.py`
     defines `exhibitions.user_id` and `visiting_cards.user_id` (and
     transitively `card_phones`/`card_emails` via `visiting_cards.card_id`)
     as foreign keys into `users.user_id`. Postgres's `TRUNCATE ... CASCADE`
     (used by `conftest.py`'s `_clean_tables`) truncates *every* table with a
     (possibly transitive) FK reference to the truncated table, regardless of
     whether the FK itself declares `ON DELETE CASCADE` — so
     `TRUNCATE TABLE phone_otp_verifications, users CASCADE` already fully
     clears `exhibitions`/`visiting_cards`/`card_phones`/`card_emails`/
     `seller_profiles` between tests. Verified by reading the migration files
     only (not application/service code), per the task's instruction. No
     workaround was needed.

The OTP provider is mocked for every test via the `fake_otp_provider` fixture
(see `conftest.py`) — no test in this file ever talks to a real SMS gateway,
OCR vision API, or enrichment provider (none of which this feature calls
anyway; they belong to later steps). Happy-path upload tests do exercise the
real `storage_service` (a real boto3 client against the configured
`s3_endpoint_url`, i.e. the local `minio` service from `docker-compose.yml`)
rather than mocking it, matching this repo's established "real local
infra over mocks" philosophy for Postgres in this same `conftest.py`; a
`monkeypatch`-based guard (`_forbid_storage_upload`) is used instead on every
*rejected*-batch test to assert storage is never touched when validation
fails, per the spec's "touch neither storage nor the database" rule. No test
ever talks to a real Celery broker: `process_card.delay(...)` is exercised by
monkeypatching `.delay` to record invocations (never a live Redis broker),
and the `process_card` task body itself is called directly as a plain
function, per the project's rule to test Celery task logic independent of
the broker — no test inspects Celery/Redis directly, since
`05-card-extraction` (not this feature) owns the task's real logic.
"""

from __future__ import annotations

import io
import uuid
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.core.config import settings
from app.main import app as fastapi_app
from app.models.visiting_card import VisitingCard
from app.workers.card_processing import process_card
from conftest import create_verified_user


# --------------------------------------------------------------------------
# Image-byte helpers — real, Pillow-decodable images per task instructions
# (never placeholder/random bytes for "valid" cases).
# --------------------------------------------------------------------------


def _image_bytes(fmt: str, size: tuple[int, int] = (20, 20), color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format=fmt)
    return buf.getvalue()


def _oversized_png_bytes(min_bytes: int) -> bytes:
    """A genuinely valid, Pillow-decodable PNG whose byte size exceeds
    `min_bytes`. Uses `compress_level=0` so the on-disk size is a
    deterministic, easily-grown function of pixel count rather than a guess
    at how well a compressor shrinks synthetic pixel data."""
    side = 64
    while True:
        buf = io.BytesIO()
        Image.new("RGB", (side, side), color="blue").save(buf, format="PNG", compress_level=0)
        data = buf.getvalue()
        if len(data) > min_bytes:
            return data
        side = int(side * 1.5) + 1


NON_IMAGE_BYTES = b"this is definitely not image bytes, just plain text content"


# --------------------------------------------------------------------------
# Auth helpers — reuse conftest's signup/verify helper, add the login step
# test_03 established, plus a "second independent session" helper for
# cross-user isolation tests (the shared `client` fixture only gives one
# cookie jar per test, so a second `TestClient` wrapping the same `app`
# singleton is needed to hold two authenticated sessions simultaneously).
# --------------------------------------------------------------------------


def _login(client: TestClient, user: dict) -> None:
    resp = client.post("/auth/login", json={"email": user["email"], "password": user["password"]})
    assert resp.status_code == 200, resp.text


def _authenticated_user(client: TestClient, fake_otp_provider, **overrides) -> dict:
    user = create_verified_user(client, fake_otp_provider, **overrides)
    _login(client, user)
    return user


def _upload_files(
    client: TestClient,
    files: list[tuple[str, bytes, str]],
    exhibition_id: str | None = None,
):
    data = {}
    if exhibition_id is not None:
        data["exhibition_id"] = exhibition_id
    return client.post(
        "/cards/bulk-upload",
        data=data,
        files=[("files", (name, content, ctype)) for name, content, ctype in files],
    )


def _forbid_storage_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails loudly if a rejected/invalid bulk-upload request touches storage
    at all. Spec: 'on any failure, reject the whole request with 400 ...
    and touch neither storage nor the database (no partial batches, no
    orphaned objects)' — this makes that rule an enforced assertion rather
    than an inference from the DB being empty."""

    def _boom(*args, **kwargs):
        raise AssertionError(
            "storage_service.upload_file must never be called for a bulk-upload "
            "request that is ultimately rejected"
        )

    monkeypatch.setattr("app.services.storage_service.upload_file", _boom)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def jpeg_bytes() -> bytes:
    return _image_bytes("JPEG")


@pytest.fixture
def png_bytes() -> bytes:
    return _image_bytes("PNG")


@pytest.fixture
def webp_bytes() -> bytes:
    return _image_bytes("WEBP")


@pytest.fixture(scope="session")
def oversized_png_bytes() -> bytes:
    return _oversized_png_bytes(settings.max_upload_file_size_bytes)


# --------------------------------------------------------------------------
# 1. Auth guard — every new route requires a session
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, path",
    [
        ("post", "/exhibitions"),
        ("get", "/exhibitions"),
        ("post", "/cards/bulk-upload"),
        ("get", "/cards"),
    ],
)
def test_endpoint_without_session_returns_401(client, method: str, path: str):
    resp = getattr(client, method)(path)
    assert resp.status_code == 401, (
        f"{method.upper()} {path} without a session must return 401, got "
        f"{resp.status_code}: {resp.text}"
    )


# --------------------------------------------------------------------------
# 2. POST /exhibitions — happy path + validation
# --------------------------------------------------------------------------


def test_create_exhibition_returns_201_owned_by_caller_with_expected_fields(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)

    payload = {
        "name": "IMTEX 2026",
        "location": "Bangalore",
        "start_date": "2026-08-01",
        "end_date": "2026-08-05",
    }
    resp = client.post("/exhibitions", json=payload)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == payload["name"]
    assert body["location"] == payload["location"]
    assert body["start_date"] == payload["start_date"]
    assert body["end_date"] == payload["end_date"]
    assert body.get("exhibition_id"), "must return a generated exhibition_id"
    assert body.get("created_at"), "must return a created_at timestamp"

    # DB side effect, observed through the API — implementation-agnostic.
    listing = client.get("/exhibitions")
    assert listing.status_code == 200, listing.text
    ids = [e["exhibition_id"] for e in listing.json()]
    assert body["exhibition_id"] in ids, (
        "an exhibition created via POST /exhibitions must show up in the caller's own "
        "GET /exhibitions"
    )


def test_create_exhibition_with_only_required_name_returns_201_with_null_optional_fields(
    client, fake_otp_provider
):
    _authenticated_user(client, fake_otp_provider)

    resp = client.post("/exhibitions", json={"name": "Minimal Show"})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Minimal Show"
    assert body["location"] is None
    assert body["start_date"] is None
    assert body["end_date"] is None


def test_create_exhibition_missing_name_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)

    resp = client.post("/exhibitions", json={"location": "Pune"})

    assert resp.status_code == 422, resp.text
    assert "detail" in resp.json()


# --------------------------------------------------------------------------
# 3. GET /exhibitions — tenant/user isolation for a member/org-less user
# --------------------------------------------------------------------------


def test_list_exhibitions_returns_only_callers_own_for_org_less_user(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    mine = client.post("/exhibitions", json={"name": "My Show"})
    assert mine.status_code == 201, mine.text

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        theirs = other_client.post("/exhibitions", json={"name": "Their Show"})
        assert theirs.status_code == 201, theirs.text

        my_list = client.get("/exhibitions")
        their_list = other_client.get("/exhibitions")

    assert my_list.status_code == 200, my_list.text
    assert their_list.status_code == 200, their_list.text

    my_names = {e["name"] for e in my_list.json()}
    their_names = {e["name"] for e in their_list.json()}

    assert "My Show" in my_names
    assert "Their Show" not in my_names, (
        "a member/org-less user must never see another user's exhibitions"
    )
    assert "Their Show" in their_names
    assert "My Show" not in their_names


# --------------------------------------------------------------------------
# 4. POST /cards/bulk-upload — happy path
# --------------------------------------------------------------------------


def test_bulk_upload_valid_files_no_exhibition_creates_expected_rows(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    _authenticated_user(client, fake_otp_provider)

    filenames = [f"card_{i}.jpg" for i in range(3)]
    files = [(name, jpeg_bytes, "image/jpeg") for name in filenames]

    resp = _upload_files(client, files)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["batch_size"] == 3
    assert len(body["cards"]) == 3

    card_ids: set[str] = set()
    for card in body["cards"]:
        assert card["status"] == "new"
        assert card["exhibition_id"] is None
        assert card["original_filename"] in filenames
        assert card.get("card_id"), "each card summary must include a generated card_id"
        card_ids.add(card["card_id"])
    assert len(card_ids) == 3, "each uploaded file must produce a distinct card_id"

    # DB side effect via the real ORM model — status='new', exhibition_id=NULL,
    # user_id set, per spec.
    rows = (
        db_session.execute(
            select(VisitingCard).where(
                VisitingCard.card_id.in_([uuid.UUID(cid) for cid in card_ids])
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 3, "exactly 3 visiting_cards rows must be created for 3 uploaded files"
    for row in rows:
        assert row.status == "new"
        assert row.exhibition_id is None
        assert row.user_id is not None
        assert row.original_filename in filenames

    # DB side effect via the API too, staying implementation-agnostic.
    listing = client.get("/cards")
    assert listing.status_code == 200, listing.text
    listed_ids = {c["card_id"] for c in listing.json()}
    assert card_ids <= listed_ids, "every created card must be visible via GET /cards"

    # Spec: GET /cards computes a presigned URL at read time and never
    # persists it — the stored key and the returned URL must differ.
    returned_card = next(c for c in listing.json() if c["card_id"] in card_ids)
    stored_row = next(r for r in rows if str(r.card_id) == returned_card["card_id"])
    assert returned_card["image_url"] != stored_row.image_url, (
        "GET /cards must return a presigned URL computed at read time, not the raw "
        "stored object key"
    )


def test_bulk_upload_enqueues_one_process_card_task_per_created_card(
    client, fake_otp_provider, jpeg_bytes, monkeypatch
):
    """Spec: 'enqueues a process_card Celery task per created card' /
    Definition of Done: '...enqueues 3 process_card tasks'. Verified by
    monkeypatching the task's `.delay` (never a live broker/Redis) and
    counting/inspecting invocations — per the project's rule to test Celery
    task interactions directly rather than requiring a running broker."""
    _authenticated_user(client, fake_otp_provider)

    enqueued_card_ids: list[str] = []
    monkeypatch.setattr(
        "app.services.card_service.process_card.delay",
        lambda card_id: enqueued_card_ids.append(card_id),
    )

    files = [(f"card_{i}.jpg", jpeg_bytes, "image/jpeg") for i in range(3)]
    resp = _upload_files(client, files)

    assert resp.status_code == 201, resp.text
    created_ids = {c["card_id"] for c in resp.json()["cards"]}

    assert len(enqueued_card_ids) == 3, (
        f"exactly one process_card task must be enqueued per created card, got {enqueued_card_ids!r}"
    )
    assert set(enqueued_card_ids) == created_ids, (
        "each enqueued process_card task must reference the id of a card actually created"
    )


def test_bulk_upload_accepts_every_configured_content_type(
    client, fake_otp_provider, jpeg_bytes, png_bytes, webp_bytes
):
    _authenticated_user(client, fake_otp_provider)

    candidates = [
        ("a.jpg", jpeg_bytes, "image/jpeg"),
        ("b.png", png_bytes, "image/png"),
        ("c.webp", webp_bytes, "image/webp"),
    ]
    allowed = settings.allowed_card_image_content_types
    files = [f for f in candidates if f[2] in allowed]
    assert files, f"expected at least one of {candidates!r} to be in {allowed!r}"

    resp = _upload_files(client, files)

    assert resp.status_code == 201, resp.text
    assert resp.json()["batch_size"] == len(files)


def test_bulk_upload_with_own_exhibition_id_attaches_cards_to_it(client, fake_otp_provider, jpeg_bytes):
    _authenticated_user(client, fake_otp_provider)
    exhibition = client.post("/exhibitions", json={"name": "Attach Show"})
    assert exhibition.status_code == 201, exhibition.text
    exhibition_id = exhibition.json()["exhibition_id"]

    resp = _upload_files(client, [("card.jpg", jpeg_bytes, "image/jpeg")], exhibition_id=exhibition_id)

    assert resp.status_code == 201, resp.text
    card = resp.json()["cards"][0]
    assert card["exhibition_id"] == exhibition_id

    filtered = client.get("/cards", params={"exhibition_id": exhibition_id})
    assert filtered.status_code == 200, filtered.text
    assert any(c["card_id"] == card["card_id"] for c in filtered.json())


# --------------------------------------------------------------------------
# 5. POST /cards/bulk-upload — exhibition_id belonging to a different user
# --------------------------------------------------------------------------


def test_bulk_upload_with_other_users_exhibition_id_returns_404_and_creates_nothing(
    client, fake_otp_provider, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        theirs = other_client.post("/exhibitions", json={"name": "Not Yours"})
        assert theirs.status_code == 201, theirs.text
        their_exhibition_id = theirs.json()["exhibition_id"]

    _forbid_storage_upload(monkeypatch)
    resp = _upload_files(client, [("card.jpg", jpeg_bytes, "image/jpeg")], exhibition_id=their_exhibition_id)

    assert resp.status_code == 404, resp.text

    listing = client.get("/cards")
    assert listing.status_code == 200, listing.text
    assert listing.json() == [], (
        "no card row may be created when exhibition_id doesn't belong to the caller"
    )


def test_bulk_upload_with_malformed_exhibition_id_returns_422(client, fake_otp_provider, jpeg_bytes):
    _authenticated_user(client, fake_otp_provider)

    resp = _upload_files(client, [("card.jpg", jpeg_bytes, "image/jpeg")], exhibition_id="not-a-uuid")

    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------
# 6. POST /cards/bulk-upload — one bad file rejects the whole batch
# --------------------------------------------------------------------------


def test_bulk_upload_with_one_non_image_file_rejects_entire_batch_and_creates_nothing(
    client, fake_otp_provider, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    _forbid_storage_upload(monkeypatch)

    files = [
        ("good1.jpg", jpeg_bytes, "image/jpeg"),
        # Genuinely non-image bytes with a lying image/jpeg content-type
        # label, so the Pillow-based `img.verify()` content check (not just
        # a Content-Type header mismatch) is what fails this file.
        ("fake.jpg", NON_IMAGE_BYTES, "image/jpeg"),
        ("good2.jpg", jpeg_bytes, "image/jpeg"),
    ]

    resp = _upload_files(client, files)

    assert resp.status_code == 400, resp.text

    listing = client.get("/cards")
    assert listing.status_code == 200, listing.text
    assert listing.json() == [], (
        "one invalid file anywhere in the batch must reject the whole request and "
        "create zero rows — no partial batches"
    )


# --------------------------------------------------------------------------
# 7. POST /cards/bulk-upload — size and batch-count limits
# --------------------------------------------------------------------------


def test_bulk_upload_with_file_over_max_size_returns_400_and_creates_nothing(
    client, fake_otp_provider, oversized_png_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    assert len(oversized_png_bytes) > settings.max_upload_file_size_bytes, (
        "fixture bug: oversized_png_bytes must actually exceed the configured limit"
    )
    _forbid_storage_upload(monkeypatch)

    resp = _upload_files(client, [("huge.png", oversized_png_bytes, "image/png")])

    assert resp.status_code == 400, resp.text

    listing = client.get("/cards")
    assert listing.status_code == 200, listing.text
    assert listing.json() == [], "an oversized file must create nothing"


def test_bulk_upload_batch_over_max_file_count_returns_400_and_creates_nothing(
    client, fake_otp_provider, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    too_many = settings.max_bulk_upload_files + 1
    files = [(f"card_{i}.jpg", jpeg_bytes, "image/jpeg") for i in range(too_many)]
    _forbid_storage_upload(monkeypatch)

    resp = _upload_files(client, files)

    assert resp.status_code == 400, resp.text

    listing = client.get("/cards")
    assert listing.status_code == 200, listing.text
    assert listing.json() == [], (
        "a batch exceeding the configured max file count must create nothing"
    )


def test_bulk_upload_with_no_files_is_rejected(client, fake_otp_provider):
    """Edge case (empty batch) not enumerated in the spec's Definition of
    Done — see module docstring judgment call #1. Either FastAPI's own
    required-field validation (422) or a service-level rejection (400) is
    accepted; 201 is not."""
    _authenticated_user(client, fake_otp_provider)

    resp = client.post("/cards/bulk-upload", data={})

    assert resp.status_code in (400, 422), (
        f"an empty upload batch must not succeed, got {resp.status_code}: {resp.text}"
    )


# --------------------------------------------------------------------------
# 8. GET /cards — user isolation, filters
# --------------------------------------------------------------------------


def test_list_cards_returns_only_callers_own_for_org_less_user(client, fake_otp_provider, jpeg_bytes):
    _authenticated_user(client, fake_otp_provider)
    mine = _upload_files(client, [("mine.jpg", jpeg_bytes, "image/jpeg")])
    assert mine.status_code == 201, mine.text
    my_card_id = mine.json()["cards"][0]["card_id"]

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        theirs = _upload_files(other_client, [("theirs.jpg", jpeg_bytes, "image/jpeg")])
        assert theirs.status_code == 201, theirs.text
        their_card_id = theirs.json()["cards"][0]["card_id"]

        my_listing = client.get("/cards")
        their_listing = other_client.get("/cards")

    assert my_listing.status_code == 200, my_listing.text
    assert their_listing.status_code == 200, their_listing.text

    my_ids = {c["card_id"] for c in my_listing.json()}
    their_ids = {c["card_id"] for c in their_listing.json()}

    assert my_card_id in my_ids
    assert my_card_id not in their_ids, "a member/org-less user must never see another user's cards"
    assert their_card_id in their_ids
    assert their_card_id not in my_ids


def test_list_cards_filter_by_exhibition_id(client, fake_otp_provider, jpeg_bytes):
    _authenticated_user(client, fake_otp_provider)
    exhibition = client.post("/exhibitions", json={"name": "Filter Show"})
    assert exhibition.status_code == 201, exhibition.text
    exhibition_id = exhibition.json()["exhibition_id"]

    attached = _upload_files(client, [("attached.jpg", jpeg_bytes, "image/jpeg")], exhibition_id=exhibition_id)
    assert attached.status_code == 201, attached.text
    attached_card_id = attached.json()["cards"][0]["card_id"]

    unattached = _upload_files(client, [("unattached.jpg", jpeg_bytes, "image/jpeg")])
    assert unattached.status_code == 201, unattached.text
    unattached_card_id = unattached.json()["cards"][0]["card_id"]

    filtered = client.get("/cards", params={"exhibition_id": exhibition_id})

    assert filtered.status_code == 200, filtered.text
    filtered_ids = {c["card_id"] for c in filtered.json()}
    assert attached_card_id in filtered_ids
    assert unattached_card_id not in filtered_ids, (
        "filtering by exhibition_id must exclude cards attached to a different (or no) exhibition"
    )


def test_list_cards_filter_by_status(client, fake_otp_provider, jpeg_bytes):
    _authenticated_user(client, fake_otp_provider)
    upload = _upload_files(client, [("card.jpg", jpeg_bytes, "image/jpeg")])
    assert upload.status_code == 201, upload.text
    card_id = upload.json()["cards"][0]["card_id"]

    matching = client.get("/cards", params={"status": "new"})
    assert matching.status_code == 200, matching.text
    assert card_id in {c["card_id"] for c in matching.json()}, (
        "status=new must include a freshly uploaded card"
    )

    # "scored" is just a status value this freshly-uploaded card cannot have
    # yet (no card can reach it without 05-card-extraction/06-scoring having
    # run) — used purely to prove the filter is actually applied rather than
    # silently ignored.
    non_matching = client.get("/cards", params={"status": "scored"})
    assert non_matching.status_code == 200, non_matching.text
    assert card_id not in {c["card_id"] for c in non_matching.json()}, (
        "status filter must actually be applied — filtering on a status this card doesn't "
        "have must exclude it"
    )


def test_list_cards_with_malformed_exhibition_id_query_param_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)

    resp = client.get("/cards", params={"exhibition_id": "not-a-uuid"})

    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------
# 9. GET /cards — image_url shape
# --------------------------------------------------------------------------


def test_card_image_url_is_a_well_formed_absolute_url(client, fake_otp_provider, jpeg_bytes):
    _authenticated_user(client, fake_otp_provider)
    upload = _upload_files(client, [("card.jpg", jpeg_bytes, "image/jpeg")])
    assert upload.status_code == 201, upload.text

    listing = client.get("/cards")
    assert listing.status_code == 200, listing.text
    cards = listing.json()
    assert len(cards) == 1
    image_url = cards[0]["image_url"]

    assert isinstance(image_url, str) and image_url, (
        "GET /cards must return a non-empty image_url per card"
    )
    parsed = urlparse(image_url)
    assert parsed.scheme in ("http", "https") and parsed.netloc, (
        f"image_url is expected to be a presigned, absolute HTTP(S) URL per spec, got "
        f"{image_url!r}"
    )


# --------------------------------------------------------------------------
# 10. `process_card` Celery task body — tested directly, no live broker
# --------------------------------------------------------------------------


def test_process_card_task_loads_existing_card_and_does_not_raise(
    client, fake_otp_provider, jpeg_bytes, db_session
):
    """Spec: this step's task body is 'a placeholder (loads the card,
    no-ops)' — real vision-LLM extraction is 05-card-extraction's job. Calls
    the task function directly (never `.delay`, never a real broker), per
    the project's rule to test Celery task logic independent of the broker."""
    _authenticated_user(client, fake_otp_provider)
    upload = _upload_files(client, [("card.jpg", jpeg_bytes, "image/jpeg")])
    assert upload.status_code == 201, upload.text
    card_id = upload.json()["cards"][0]["card_id"]

    process_card(card_id)  # direct call — must not raise

    row = db_session.execute(
        select(VisitingCard).where(VisitingCard.card_id == uuid.UUID(card_id))
    ).scalar_one()
    assert row.status == "new", (
        "the placeholder process_card task body must not mutate card status — "
        "extraction logic belongs to 05-card-extraction, not this step"
    )


def test_process_card_task_with_unknown_card_id_does_not_raise():
    """Edge case implied by (not verbatim in) the spec's 'loads the card,
    no-ops' placeholder description: a stale/deleted card_id reaching a
    worker must not crash it."""
    process_card(str(uuid.uuid4()))  # direct call — must not raise


# --------------------------------------------------------------------------
# Out of scope for this file (documented, not silently skipped) — see module
# docstring and task instructions: admin-sees-org-members visibility needs an
# org/admin signup path no conftest helper currently supports.
# --------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "Admin-sees-org-members visibility for GET /exhibitions and GET /cards requires "
        "putting a user through an org + admin/member setup that no conftest helper "
        "currently supports (02-user-registration only ever produces org_id=NULL, "
        "role=NULL accounts). Per task instructions, this is documented as a deliberate "
        "gap rather than fabricated via direct ORM row manipulation that would encode "
        "assumptions about a future org-invite feature's implementation."
    )
)
def test_admin_sees_every_org_members_exhibitions_and_cards():
    pass
