"""
Tests for the `05-parsing-visiting-card` feature (spec:
`.claude/specs/05-parsing-visiting-card.md`).

These tests are written directly against the spec's documented contract, not
against the implementation of `workers/card_processing.py`,
`services/extraction_service.py`, `services/vision_client.py`,
`services/designation.py`, or `services/card_service.py`:

- `process_card(card_id)` (Celery task) — loads the card, guards against
  double-delivery, calls `extraction_service.extract_card`, and maps the
  outcome ("extracted" | "merged" | "duplicate") or a caught exception to
  `status`/`processed_at`/`extraction_error`. A permanent failure
  (`ExtractionValidationError`) is never retried; a transient failure
  (`VisionApiError`) is retried by Celery up to `max_retries=3` before
  falling through to the same `status='failed'` handling.
- Back-of-card handling: a photo with no name/title/emails/phones (or the
  model's own `is_back_of_card` flag) is folded onto the sibling card at
  `batch_sequence - 1` in the same `upload_batch_id`, fill-gaps-only (the
  canonical row's existing fields are never overwritten). No sibling ->
  processed as an ordinary card instead of being dropped.
- Duplicate handling: a three-tier priority lookup (primary email
  case-insensitive -> primary E.164 phone -> normalized full_name + company)
  scoped to the same visibility rule as the rest of this app (never crosses
  users/orgs) folds a re-scanned contact onto the existing lead the same
  fill-gaps-only way.
- `GET /cards/{card_id}` / `POST /cards/{card_id}/reprocess` — org-scoped
  detail/reprocess endpoints with the same visibility rule as `GET /cards`.

Mocking strategy: every test in this file mocks `vision_client.extract_card_fields`
(never the raw `anthropic` SDK, never a real network call) via `_patch_vision`.
Storage (`storage_service.upload_file`/`download_file`) is exercised for real
against the local MinIO service from `infra/docker-compose.yml`, matching
this repo's established "real local infra over mocks" philosophy for
Postgres/MinIO in `test_04_visiting_card_bulk_upload.py` — this feature never
touches a real Anthropic endpoint, which is the one boundary the task
instructions require mocking.

Judgment calls made in the absence of explicit spec text (documented inline
at point of use too):
  1. **Celery retry-loop testing methodology.** A bare `process_card(card_id)`
     function call runs through Celery's `Task.__call__`, which defaults
     `self.request.called_directly = True`. Celery's own `Task.retry()`
     implementation special-cases `called_directly`: it immediately
     re-raises the original exception instead of looping/backing off, since
     there's no live worker/broker to redeliver the task. `process_card.apply(args=(card_id,))`,
     by contrast, runs through the exact same `trace_task` machinery a live
     worker uses (`called_directly=False`, `is_eager=True`), so Celery's
     retry loop (backoff, retry counting, eventual `MaxRetriesExceededError`)
     is exercised for real, entirely in-process — no broker required, per
     this project's rule to test Celery task logic independent of the
     broker. Every test that exercises a transient (`VisionApiError`) retry
     path in this file therefore uses `.apply(...)`, not a bare call; every
     other test uses a bare `process_card(card_id)` call (matching
     `test_04`'s established convention) since no retry is involved.
  2. **Distinct company names per test.** `conftest.py`'s `_clean_tables`
     fixture truncates `phone_otp_verifications, users CASCADE` — which
     transitively clears `visiting_cards`/`card_emails`/`card_phones` (FK
     descendants of `users`) but **not** `companies` (a `companies` row has
     no FK path back to `users`; it's the *referenced* table, not a
     dependent one, per the data model's "shared across orgs" design). To
     keep every test's `companies` get-or-create lookup (`normalized_name`)
     from ever accidentally matching a stray row left behind by a different
     test (order-dependent, non-deterministic flakiness), every test in this
     file uses a company name string that appears nowhere else in the file.
  3. **Phone/email fixture values.** `"+14155552671"` is used throughout as
     the "valid, parseable" phone example — a real NANP-format number that
     passes `phonenumbers.is_valid_number` regardless of the extraction
     service's configured default region (it carries its own `+1` country
     code). Emails use `.com`/`.org`-style domains rather than reserved
     example TLDs, since `email_validator`'s syntax check (even with
     `check_deliverability=False`) is domain-shape sensitive.
  4. **Admin-sees-teammate visibility** for `GET /cards/{card_id}` is left as
     a documented gap, identical in cause and treatment to
     `test_04_visiting_card_bulk_upload.py`'s own skip: `02-user-registration`
     only ever produces `org_id=NULL, role=NULL` accounts, and no conftest
     helper exists yet to put a user through an org-invite/admin flow.

Note on `test_04_visiting_card_bulk_upload.py`: that file's
`test_process_card_task_loads_existing_card_and_does_not_raise` asserted the
OLD placeholder no-op behavior of `process_card` (loads the card, does not
mutate `status`) — this is now obsolete now that `process_card` performs real
extraction, and has been replaced there with a `pytest.mark.skip` placeholder
pointing at this file's real coverage, rather than left in place to fail
non-deterministically (it would otherwise attempt a real, unmocked call to
the Anthropic API on every run of that file).
"""

from __future__ import annotations

import io
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.main import app as fastapi_app
from app.models.card_email import CardEmail
from app.models.card_phone import CardPhone
from app.models.company import Company
from app.models.visiting_card import VisitingCard
from app.services.exceptions import VisionApiError
from app.workers.card_processing import process_card
from conftest import create_verified_user

VALID_PHONE = "+14155552671"


# --------------------------------------------------------------------------
# Image bytes — a real, Pillow-decodable JPEG (never placeholder bytes),
# matching test_04's established convention.
# --------------------------------------------------------------------------


def _image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (20, 20), color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture
def jpeg_bytes() -> bytes:
    return _image_bytes("JPEG")


# --------------------------------------------------------------------------
# Auth helpers — same pattern as test_04.
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


def _upload_one(client: TestClient, jpeg_bytes: bytes, filename: str = "card.jpg") -> str:
    resp = _upload_files(client, [(filename, jpeg_bytes, "image/jpeg")])
    assert resp.status_code == 201, resp.text
    return resp.json()["cards"][0]["card_id"]


def _upload_two(client: TestClient, jpeg_bytes: bytes) -> tuple[str, str]:
    """Uploads two files in a single bulk-upload call so they share one
    `upload_batch_id` with sequential `batch_sequence` (0, 1) — required for
    the back-of-card sibling lookup."""
    resp = _upload_files(
        client,
        [("front.jpg", jpeg_bytes, "image/jpeg"), ("back.jpg", jpeg_bytes, "image/jpeg")],
    )
    assert resp.status_code == 201, resp.text
    cards = resp.json()["cards"]
    return cards[0]["card_id"], cards[1]["card_id"]


