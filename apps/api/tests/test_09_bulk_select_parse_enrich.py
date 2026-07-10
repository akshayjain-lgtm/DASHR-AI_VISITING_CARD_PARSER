"""
Tests for the `09-bulk-select-parse-enrich` feature (spec:
`.claude/specs/09-bulk-select-parse-enrich.md`).

These tests are written directly against the spec's documented contract, not
against the implementation of `services/card_service.py` or
`routers/cards.py`:

- `POST /cards/process` (`CardProcessRequest`) gains an optional
  `card_ids: list[uuid.UUID] | None = None` alongside the existing
  `exhibition_id`. Omitted/null `card_ids` must behave byte-for-byte like
  before this feature (every visible `status="new"` card enqueued, optionally
  narrowed by `exhibition_id`). A provided `card_ids` list additionally
  narrows the query to those ids — any id that isn't visible to the caller or
  isn't `status="new"` is silently excluded from both the enqueue and the
  returned `enqueued_count`, never a 404/403 for an individual id. Response
  stays `{"enqueued_count": int}`.
- `POST /cards/enrich-companies` (new) — body `{"card_ids": [...]}`,
  `card_ids` has Pydantic `min_length=1` (empty list -> `422` before any DB
  access). For each id, in list order: skip (never raise) if the card isn't
  visible to the caller, has no linked company, its company's
  `enrichment_status != "pending"`, or its `company_id` was already mapped to
  an earlier id in the same request. Every remaining id enqueues
  `enrich_company_task.delay(company_id, card_id)` — the exact same task the
  existing single-card `POST /cards/{card_id}/enrich-company` endpoint
  already uses. Response: `{"enqueued_count": int, "skipped_count": int}`,
  and `enqueued_count + skipped_count == len(card_ids)` always.
- `GET /cards` (`CardOut`) gains `company_id: uuid.UUID | None` and
  `company_enrichment_status: str | None` (mirrors the linked `Company`'s
  `enrichment_status`; both null when the card has no linked company yet).

Mocking strategy: this feature introduces no new external boundary or Celery
task of its own — it only enqueues the two *existing* tasks
(`process_card`, `enrich_company_task`) more than one at a time per request.
Both are mocked at their `.delay` call site exactly as the sibling files that
introduced them already do:
`app.services.card_service.process_card.delay` (see
`test_05_parsing_visiting_card.py::test_reprocess_failed_card_resets_to_new_clears_error_and_reenqueues`)
and `app.services.card_service.enrich_company_task.delay` (see
`test_07_data_enrichment.py::_patch_enrich_delay`). Getting a card from
`status="new"` all the way to `status="extracted"` with a real linked
`Company` row (the only legitimate way to reach a card with a non-null
`company_id`) still goes through the real (in-process, no broker)
`process_card` task from `app.workers.card_processing`, with
`vision_client.extract_card_fields` mocked via `_patch_vision` — the sole
external boundary that pipeline itself has.

Judgment calls made in the absence of explicit spec text:
  1. **Distinct company names per test.** Per this repo's testing
     convention, `companies` is not truncated by `conftest.py`'s autouse
     `_clean_tables` fixture (no FK path back to `users`), so every test that
     drives a card to `status="extracted"` uses a company name containing a
     fresh `uuid.uuid4()` fragment via `_unique_company_name`, copied
     verbatim from `test_07_data_enrichment.py`.
  2. **Same-company dedup ordering.** The spec says a card whose company was
     "already matched by an earlier id in the SAME request" is skipped, which
     implies list order matters but doesn't spell out which of two ids
     sharing one company gets enqueued. This suite asserts the natural
     reading — iterating `card_ids` in the given order, the first id to map
     to a not-yet-seen `company_id` is the one enqueued, and any later id
     mapping to that same company is the one skipped.
  3. **Isolating "wrong org" from "no company" in the cross-tenant skip
     test.** To prove the visibility gate itself (not just the "no linked
     company" gate) causes the skip, the other org's card is driven through a
     real extraction to a genuine `pending`-company state before being named
     in the caller's `card_ids` list — otherwise a bare, unprocessed card
     would be skipped for the (also-true, but less interesting) "no company"
     reason instead.
  4. **`POST /cards/process` request bodies.** `CardProcessRequest`'s fields
     are all optional, so a bare `json={}` body is used for the "no
     `card_ids`" behavior-preservation test, matching how one would call this
     endpoint pre-feature.
"""