# --------------------------------------------------------------------------
# Vision-model mocking — the ONLY external boundary this feature calls.
# Never a real network call; `vision_client.extract_card_fields` is patched
# directly (not the raw `anthropic` SDK).
# --------------------------------------------------------------------------


def _patch_vision(monkeypatch: pytest.MonkeyPatch, *responses) -> list[tuple[bytes, str]]:
    """Each call to `extract_card_fields` consumes the next entry in
    `responses`, in order. An entry that is an `Exception` *instance* is
    raised instead of returned, so a test can script e.g.
    `VisionApiError(...), <success dict>` to simulate "fails once, then
    recovers on retry"."""
    queue = list(responses)
    calls: list[tuple[bytes, str]] = []

    def _fake(image_bytes: bytes, media_type: str):
        calls.append((image_bytes, media_type))
        if not queue:
            raise AssertionError(
                "extract_card_fields was called more times than this test scripted responses for"
            )
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("app.services.vision_client.extract_card_fields", _fake)
    return calls


def _fields(
    *,
    is_back_of_card: bool = False,
    full_name: str | None = "Extracted Contact",
    job_title: str | None = None,
    company_name: str | None = None,
    website: str | None = None,
    address: str | None = None,
    products_offered: str | None = None,
    special_remark: str | None = None,
    raw_ocr_text: str | None = "verbatim card text",
    emails: list[dict] | None = None,
    phones: list[dict] | None = None,
    gst_number: str | None = None,
) -> dict:
    """Builds a raw vision-model response dict matching the tool schema
    `vision_client.py` documents (name, title, company, website, address,
    products_offered, handwritten remark, emails[], phones[], is_back_of_card,
    gst_number [GST-extraction amendment]). `gst_number` defaults to `None` so
    every pre-existing call site in this file (written before the amendment)
    is unaffected."""
    return {
        "is_back_of_card": is_back_of_card,
        "full_name": full_name,
        "job_title": job_title,
        "company_name": company_name,
        "website": website,
        "address": address,
        "products_offered": products_offered,
        "special_remark": special_remark,
        "raw_ocr_text": raw_ocr_text,
        "emails": [] if emails is None else emails,
        "phones": [] if phones is None else phones,
        "gst_number": gst_number,
    }


def _empty_fields(raw_ocr_text: str | None = "blank or unrelated photo") -> dict:
    """A well-formed model response with no usable card fields at all — the
    permanent-failure ('this wasn't a business card') case per spec."""
    return _fields(
        full_name=None,
        raw_ocr_text=raw_ocr_text,
    )


def _set_card_fields(db_session, card_id: str, **fields) -> None:
    """Directly sets fields on a card row via the ORM, for constructing a
    specific pre-existing state (e.g. status='failed', or an artificial
    status the pipeline itself never organically reaches within a single
    test) — a test-data setup technique, not a derivation of feature logic."""
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    for key, value in fields.items():
        setattr(card, key, value)
    db_session.commit()


# --------------------------------------------------------------------------
# 1. Auth guard — GET /cards/{card_id} and POST /cards/{card_id}/reprocess
#    both require a session.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method, suffix", [("get", ""), ("post", "/reprocess")])
def test_card_detail_and_reprocess_without_session_return_401(client, method: str, suffix: str):
    resp = getattr(client, method)(f"/cards/{uuid.uuid4()}{suffix}")
    assert resp.status_code == 401, (
        f"{method.upper()} /cards/{{card_id}}{suffix} without a session must return 401, got "
        f"{resp.status_code}: {resp.text}"
    )


# --------------------------------------------------------------------------
# 2. Happy-path extraction
# --------------------------------------------------------------------------


def test_extraction_happy_path_populates_fields_and_creates_related_rows(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: a clear, valid business-card photo results in status='extracted',
    processed_at set, all printed fields populated, at least one email/phone
    row, and a companies row linked with enrichment_status='pending'; the
    lead-scoring fields stay untouched (this step never scores)."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)

    calls = _patch_vision(
        monkeypatch,
        _fields(
            full_name="Priya Sharma",
            job_title="Chief Executive Officer",
            company_name="Acme Industries Pvt Ltd",
            website="https://acme-industries.example.com",
            address="12 MG Road, Bengaluru",
            products_offered="Manufacturers of industrial valves & fittings",
            special_remark="Met at booth 14, follow up next week",
            emails=[{"email": "priya@acme-industries.com", "email_type": "work"}],
            phones=[{"phone": VALID_PHONE, "phone_type": "mobile"}],
        ),
    )

    process_card(card_id)
    assert len(calls) == 1, "exactly one vision-model call is expected for a single-attempt success"

    row = db_session.execute(
        select(VisitingCard).where(VisitingCard.card_id == uuid.UUID(card_id))
    ).scalar_one()
    assert row.status == "extracted"
    assert row.processed_at is not None
    assert row.full_name == "Priya Sharma"
    assert row.job_title == "Chief Executive Officer"
    assert row.designation_level == "c_level", "a Chief/CEO title must classify as the most senior tier"
    assert row.website == "https://acme-industries.example.com"
    assert row.address == "12 MG Road, Bengaluru"
    assert row.products_offered == "Manufacturers of industrial valves & fittings"
    assert row.special_remark == "Met at booth 14, follow up next week"
    assert row.company_id is not None

    company = db_session.get(Company, row.company_id)
    assert company is not None
    assert company.name == "Acme Industries Pvt Ltd"
    assert company.enrichment_status == "pending", (
        "extraction must not run enrichment — the companies shell row stays pending"
    )
    assert row.lead_score is None and row.score_breakdown is None and row.scored_at is None, (
        "extraction must not compute a lead score — that belongs to a later pipeline step"
    )

    emails = db_session.scalars(select(CardEmail).where(CardEmail.card_id == row.card_id)).all()
    assert len(emails) == 1
    assert emails[0].email == "priya@acme-industries.com"
    assert emails[0].is_primary is True

    phones = db_session.scalars(select(CardPhone).where(CardPhone.card_id == row.card_id)).all()
    assert len(phones) == 1
    assert phones[0].phone_e164 == VALID_PHONE
    assert phones[0].is_primary is True

    # Same DB side effect, observed through the API too.
    detail = client.get(f"/cards/{card_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["status"] == "extracted"
    assert body["company"]["name"] == "Acme Industries Pvt Ltd"
    assert body["company"]["enrichment_status"] == "pending"
    assert body["emails"][0]["email"] == "priya@acme-industries.com"
    assert body["phones"][0]["phone_e164"] == VALID_PHONE


# --------------------------------------------------------------------------
# 3. Companies get-or-create — shared across cards, cache-hit path, website
#    backfill only when currently NULL.
# --------------------------------------------------------------------------


def test_two_cards_same_normalized_company_share_one_row_and_backfill_website(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: two cards from the same (normalized) company name share one
    companies row rather than creating two; if the first card had no website
    but the second one printed one, companies.website gets backfilled."""
    _authenticated_user(client, fake_otp_provider)

    card_1 = _upload_one(client, jpeg_bytes, filename="card1.jpg")
    _patch_vision(
        monkeypatch,
        _fields(full_name="Amit Rao", company_name="Bharat Steel Works", website=None),
    )
    process_card(card_1)

    card_2 = _upload_one(client, jpeg_bytes, filename="card2.jpg")
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Sunita Iyer",
            # Different case/whitespace of the *same* company — normalized
            # matching must treat this as the same company, not a new one.
            company_name="  bharat STEEL   works ",
            website="https://bharatsteel.example.com",
        ),
    )
    process_card(card_2)

    row1 = db_session.get(VisitingCard, uuid.UUID(card_1))
    row2 = db_session.get(VisitingCard, uuid.UUID(card_2))
    assert row1.company_id is not None and row2.company_id is not None
    assert row1.company_id == row2.company_id, (
        "two cards from the same normalized company name must share one companies row, "
        "not create a second one (cache-hit path)"
    )

    company = db_session.get(Company, row1.company_id)
    assert company.website == "https://bharatsteel.example.com", (
        "an existing companies row's NULL website must be backfilled once a later card prints one"
    )


# --------------------------------------------------------------------------
# 4. Validator/normalizer pass — malformed email dropped, unparseable phone
#    kept with phone_e164=NULL.
# --------------------------------------------------------------------------


def test_malformed_email_is_dropped_while_wellformed_ones_are_kept(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: a card with a malformed extracted email (missing '@') is
    persisted with that email dropped, not stored."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(
            company_name="Nilgiri Precision Works",
            emails=[
                {"email": "not-a-valid-email-missing-at-sign", "email_type": "work"},
                {"email": "valid.contact@example.com", "email_type": "personal"},
            ],
        ),
    )

    process_card(card_id)

    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row.status == "extracted"

    emails = db_session.scalars(select(CardEmail).where(CardEmail.card_id == row.card_id)).all()
    stored = {e.email for e in emails}
    assert stored == {"valid.contact@example.com"}, (
        "a malformed email (missing '@') must never be persisted, while a well-formed one "
        "on the same card is still stored"
    )


def test_unparseable_phone_is_kept_with_null_e164_and_raw_string_preserved(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: a card with an unparseable phone number is persisted with
    phone_e164=NULL but phone_raw set to the original extracted string —
    never dropped silently."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(
            company_name="Konkan Alloys Ltd",
            phones=[{"phone": "not-a-real-phone-number", "phone_type": "office"}],
        ),
    )

    process_card(card_id)

    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row.status == "extracted"

    phones = db_session.scalars(select(CardPhone).where(CardPhone.card_id == row.card_id)).all()
    assert len(phones) == 1, "an unparseable phone must never be silently dropped"
    assert phones[0].phone_e164 is None
    assert phones[0].phone_raw == "not-a-real-phone-number"


# --------------------------------------------------------------------------
# 5. Permanent failure — not a business card at all.
# --------------------------------------------------------------------------


def test_unreadable_card_image_is_marked_failed_with_no_related_rows(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: uploading a photo that is not a business card results in
    status='failed', a non-null extraction_error, processed_at set, and zero
    card_emails/card_phones rows created."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    calls = _patch_vision(monkeypatch, _empty_fields())

    process_card(card_id)

    assert len(calls) == 1, "a permanent validation failure must not be retried"

    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row.status == "failed"
    assert row.extraction_error, "a failed card must carry a non-empty extraction_error reason"
    assert row.processed_at is not None
    assert row.company_id is None

    emails = db_session.scalars(select(CardEmail).where(CardEmail.card_id == row.card_id)).all()
    phones = db_session.scalars(select(CardPhone).where(CardPhone.card_id == row.card_id)).all()
    assert emails == [] and phones == [], (
        "a card that isn't a readable business card must create zero card_emails/card_phones rows"
    )


# --------------------------------------------------------------------------
# 6. Back-of-card handling
# --------------------------------------------------------------------------


def test_back_of_card_merges_onto_front_sibling_fill_gaps_only(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: a front-of-card photo immediately followed (same batch, next
    batch_sequence) by its back results in exactly one extracted card
    carrying both sides' fields; the back photo's own row is status='merged'
    with merged_into_card_id pointing at the front, and is excluded from a
    default GET /cards. Merge is fill-gaps-only: canonical's existing fields
    are never overwritten."""
    _authenticated_user(client, fake_otp_provider)
    front_id, back_id = _upload_two(client, jpeg_bytes)

    # Front: full contact fields, already has its OWN website — this must
    # never be overwritten by the back photo's (different) website.
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Karan Mehta",
            job_title="Director - Sales",
            company_name="Precision Tools Ltd",
            website="https://precisiontools.example.com/front-printed",
            emails=[{"email": "karan@precisiontools.example.com", "email_type": "work"}],
            phones=[{"phone": VALID_PHONE, "phone_type": "mobile"}],
        ),
    )
    process_card(front_id)

    # Back: logo/address/QR only — no name, no contact info. Prints a
    # *different* website plus fields the front never had.
    _patch_vision(
        monkeypatch,
        _fields(
            is_back_of_card=True,
            full_name=None,
            job_title=None,
            company_name=None,
            website="https://precisiontools.example.com/back-printed",
            address="Plot 7, Industrial Estate, Pune",
            products_offered="Manufacturers of precision cutting tools",
            special_remark="Handwritten: ask about bulk pricing",
        ),
    )
    process_card(back_id)

    front = db_session.get(VisitingCard, uuid.UUID(front_id))
    back = db_session.get(VisitingCard, uuid.UUID(back_id))

    assert back.status == "merged"
    assert str(back.merged_into_card_id) == front_id
    assert back.processed_at is not None
    assert back.company_id is None, "a merged card must not get its own companies row"

    assert front.status == "extracted"
    assert front.merged_into_card_id is None
    assert front.website == "https://precisiontools.example.com/front-printed", (
        "fill-gaps-only merge must never overwrite a field the canonical card already has"
    )
    assert front.address == "Plot 7, Industrial Estate, Pune", "a gap must be filled from the back scan"
    assert front.products_offered == "Manufacturers of precision cutting tools"
    assert front.special_remark == "Handwritten: ask about bulk pricing"

    back_emails = db_session.scalars(select(CardEmail).where(CardEmail.card_id == back.card_id)).all()
    back_phones = db_session.scalars(select(CardPhone).where(CardPhone.card_id == back.card_id)).all()
    assert back_emails == [] and back_phones == [], (
        "the back card's own row must not get its own card_emails/card_phones rows"
    )

    # Default GET /cards must hide the merged back card; ?status=merged must
    # still surface it (audit trail).
    default_listing = client.get("/cards")
    assert default_listing.status_code == 200, default_listing.text
    default_ids = {c["card_id"] for c in default_listing.json()}
    assert front_id in default_ids
    assert back_id not in default_ids, "default GET /cards must exclude a merged back-of-card row"

    merged_listing = client.get("/cards", params={"status": "merged"})
    assert merged_listing.status_code == 200, merged_listing.text
    assert back_id in {c["card_id"] for c in merged_listing.json()}

    # GET /cards/{card_id} must still work for a merged card.
    back_detail = client.get(f"/cards/{back_id}")
    assert back_detail.status_code == 200, back_detail.text
    back_body = back_detail.json()
    assert back_body["status"] == "merged"
    assert back_body["merged_into_card_id"] == front_id


def test_back_of_card_with_no_sibling_is_processed_as_an_ordinary_card(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Spec: if no sibling exists (e.g. this was the first photo in the
    batch), the card is processed as an ordinary card instead of being
    dropped — 'better to keep a lead with only address/website/products than
    silently drop it.'"""
    _authenticated_user(client, fake_otp_provider)
    # Single-file batch: batch_sequence=0 has no batch_sequence=-1 sibling.
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(
            is_back_of_card=True,
            full_name=None,
            job_title=None,
            company_name=None,
            website="https://onlyback.example.com",
            address="Plot 9, Industrial Estate",
            products_offered="Distributors of bearings",
        ),
    )

    process_card(card_id)

    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row.status == "extracted", (
        "a back-looking photo with no matching sibling must still become its own lead, never "
        "silently dropped nor marked merged/failed"
    )
    assert row.merged_into_card_id is None
    assert row.website == "https://onlyback.example.com"
    assert row.address == "Plot 9, Industrial Estate"
    assert row.products_offered == "Distributors of bearings"


# --------------------------------------------------------------------------
# 7. Duplicate detection — three priority tiers, merge behavior.
# --------------------------------------------------------------------------


def test_duplicate_detected_by_same_primary_email_case_insensitive(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: a card for a contact whose email already exists on one of the
    caller's other non-merged/non-duplicate cards is marked duplicate,
    folded onto the existing card (filling gaps), and excluded from the
    default GET /cards listing."""
    _authenticated_user(client, fake_otp_provider)
    first_id = _upload_one(client, jpeg_bytes, filename="first.jpg")
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Neha Joshi",
            company_name="Orion Fasteners",
            emails=[{"email": "neha@orionfasteners.com", "email_type": "work"}],
        ),
    )
    process_card(first_id)

    second_id = _upload_one(client, jpeg_bytes, filename="second.jpg")
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Neha Joshi",
            company_name="Orion Fasteners",
            emails=[{"email": "NEHA@ORIONFASTENERS.COM", "email_type": "work"}],
            address="Re-scanned at a later booth",
        ),
    )
    process_card(second_id)

    first = db_session.get(VisitingCard, uuid.UUID(first_id))
    second = db_session.get(VisitingCard, uuid.UUID(second_id))

    assert second.status == "duplicate"
    assert str(second.merged_into_card_id) == first_id
    assert second.company_id is None

    assert first.status == "extracted"
    assert first.address == "Re-scanned at a later booth", (
        "a gap on the canonical card (no address yet) must be filled from the duplicate scan"
    )

    listing = client.get("/cards")
    assert listing.status_code == 200, listing.text
    assert second_id not in {c["card_id"] for c in listing.json()}, (
        "default GET /cards must exclude a duplicate row"
    )
    explicit = client.get("/cards", params={"status": "duplicate"})
    assert explicit.status_code == 200, explicit.text
    assert second_id in {c["card_id"] for c in explicit.json()}


def test_duplicate_detected_by_same_primary_phone_when_no_email_present(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Second priority tier: primary E.164 phone match, used when there is
    no primary-email match (here, no email at all)."""
    _authenticated_user(client, fake_otp_provider)
    first_id = _upload_one(client, jpeg_bytes, filename="first.jpg")
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Vikram Nair",
            company_name="Coastal Pumps Pvt Ltd",
            phones=[{"phone": VALID_PHONE, "phone_type": "mobile"}],
        ),
    )
    process_card(first_id)

    second_id = _upload_one(client, jpeg_bytes, filename="second.jpg")
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Vikram Nair",
            company_name="Coastal Pumps Pvt Ltd",
            phones=[{"phone": VALID_PHONE, "phone_type": "mobile"}],
            products_offered="Centrifugal & submersible pumps",
        ),
    )
    process_card(second_id)

    first = db_session.get(VisitingCard, uuid.UUID(first_id))
    second = db_session.get(VisitingCard, uuid.UUID(second_id))

    assert second.status == "duplicate"
    assert str(second.merged_into_card_id) == first_id
    assert first.products_offered == "Centrifugal & submersible pumps"