from __future__ import annotations

import io
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app as fastapi_app
from app.models.company import Company
from app.models.visiting_card import VisitingCard
from app.workers.card_processing import process_card
from conftest import create_verified_user

VALID_PHONE = "+14155552671"


# --------------------------------------------------------------------------
# Company-name uniqueness helper — copied verbatim from
# test_07_data_enrichment.py's identically-named helper, since `companies` is
# not truncated between tests.
# --------------------------------------------------------------------------


def _unique_company_name(label: str) -> str:
    """`companies` is not truncated between tests — every row created here
    must carry a name no other test could ever also create."""
    return f"{label} {uuid.uuid4().hex[:10]} Pvt Ltd"


# --------------------------------------------------------------------------
# Image bytes — a real, Pillow-decodable JPEG, matching test_05/test_07's
# established convention.
# --------------------------------------------------------------------------


def _image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (20, 20), color: str = "green") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture
def jpeg_bytes() -> bytes:
    return _image_bytes()


# --------------------------------------------------------------------------
# Auth / upload helpers — same pattern as test_05/test_07.
# --------------------------------------------------------------------------


def _login(client: TestClient, user: dict) -> None:
    resp = client.post("/auth/login", json={"email": user["email"], "password": user["password"]})
    assert resp.status_code == 200, resp.text


def _authenticated_user(client: TestClient, fake_otp_provider, **overrides) -> dict:
    user = create_verified_user(client, fake_otp_provider, **overrides)
    _login(client, user)
    return user


def _upload_files(client: TestClient, files: list[tuple[str, bytes, str]]):
    return client.post(
        "/cards/bulk-upload",
        data={},
        files=[("files", (name, content, ctype)) for name, content, ctype in files],
    )


def _upload_one(client: TestClient, jpeg_bytes: bytes, filename: str = "card.jpg") -> str:
    resp = _upload_files(client, [(filename, jpeg_bytes, "image/jpeg")])
    assert resp.status_code == 201, resp.text
    return resp.json()["cards"][0]["card_id"]


# --------------------------------------------------------------------------
# Vision-model mocking — the only external boundary `process_card` itself
# calls; never a real network call.
# --------------------------------------------------------------------------


def _patch_vision(monkeypatch: pytest.MonkeyPatch, *responses) -> list[tuple[bytes, str]]:
    queue = list(responses)
    calls: list[tuple[bytes, str]] = []

    def _fake(image_bytes: bytes, media_type: str):
        calls.append((image_bytes, media_type))
        if not queue:
            raise AssertionError("extract_card_fields called more times than this test scripted")
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


def _empty_fields() -> dict:
    """A well-formed model response with no usable card fields at all — the
    permanent-failure ('this wasn't a business card') case, used here only to
    drive a card to status='failed' (and therefore company_id=None) for the
    "card with no linked company" enrich-companies scenario."""
    return _fields(full_name=None, raw_ocr_text="blank or unrelated photo")


# --------------------------------------------------------------------------
# .delay() capture helpers — same pattern as
# test_07_data_enrichment.py::_patch_enrich_delay and the reprocess-flow
# patch in test_05_parsing_visiting_card.py, generalized to also capture
# kwargs for consistency across both call sites this feature enqueues.
# --------------------------------------------------------------------------


def _patch_process_delay(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple, dict]]:
    captured: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "app.services.card_service.process_card.delay",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )
    return captured


def _patch_enrich_delay(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple, dict]]:
    captured: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "app.services.card_service.enrich_company_task.delay",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )
    return captured


# ==========================================================================
# 1. POST /cards/process with no card_ids — unchanged pre-feature behavior.
# ==========================================================================