def test_duplicate_detected_by_normalized_name_and_company_when_no_email_or_phone(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Third priority tier: (normalized full_name, company) match, used only
    when there is no email or phone to match on."""
    _authenticated_user(client, fake_otp_provider)
    first_id = _upload_one(client, jpeg_bytes, filename="first.jpg")
    _patch_vision(
        monkeypatch,
        _fields(full_name="Ravi Kumar", company_name="Malabar Industrial Supplies"),
    )
    process_card(first_id)

    second_id = _upload_one(client, jpeg_bytes, filename="second.jpg")
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="  ravi   KUMAR  ",  # same person, different case/whitespace
            company_name="Malabar Industrial Supplies",
            special_remark="Second scan of the same person",
        ),
    )
    process_card(second_id)

    first = db_session.get(VisitingCard, uuid.UUID(first_id))
    second = db_session.get(VisitingCard, uuid.UUID(second_id))

    assert second.status == "duplicate"
    assert str(second.merged_into_card_id) == first_id
    assert first.special_remark == "Second scan of the same person"


def test_duplicate_detection_never_matches_across_different_users(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Highest-value tenant-isolation case for this feature: the
    duplicate-detection lookup must stay scoped to the card owner's own
    visibility, never searching another user's cards — a cross-tenant
    duplicate merge would silently fold one org's lead into another's."""
    _authenticated_user(client, fake_otp_provider)
    mine_id = _upload_one(client, jpeg_bytes, filename="mine.jpg")
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Shared Name",
            company_name="Shared Co Industries",
            emails=[{"email": "shared.contact@example.com", "email_type": "work"}],
        ),
    )
    process_card(mine_id)

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        theirs_id = _upload_one(other_client, jpeg_bytes, filename="theirs.jpg")
        _patch_vision(
            monkeypatch,
            _fields(
                full_name="Shared Name",
                company_name="Shared Co Industries",
                emails=[{"email": "shared.contact@example.com", "email_type": "work"}],
            ),
        )
        process_card(theirs_id)

    mine = db_session.get(VisitingCard, uuid.UUID(mine_id))
    theirs = db_session.get(VisitingCard, uuid.UUID(theirs_id))

    assert mine.status == "extracted"
    assert theirs.status == "extracted", (
        "an identical email/name/company on a DIFFERENT user's card must never be folded in as "
        "a duplicate — that would leak/merge one tenant's lead into another's"
    )
    assert theirs.merged_into_card_id is None


# --------------------------------------------------------------------------
# 8. Transient failure / Celery retry — see module docstring judgment call #1
#    for why these use `.apply(...)` rather than a bare call.
# --------------------------------------------------------------------------


def test_transient_vision_error_recovers_after_one_retry(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Spec: a transient VisionApiError (timeout/rate-limit/5xx) is retried
    rather than failing the card outright."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)

    calls = _patch_vision(
        monkeypatch,
        VisionApiError("simulated 503 from the vision API"),
        _fields(full_name="Recovered Contact", company_name="Retry Co Manufacturing"),
    )

    process_card.apply(args=(card_id,))

    assert len(calls) == 2, "the task must retry after one transient failure, succeeding on attempt 2"

    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row.status == "extracted", "a card must reach 'extracted' once a retry succeeds"
    assert row.full_name == "Recovered Contact"


def test_transient_vision_errors_exhausted_after_max_retries_marks_card_failed(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: a transient vision-API error (simulated timeout/5xx) is retried
    up to 3 times by Celery before the card is marked failed."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)

    calls = _patch_vision(
        monkeypatch,
        VisionApiError("simulated timeout #1"),
        VisionApiError("simulated timeout #2"),
        VisionApiError("simulated timeout #3"),
        VisionApiError("simulated timeout #4"),
    )

    process_card.apply(args=(card_id,))

    assert len(calls) == 4, (
        "spec's max_retries=3 means 1 initial attempt + 3 retries = 4 total vision-model calls "
        "before the card is finalized"
    )

    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row.status == "failed", (
        "once retries are exhausted, a transient failure must fall through to the same "
        "status='failed' handling as a permanent validation failure"
    )
    assert row.extraction_error, "a failed card must carry a non-empty extraction_error reason"
    assert row.processed_at is not None


def test_permanent_validation_failure_is_never_retried(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Contrast case for the two tests above: an ExtractionValidationError
    (non-card image) is marked failed immediately with no retries — a single
    vision-model call, not up to 4."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    calls = _patch_vision(monkeypatch, _empty_fields())

    process_card.apply(args=(card_id,))

    assert len(calls) == 1, "a permanent validation failure must be marked failed with no retries"

    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row.status == "failed"
    assert row.extraction_error


# --------------------------------------------------------------------------
# 9. GET /cards/{card_id} — full extraction detail.
# --------------------------------------------------------------------------


def test_get_card_detail_happy_path_returns_full_extraction_detail(
    client, fake_otp_provider, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Deepak Verma",
            job_title="Plant Manager",
            company_name="Vertex Castings",
            website="https://vertexcastings.example.com",
            address="Sector 5, MIDC, Nashik",
            products_offered="Sand & die casting for automotive parts",
            special_remark="Interested in a demo",
            emails=[{"email": "deepak@vertexcastings.com", "email_type": "work"}],
            phones=[{"phone": VALID_PHONE, "phone_type": "mobile"}],
        ),
    )
    process_card(card_id)

    resp = client.get(f"/cards/{card_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["card_id"] == card_id
    assert body["status"] == "extracted"
    assert body["full_name"] == "Deepak Verma"
    assert body["job_title"] == "Plant Manager"
    assert body["designation_level"] == "manager"
    assert body["website"] == "https://vertexcastings.example.com"
    assert body["address"] == "Sector 5, MIDC, Nashik"
    assert body["products_offered"] == "Sand & die casting for automotive parts"
    assert body["special_remark"] == "Interested in a demo"
    assert body["raw_ocr_text"]
    assert body["extraction_error"] is None
    assert body["merged_into_card_id"] is None
    assert body["company"]["name"] == "Vertex Castings"
    assert body["company"]["enrichment_status"] == "pending"
    assert len(body["emails"]) == 1
    assert body["emails"][0]["email"] == "deepak@vertexcastings.com"
    assert len(body["phones"]) == 1
    assert body["phones"][0]["phone_e164"] == VALID_PHONE


def test_get_card_detail_nonexistent_card_returns_404(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    resp = client.get(f"/cards/{uuid.uuid4()}")
    assert resp.status_code == 404, resp.text


def test_get_card_detail_malformed_card_id_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    resp = client.get("/cards/not-a-uuid")
    assert resp.status_code == 422, resp.text


def test_get_card_detail_for_another_users_card_returns_404(client, fake_otp_provider, jpeg_bytes):
    """Tenant-isolation: a user must never be able to fetch another user's
    card detail."""
    _authenticated_user(client, fake_otp_provider)

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        theirs_id = _upload_one(other_client, jpeg_bytes)
        resp = client.get(f"/cards/{theirs_id}")

    assert resp.status_code == 404, (
        f"a user must never be able to fetch another user's card detail, got {resp.status_code}"
    )


# --------------------------------------------------------------------------
# 10. POST /cards/{card_id}/reprocess
# --------------------------------------------------------------------------


def test_reprocess_failed_card_resets_to_new_clears_error_and_reenqueues(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: reprocessing a failed card resets status to 'new', clears
    extraction_error, and re-enqueues process_card, keeping the card's
    original upload_batch_id/batch_sequence so it can still be matched as a
    back side or duplicate on retry."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(monkeypatch, _empty_fields())
    process_card(card_id)  # -> status='failed'

    row_before = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row_before.status == "failed", "fixture setup: card must be failed before reprocessing"
    original_batch_id = row_before.upload_batch_id
    original_sequence = row_before.batch_sequence

    enqueued: list[str] = []
    monkeypatch.setattr(
        "app.services.card_service.process_card.delay",
        lambda cid: enqueued.append(cid),
    )

    resp = client.post(f"/cards/{card_id}/reprocess")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "new"

    db_session.expire_all()
    row_after = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row_after.status == "new"
    assert row_after.extraction_error is None
    assert row_after.upload_batch_id == original_batch_id, (
        "a reprocessed card must keep its original upload_batch_id so back-of-card/duplicate "
        "matching still works on retry"
    )
    assert row_after.batch_sequence == original_sequence

    assert enqueued == [card_id], "reprocess must re-enqueue exactly one process_card task"


@pytest.mark.parametrize("current_status", ["new", "processing", "extracted", "merged", "duplicate"])
def test_reprocess_card_not_in_failed_state_returns_409_and_makes_no_change(
    client, fake_otp_provider, db_session, jpeg_bytes, current_status
):
    """DoD: reprocessing a card whose status isn't 'failed' returns 409 and
    makes no change."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _set_card_fields(db_session, card_id, status=current_status, extraction_error=None)

    resp = client.post(f"/cards/{card_id}/reprocess")

    assert resp.status_code == 409, (
        f"reprocessing a card whose status is {current_status!r} (not 'failed') must return 409, "
        f"got {resp.status_code}: {resp.text}"
    )

    db_session.expire_all()
    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row.status == current_status, "a rejected reprocess request must not change the card's status"


def test_reprocess_nonexistent_card_returns_404(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    resp = client.post(f"/cards/{uuid.uuid4()}/reprocess")
    assert resp.status_code == 404, resp.text


def test_reprocess_malformed_card_id_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    resp = client.post("/cards/not-a-uuid/reprocess")
    assert resp.status_code == 422, resp.text


def test_reprocess_another_users_failed_card_returns_404_and_makes_no_change(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Tenant-isolation: a user must never be able to reprocess another
    user's card."""
    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        theirs_id = _upload_one(other_client, jpeg_bytes)
        _patch_vision(monkeypatch, _empty_fields())
        process_card(theirs_id)  # -> status='failed', owned by other_client's user

    _authenticated_user(client, fake_otp_provider)
    resp = client.post(f"/cards/{theirs_id}/reprocess")

    assert resp.status_code == 404, (
        f"a user must never be able to reprocess another user's card, got {resp.status_code}"
    )

    row = db_session.get(VisitingCard, uuid.UUID(theirs_id))
    assert row.status == "failed", "a rejected cross-user reprocess must not change the card's status"


# --------------------------------------------------------------------------
# 11. GET /cards — default listing excludes merged/duplicate; explicit
#     ?status= still returns them.
# --------------------------------------------------------------------------


def test_list_cards_excludes_merged_and_duplicate_by_default_but_includes_on_explicit_filter(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    _authenticated_user(client, fake_otp_provider)
    normal_id = _upload_one(client, jpeg_bytes, filename="normal.jpg")
    merged_id = _upload_one(client, jpeg_bytes, filename="merged.jpg")
    duplicate_id = _upload_one(client, jpeg_bytes, filename="duplicate.jpg")

    _set_card_fields(db_session, merged_id, status="merged", merged_into_card_id=uuid.UUID(normal_id))
    _set_card_fields(
        db_session, duplicate_id, status="duplicate", merged_into_card_id=uuid.UUID(normal_id)
    )

    default_listing = client.get("/cards")
    assert default_listing.status_code == 200, default_listing.text
    default_ids = {c["card_id"] for c in default_listing.json()}
    assert normal_id in default_ids
    assert merged_id not in default_ids, "default GET /cards must exclude status='merged' rows"
    assert duplicate_id not in default_ids, "default GET /cards must exclude status='duplicate' rows"

    merged_listing = client.get("/cards", params={"status": "merged"})
    assert merged_listing.status_code == 200, merged_listing.text
    assert merged_id in {c["card_id"] for c in merged_listing.json()}

    duplicate_listing = client.get("/cards", params={"status": "duplicate"})
    assert duplicate_listing.status_code == 200, duplicate_listing.text
    assert duplicate_id in {c["card_id"] for c in duplicate_listing.json()}


# --------------------------------------------------------------------------
# 11b. GET /cards `include_folded` amendment + `CardOut.merged_into_card_id`.
#
# Per the spec's amended "GET /cards" bullet: when `status` is omitted,
# `include_folded=true` includes merged/duplicate rows alongside normal ones
# (the default, `include_folded=false`, keeps excluding them — unchanged,
# already covered by
# `test_list_cards_excludes_merged_and_duplicate_by_default_but_includes_on_explicit_filter`
# above). An explicit `status=` filter always wins over `include_folded`
# regardless of its value. `CardOut` (list endpoint) now also carries
# `merged_into_card_id`: null for a normal/standalone card, set to the
# canonical card's id for a folded row.
#
# These tests build the merged/duplicate row directly via `_set_card_fields`
# (matching the established pattern in
# `test_list_cards_excludes_merged_and_duplicate_by_default_but_includes_on_explicit_filter`
# just above) rather than driving a full extraction merge, since the listing
# endpoint's filtering/serialization behavior being tested here doesn't
# depend on how a card came to be `merged`/`duplicate`.
# --------------------------------------------------------------------------


def test_list_cards_include_folded_true_includes_merged_and_duplicate_rows(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """Amendment: GET /cards?include_folded=true (status omitted) includes
    merged/duplicate rows alongside normal ones — the folded rows appear on
    top of whatever the default (include_folded=false) call already
    returns."""
    _authenticated_user(client, fake_otp_provider)
    normal_id = _upload_one(client, jpeg_bytes, filename="normal.jpg")
    merged_id = _upload_one(client, jpeg_bytes, filename="merged.jpg")
    duplicate_id = _upload_one(client, jpeg_bytes, filename="duplicate.jpg")

    _set_card_fields(db_session, merged_id, status="merged", merged_into_card_id=uuid.UUID(normal_id))
    _set_card_fields(
        db_session, duplicate_id, status="duplicate", merged_into_card_id=uuid.UUID(normal_id)
    )

    default_listing = client.get("/cards")
    assert default_listing.status_code == 200, default_listing.text
    default_ids = {c["card_id"] for c in default_listing.json()}
    assert normal_id in default_ids
    assert merged_id not in default_ids and duplicate_id not in default_ids, (
        "fixture setup: the default (include_folded=false) call must still exclude the folded rows"
    )

    folded_listing = client.get("/cards", params={"include_folded": "true"})
    assert folded_listing.status_code == 200, folded_listing.text
    folded_ids = {c["card_id"] for c in folded_listing.json()}
    assert normal_id in folded_ids
    assert merged_id in folded_ids, "include_folded=true must surface the merged row"
    assert duplicate_id in folded_ids, "include_folded=true must surface the duplicate row"
    assert len(folded_listing.json()) == len(default_listing.json()) + 2, (
        "include_folded=true must add exactly the previously-excluded folded rows on top of the "
        "default listing, one more row per folded card"
    )


def test_list_cards_default_call_still_excludes_merged_and_duplicate_when_include_folded_omitted(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """Brief sanity check that the new include_folded param defaults to false
    and doesn't change old behavior — full coverage of the default-exclude
    behavior already lives in
    test_list_cards_excludes_merged_and_duplicate_by_default_but_includes_on_explicit_filter."""
    _authenticated_user(client, fake_otp_provider)
    normal_id = _upload_one(client, jpeg_bytes, filename="normal.jpg")
    merged_id = _upload_one(client, jpeg_bytes, filename="merged.jpg")
    _set_card_fields(db_session, merged_id, status="merged", merged_into_card_id=uuid.UUID(normal_id))

    resp = client.get("/cards")

    assert resp.status_code == 200, resp.text
    ids = {c["card_id"] for c in resp.json()}
    assert normal_id in ids
    assert merged_id not in ids, (
        "include_folded must default to false, preserving the pre-amendment exclude-by-default behavior"
    )


def test_list_cards_every_row_carries_merged_into_card_id_null_for_normal_set_for_folded(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """Amendment: CardOut on the list endpoint now carries
    merged_into_card_id — null for a normal/standalone card, equal to the
    canonical card's id for a folded (merged/duplicate) row."""
    _authenticated_user(client, fake_otp_provider)
    normal_id = _upload_one(client, jpeg_bytes, filename="normal.jpg")
    merged_id = _upload_one(client, jpeg_bytes, filename="merged.jpg")
    _set_card_fields(db_session, merged_id, status="merged", merged_into_card_id=uuid.UUID(normal_id))

    resp = client.get("/cards", params={"include_folded": "true"})

    assert resp.status_code == 200, resp.text
    by_id = {c["card_id"]: c for c in resp.json()}

    assert "merged_into_card_id" in by_id[normal_id], (
        "CardOut must carry a merged_into_card_id key on every row, including a normal card"
    )
    assert by_id[normal_id]["merged_into_card_id"] is None, (
        "a normal/standalone card's merged_into_card_id must be null"
    )

    assert "merged_into_card_id" in by_id[merged_id]
    assert by_id[merged_id]["merged_into_card_id"] == normal_id, (
        "a folded card's merged_into_card_id on the list endpoint must equal the canonical card's id"
    )


def test_list_cards_explicit_status_filter_wins_over_include_folded_flag(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """Amendment: an explicit status= filter always wins over include_folded
    — GET /cards?status=duplicate returns duplicate rows regardless of
    whether include_folded is false or true."""
    _authenticated_user(client, fake_otp_provider)
    normal_id = _upload_one(client, jpeg_bytes, filename="normal.jpg")
    duplicate_id = _upload_one(client, jpeg_bytes, filename="duplicate.jpg")
    _set_card_fields(
        db_session, duplicate_id, status="duplicate", merged_into_card_id=uuid.UUID(normal_id)
    )

    without_folded = client.get("/cards", params={"status": "duplicate", "include_folded": "false"})
    assert without_folded.status_code == 200, without_folded.text
    without_ids = {c["card_id"] for c in without_folded.json()}
    assert duplicate_id in without_ids
    assert normal_id not in without_ids, "an explicit status=duplicate filter must not return normal cards"

    with_folded = client.get("/cards", params={"status": "duplicate", "include_folded": "true"})
    assert with_folded.status_code == 200, with_folded.text
    with_ids = {c["card_id"] for c in with_folded.json()}
    assert duplicate_id in with_ids
    assert normal_id not in with_ids

    assert without_folded.json() == with_folded.json(), (
        "an explicit status filter must produce identical results regardless of the include_folded value"
    )


# --------------------------------------------------------------------------
# 12. GST-extraction amendment: gst_number normalization/validation, the
#     "usable field" rule, fill-gaps-only merge behavior, and the
#     detail-only (never list-view) exposure rule.
# --------------------------------------------------------------------------


def test_gst_number_extracted_and_normalized_uppercase_and_whitespace_stripped(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Amendment DoD: a card printed with a well-formed GSTIN results in
    gst_number populated on the extracted card, with whitespace stripped and
    letters uppercased even if the (mocked) vision output had stray spacing
    or lowercase letters."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Rajesh Gupta",
            company_name="Himalayan Forge Works",
            gst_number="  27abcde1234f1z5 ",
        ),
    )

    process_card(card_id)

    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row.status == "extracted"
    assert row.gst_number == "27ABCDE1234F1Z5", (
        "a well-formed GSTIN must be stored uppercased with surrounding whitespace stripped"
    )


@pytest.mark.parametrize(
    "bad_gst, company_name",
    [
        # 15 characters but the 13th char must be the literal 'Z' — here it's 'Y'.
        ("27ABCDE1234F1Y5", "Deccan Fabrications Alpha"),
        # Only 14 characters — one short of the required 15.
        ("27ABCDE1234F1Z", "Deccan Fabrications Beta"),
        # 15 characters but chars 3-7 (should be 5 letters) are digits instead.
        ("1234567890ABCDE", "Deccan Fabrications Gamma"),
        # Not GSTIN-shaped at all.
        ("NOT-A-REAL-GSTIN-1234", "Deccan Fabrications Delta"),
    ],
)
def test_malformed_gst_number_variants_are_dropped_not_stored(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch, bad_gst, company_name
):
    """Amendment DoD: a malformed/hallucinated-looking GSTIN string (doesn't
    match the standard 15-character GSTIN structure) is dropped rather than
    stored, same as a malformed email — it must never crash extraction nor
    persist garbage."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(full_name="Sanjay Mehta", company_name=company_name, gst_number=bad_gst),
    )

    process_card(card_id)

    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row.status == "extracted", "a card with other usable fields must still extract successfully"
    assert row.gst_number is None, (
        f"malformed GSTIN {bad_gst!r} must be dropped, never persisted as a misread/hallucinated value"
    )


def test_no_gst_number_present_leaves_field_null(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Amendment DoD: a card with no GSTIN at all (field omitted/null from
    the model) leaves gst_number NULL, with no error."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(full_name="Meena Pillai", company_name="Anand Engineering Co", gst_number=None),
    )

    process_card(card_id)

    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row.status == "extracted"
    assert row.gst_number is None


def test_card_with_only_a_valid_gst_number_is_not_marked_failed(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Amendment: gst_number counts as a "usable field" in its own right — a
    card with only a GSTIN extracted (no name/company/contact info,
    address/website/products) must NOT be marked failed for lack of usable
    fields; it becomes a normal extracted lead."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    calls = _patch_vision(
        monkeypatch,
        _fields(
            full_name=None,
            job_title=None,
            company_name=None,
            website=None,
            address=None,
            products_offered=None,
            special_remark=None,
            gst_number="29AAAAA0000A1Z5",
        ),
    )

    process_card(card_id)

    assert len(calls) == 1, "a GSTIN-only card must succeed on the first attempt, no retry involved"
    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row.status == "extracted", (
        "a card with only a valid GSTIN extracted must NOT be marked failed — gst_number counts "
        "as a usable field on its own, same as name/company/contact info/address/website/products"
    )
    assert row.gst_number == "29AAAAA0000A1Z5"
    assert row.extraction_error is None


def test_gst_number_merge_does_not_overwrite_existing_canonical_value(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Amendment: gst_number participates in the fill-gaps-only merge — if
    the canonical card already has a gst_number, a second (duplicate) scan's
    different GSTIN must never overwrite it."""
    _authenticated_user(client, fake_otp_provider)
    first_id = _upload_one(client, jpeg_bytes, filename="first.jpg")
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Arjun Bhatt",
            company_name="Sahyadri Metal Industries",
            emails=[{"email": "arjun@sahyadrimetal.com", "email_type": "work"}],
            gst_number="27ABCDE1234F1Z5",
        ),
    )
    process_card(first_id)

    second_id = _upload_one(client, jpeg_bytes, filename="second.jpg")
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Arjun Bhatt",
            company_name="Sahyadri Metal Industries",
            # Same primary email (case-insensitive) -> duplicate match, but a
            # DIFFERENT well-formed GSTIN than the canonical card already has.
            emails=[{"email": "ARJUN@SAHYADRIMETAL.COM", "email_type": "work"}],
            gst_number="29XYZAB5678C1Z9",
        ),
    )
    process_card(second_id)

    first = db_session.get(VisitingCard, uuid.UUID(first_id))
    second = db_session.get(VisitingCard, uuid.UUID(second_id))

    assert second.status == "duplicate"
    assert str(second.merged_into_card_id) == first_id
    assert first.gst_number == "27ABCDE1234F1Z5", (
        "fill-gaps-only merge must never overwrite a gst_number the canonical card already has"
    )


def test_gst_number_merge_fills_gap_when_canonical_has_none(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Amendment: the converse fill-gaps case — if the canonical card has no
    gst_number yet, a duplicate scan's GSTIN gets filled onto it."""
    _authenticated_user(client, fake_otp_provider)
    first_id = _upload_one(client, jpeg_bytes, filename="first.jpg")
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Kavita Desai",
            company_name="Ratnagiri Marine Engineering",
            emails=[{"email": "kavita@ratnagirimarine.com", "email_type": "work"}],
            gst_number=None,
        ),
    )
    process_card(first_id)

    second_id = _upload_one(client, jpeg_bytes, filename="second.jpg")
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Kavita Desai",
            company_name="Ratnagiri Marine Engineering",
            emails=[{"email": "KAVITA@RATNAGIRIMARINE.COM", "email_type": "work"}],
            gst_number="24LMNOP9012Q1Z3",
        ),
    )
    process_card(second_id)

    first = db_session.get(VisitingCard, uuid.UUID(first_id))
    second = db_session.get(VisitingCard, uuid.UUID(second_id))

    assert second.status == "duplicate"
    assert str(second.merged_into_card_id) == first_id
    assert first.gst_number == "24LMNOP9012Q1Z3", (
        "a gap on the canonical card (no gst_number yet) must be filled in from the duplicate scan"
    )


def test_get_card_detail_includes_gst_number_for_owner(
    client, fake_otp_provider, jpeg_bytes, monkeypatch
):
    """Amendment DoD: GET /cards/{card_id} exposes gst_number (normalized) in
    the detail payload for the owner. (Admin-viewing-a-teammate's-card
    coverage is out of scope here for the same documented reason as this
    file's existing `test_admin_sees_teammates_card_detail_via_get_card_id`
    skip — no conftest helper currently supports putting a user through an
    org/admin setup; once that gap is closed, gst_number rides along with
    every other CardDetailOut field already covered there.)"""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Farhan Ali",
            company_name="Nashik Tool Room",
            gst_number="  27abcde1234f1z5 ",
        ),
    )
    process_card(card_id)

    resp = client.get(f"/cards/{card_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["gst_number"] == "27ABCDE1234F1Z5", (
        "GET /cards/{card_id} must expose the normalized gst_number in its detail payload"
    )