def test_process_cards_without_card_ids_enqueues_only_status_new_cards_in_scope(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: 'POST /cards/process with no card_ids behaves exactly as before
    (all status="new" cards in scope, optionally by exhibition_id).' Two
    cards are in scope — one left status="new", one already driven to
    status="extracted" — only the "new" one may be enqueued."""
    _authenticated_user(client, fake_otp_provider)
    new_card_id = _upload_one(client, jpeg_bytes, filename="new.jpg")
    extracted_card_id = _upload_one(client, jpeg_bytes, filename="extracted.jpg")

    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Already Extracted Contact",
            company_name=_unique_company_name("Already Extracted Co"),
        ),
    )
    process_card(extracted_card_id)

    extracted_row = db_session.get(VisitingCard, uuid.UUID(extracted_card_id))
    assert extracted_row.status == "extracted", "fixture setup: this card must already be extracted"
    new_row = db_session.get(VisitingCard, uuid.UUID(new_card_id))
    assert new_row.status == "new", "fixture setup: this card must remain status='new'"

    captured = _patch_process_delay(monkeypatch)

    resp = client.post("/cards/process", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enqueued_count": 1}, (
        "with no card_ids, only the single status='new' card in scope must be counted"
    )
    assert len(captured) == 1, "exactly one process_card.delay call must be made"
    assert captured[0][0] == (new_card_id,), (
        f"process_card.delay must be enqueued for the status='new' card only, got {captured[0][0]!r}"
    )
    for args, _kwargs in captured:
        assert extracted_card_id not in args, (
            "an already-extracted card must never be passed to process_card.delay"
        )


# ==========================================================================
# 2. POST /cards/process with card_ids — filters to the eligible+visible
#    subset only, silently excluding wrong-status and wrong-org ids.
# ==========================================================================


def test_process_cards_with_card_ids_only_enqueues_eligible_and_visible_subset(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: 'POST /cards/process with card_ids naming a mix of eligible and
    ineligible (wrong status, or another org's) ids only enqueues the
    eligible-and-visible subset, and enqueued_count matches that subset's
    size exactly.'"""
    _authenticated_user(client, fake_otp_provider)
    eligible_id = _upload_one(client, jpeg_bytes, filename="eligible.jpg")
    ineligible_status_id = _upload_one(client, jpeg_bytes, filename="ineligible-status.jpg")

    _patch_vision(
        monkeypatch,
        _fields(full_name="Wrong Status Contact", company_name=_unique_company_name("Wrong Status Co")),
    )
    process_card(ineligible_status_id)
    assert db_session.get(VisitingCard, uuid.UUID(ineligible_status_id)).status == "extracted", (
        "fixture setup: this card must not be status='new' by the time card_ids is submitted"
    )

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        other_org_id = _upload_one(other_client, jpeg_bytes, filename="other-org.jpg")

    captured = _patch_process_delay(monkeypatch)

    resp = client.post(
        "/cards/process",
        json={"card_ids": [eligible_id, ineligible_status_id, other_org_id]},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enqueued_count": 1}, (
        "only the eligible-and-visible id in the submitted list must be counted"
    )
    assert len(captured) == 1, "exactly one process_card.delay call must be made"
    assert captured[0][0] == (eligible_id,)
    for args, _kwargs in captured:
        assert ineligible_status_id not in args, (
            "a non-'new' card named in card_ids must never be passed to process_card.delay"
        )
        assert other_org_id not in args, (
            "another org's card named in card_ids must never be passed to process_card.delay"
        )


# ==========================================================================
# 3. POST /cards/enrich-companies — pending / already-enriched / no-company /
#    nonexistent mix.
# ==========================================================================


def test_enrich_companies_enqueues_only_the_pending_company_card_among_four_ineligible_variants(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: 'POST /cards/enrich-companies with a list containing one
    pending-company card, one already-enriched-company card, one card with no
    company, and one nonexistent card id returns {enqueued_count: 1,
    skipped_count: 3}, and exactly one enrich_company_task.delay call is
    made.'"""
    _authenticated_user(client, fake_otp_provider)

    pending_card_id = _upload_one(client, jpeg_bytes, filename="pending.jpg")
    _patch_vision(
        monkeypatch,
        _fields(full_name="Pending Contact", company_name=_unique_company_name("Pending Enrich Co")),
    )
    process_card(pending_card_id)
    pending_card = db_session.get(VisitingCard, uuid.UUID(pending_card_id))
    assert pending_card.company_id is not None, "fixture setup: extraction must have linked a company"
    pending_company = db_session.get(Company, pending_card.company_id)
    assert pending_company.enrichment_status == "pending", (
        "fixture setup: a freshly extracted company must start 'pending'"
    )

    already_enriched_card_id = _upload_one(client, jpeg_bytes, filename="already-enriched.jpg")
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Already Enriched Contact",
            company_name=_unique_company_name("Already Enriched Co"),
        ),
    )
    process_card(already_enriched_card_id)
    already_enriched_card = db_session.get(VisitingCard, uuid.UUID(already_enriched_card_id))
    already_enriched_company = db_session.get(Company, already_enriched_card.company_id)
    already_enriched_company.enrichment_status = "enriched"
    db_session.commit()

    no_company_card_id = _upload_one(client, jpeg_bytes, filename="no-company.jpg")
    _patch_vision(monkeypatch, _empty_fields())
    process_card(no_company_card_id)
    no_company_card = db_session.get(VisitingCard, uuid.UUID(no_company_card_id))
    assert no_company_card.status == "failed"
    assert no_company_card.company_id is None, "fixture setup: this card must have no linked company"

    nonexistent_card_id = str(uuid.uuid4())

    captured = _patch_enrich_delay(monkeypatch)

    resp = client.post(
        "/cards/enrich-companies",
        json={
            "card_ids": [
                pending_card_id,
                already_enriched_card_id,
                no_company_card_id,
                nonexistent_card_id,
            ]
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enqueued_count": 1, "skipped_count": 3}
    assert len(captured) == 1, "exactly one enrich_company_task.delay call must be made"
    assert captured[0] == ((str(pending_card.company_id), pending_card_id), {}), (
        f"enrich_company_task.delay must be enqueued with (company_id, card_id) for the pending "
        f"card only, got {captured[0]!r}"
    )


# ==========================================================================
# 4. POST /cards/enrich-companies — two cards sharing one still-pending
#    company are deduplicated to a single enqueue.
# ==========================================================================


def test_enrich_companies_dedupes_two_cards_sharing_the_same_pending_company(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: 'POST /cards/enrich-companies with two card ids that share the
    same still-"pending" company_id enqueues that company exactly once
    (enqueued_count: 1, skipped_count: 1), never twice.'"""
    _authenticated_user(client, fake_otp_provider)
    shared_company_name = _unique_company_name("Shared Pending Co")

    card_a_id = _upload_one(client, jpeg_bytes, filename="a.jpg")
    _patch_vision(
        monkeypatch,
        _fields(full_name="Contact A", company_name=shared_company_name),
    )
    process_card(card_a_id)

    card_b_id = _upload_one(client, jpeg_bytes, filename="b.jpg")
    _patch_vision(
        monkeypatch,
        _fields(full_name="Contact B", company_name=shared_company_name),
    )
    process_card(card_b_id)

    card_a = db_session.get(VisitingCard, uuid.UUID(card_a_id))
    card_b = db_session.get(VisitingCard, uuid.UUID(card_b_id))
    assert card_a.company_id is not None and card_b.company_id is not None
    assert card_a.company_id == card_b.company_id, (
        "fixture setup: both cards must resolve to the same shared Company row"
    )
    company = db_session.get(Company, card_a.company_id)
    assert company.enrichment_status == "pending", "fixture setup: the shared company must still be pending"

    captured = _patch_enrich_delay(monkeypatch)

    resp = client.post("/cards/enrich-companies", json={"card_ids": [card_a_id, card_b_id]})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enqueued_count": 1, "skipped_count": 1}
    assert len(captured) == 1, (
        "the shared company must be enqueued exactly once, never twice, across both card ids"
    )
    assert captured[0] == ((str(card_a.company_id), card_a_id), {}), (
        "the first card id in the request mapping to the not-yet-seen company must be the one "
        f"enqueued, got {captured[0]!r}"
    )


# ==========================================================================
# 5. POST /cards/enrich-companies — another org's card is skipped exactly
#    like a nonexistent id, never a 403/500.
# ==========================================================================


def test_enrich_companies_for_another_orgs_card_is_skipped_not_raised(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: 'POST /cards/enrich-companies for a card belonging to another org
    is skipped (not raised, not enqueued) exactly like a nonexistent id — no
    403/500.' The other org's card is driven to a genuine pending-company
    state first, so this test isolates the visibility gate specifically
    (rather than the also-true but less interesting "no company" gate)."""
    _authenticated_user(client, fake_otp_provider)

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        their_card_id = _upload_one(other_client, jpeg_bytes)
        _patch_vision(
            monkeypatch,
            _fields(full_name="Owner Contact", company_name=_unique_company_name("Owner Only Enrich Co")),
        )
        process_card(their_card_id)
        their_card = db_session.get(VisitingCard, uuid.UUID(their_card_id))
        assert their_card.company_id is not None, "fixture setup: extraction must have linked a company"
        assert db_session.get(Company, their_card.company_id).enrichment_status == "pending", (
            "fixture setup: the other org's company must still be pending, isolating the "
            "visibility gate as the only reason this id gets skipped"
        )

    captured = _patch_enrich_delay(monkeypatch)

    resp = client.post("/cards/enrich-companies", json={"card_ids": [their_card_id]})

    assert resp.status_code == 200, (
        f"a card belonging to another org must be silently skipped, never a 403/500, "
        f"got {resp.status_code}: {resp.text}"
    )
    assert resp.json() == {"enqueued_count": 0, "skipped_count": 1}
    assert captured == [], "another org's card must never be enqueued for enrichment"


# ==========================================================================
# 6. POST /cards/enrich-companies — empty card_ids -> 422 (Pydantic level).
# ==========================================================================


def test_enrich_companies_with_empty_card_ids_returns_422(client, fake_otp_provider, monkeypatch):
    """DoD: 'POST /cards/enrich-companies with an empty card_ids list returns
    422.' This must be a Pydantic-level rejection before any DB access, so no
    fixture card is even created for this test."""
    _authenticated_user(client, fake_otp_provider)
    captured = _patch_enrich_delay(monkeypatch)

    resp = client.post("/cards/enrich-companies", json={"card_ids": []})

    assert resp.status_code == 422, resp.text
    assert captured == [], "an empty card_ids list must never reach the enqueue loop"


# ==========================================================================
# 7. GET /cards — company_id / company_enrichment_status on every row.
# ==========================================================================


def test_list_cards_includes_company_id_and_company_enrichment_status(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: 'GET /cards includes company_id/company_enrichment_status on
    every row, null for a card with no linked company.'"""
    _authenticated_user(client, fake_otp_provider)

    no_company_card_id = _upload_one(client, jpeg_bytes, filename="no-company.jpg")
    _patch_vision(monkeypatch, _empty_fields())
    process_card(no_company_card_id)
    assert db_session.get(VisitingCard, uuid.UUID(no_company_card_id)).company_id is None, (
        "fixture setup: this card must have no linked company"
    )

    with_company_card_id = _upload_one(client, jpeg_bytes, filename="with-company.jpg")
    _patch_vision(
        monkeypatch,
        _fields(full_name="Listed Contact", company_name=_unique_company_name("Listed Company Co")),
    )
    process_card(with_company_card_id)
    with_company_card = db_session.get(VisitingCard, uuid.UUID(with_company_card_id))
    assert with_company_card.company_id is not None, "fixture setup: extraction must have linked a company"

    resp = client.get("/cards")
    assert resp.status_code == 200, resp.text
    by_id = {c["card_id"]: c for c in resp.json()}

    assert "company_id" in by_id[no_company_card_id]
    assert by_id[no_company_card_id]["company_id"] is None
    assert "company_enrichment_status" in by_id[no_company_card_id]
    assert by_id[no_company_card_id]["company_enrichment_status"] is None, (
        "a card with no linked company must expose company_enrichment_status=null"
    )

    assert by_id[with_company_card_id]["company_id"] == str(with_company_card.company_id), (
        "a card with a linked company must expose that company's real id"
    )
    assert by_id[with_company_card_id]["company_enrichment_status"] == "pending", (
        "company_enrichment_status must mirror the linked Company's enrichment_status"
    )


# ==========================================================================
# 8. Auth guard — both endpoints require a session.
# ==========================================================================


@pytest.mark.parametrize(
    "path, body",
    [
        ("/cards/process", {}),
        ("/cards/enrich-companies", {"card_ids": [str(uuid.uuid4())]}),
    ],
)
def test_process_and_enrich_companies_without_session_return_401(client, path: str, body: dict):
    resp = client.post(path, json=body)
    assert resp.status_code == 401, (
        f"POST {path} without a session must return 401, got {resp.status_code}: {resp.text}"
    )