def test_list_cards_response_omits_gst_number_key(client, fake_otp_provider, jpeg_bytes, monkeypatch):
    """Amendment: gst_number is deliberately a detail-only field, the same
    tier as website/address/products_offered — it must not appear at all on
    the GET /cards list view (CardOut), not even as a null key."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Ismail Sheikh",
            company_name="Solapur Textile Machinery",
            gst_number="27ABCDE1234F1Z5",
        ),
    )
    process_card(card_id)

    resp = client.get("/cards")

    assert resp.status_code == 200, resp.text
    cards = resp.json()
    matching = [c for c in cards if c["card_id"] == card_id]
    assert len(matching) == 1, "the processed card must appear exactly once in the default listing"
    assert "gst_number" not in matching[0], (
        "GET /cards (list view / CardOut) must not include a gst_number key at all — it stays a "
        "detail-only field exposed only via GET /cards/{card_id}"
    )


# --------------------------------------------------------------------------
# Out of scope for this file (documented, not silently skipped) — see module
# docstring judgment call #4: admin-sees-org-members visibility needs an
# org/admin signup path no conftest helper currently supports.
# --------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "Admin-sees-org-members visibility for GET /cards/{card_id} requires putting a user "
        "through an org + admin/member setup that no conftest helper currently supports "
        "(02-user-registration only ever produces org_id=NULL, role=NULL accounts). Same "
        "documented gap as test_04_visiting_card_bulk_upload.py's own skip for GET /exhibitions "
        "and GET /cards."
    )
)
def test_admin_sees_teammates_card_detail_via_get_card_id():
    pass
