"""
Tests for the `13-bulk-delete-cards` feature (spec:
`.claude/specs/13-bulk-delete-cards.md`).

These tests are written directly against the spec's documented contract, not
against the implementation of `routers/cards.py::bulk_delete_cards` or
`services/card_service.py::bulk_delete_cards`. `apps/api/app/` was read only
to learn this repo's real schema/fixture/routing conventions (request/response
shapes, cookie-based auth, `scope_to_visible_users`, the `client`/`db_session`
fixtures) — never to derive what a test should assert.

Contract under test, per spec:
- `POST /cards/bulk-delete`, body `CardBulkDeleteRequest {card_ids: list[UUID]
  (min_length=1, max_length=200), confirm_cascade: bool = false}`. Response
  `CardBulkDeleteResponse {deleted_count, skipped_count}`, `200` on success.
- Auth-required (same cookie session as every other `/cards` route); org/owner
  visibility via `scope_to_visible_users`.
- `card_ids` not visible to the caller (wrong owner, different org, or
  nonexistent) are silently skipped and counted in `skipped_count` — the
  request never fails outright because of them (best-effort batch semantics,
  mirroring `POST /cards/enrich-companies`/`POST /cards/score`).
- If any *selected* card has a merged/duplicate child (`merged_into_card_id`
  pointing at it) that is NOT itself part of the selection, and
  `confirm_cascade` is omitted/`false`: `409` with `{message, child_count}`
  aggregating the extra-child count across the *whole* batch, and NOTHING in
  the batch is deleted (not even other selected cards with no cascade issue
  of their own).
- Resending the same request with `confirm_cascade=true` deletes both the
  originally selected cards and their extra merged children.
- A child already included in the selection needs no cascade confirmation at
  all — selecting a parent and its child together deletes both directly.
- A concurrent state change between the lookup and the commit can also
  produce a `409`, independently retryable — this is the same
  `CardStateChangedError` contract `DELETE /cards/{card_id}` already
  documents (spec: "09-bulk-select..."/"08-delete-card" dependency section).
- Deleting a card also removes its storage object; `card_emails`/`card_phones`
  cascade at the DB level (`ON DELETE CASCADE`), same as single-card delete.

Mocking strategy: `vision_client.extract_card_fields` is mocked purely as
test setup (via `process_card`), never a real network/Anthropic call, matching
every other file in this suite. Storage assertions run against real local
MinIO (`storage_service.download_file` raising `ClientError` post-delete),
matching `test_08_delete_card.py`'s/`test_08-delete-card.py`'s established
"real local infra over mocks" convention for this repo — `storage_service`
itself is not mocked/monkeypatched anywhere in this suite for delete
assertions, so this file follows that same precedent rather than mocking
`storage_service.delete_file`.

Judgment calls made in the absence of explicit spec text (documented inline
at point of use too):
  1. **Merged-child relationships are constructed via `_set_card_fields`**
     (direct ORM mutation), matching `test_08_delete_card.py`'s (underscore
     file) convention — these tests care about `bulk_delete_cards`'s
     cascade-aggregation/atomicity/ordering behavior given a
     `merged_into_card_id` relationship, not about how that relationship
     organically arises (already covered by `test_05_parsing_visiting_card.py`
     and `test_08*delete_card.py`'s real-merge variants).
  2. **The concurrent-state (`CardStateChangedError`) `409` path** is not
     exercised by either `test_08_delete_card.py` or
     `test_08-delete-card.py` (neither file has a test for it — confirmed by
     reading both), so there is no existing pattern to mirror exactly. It's
     simulated here by monkeypatching `sqlalchemy.orm.Session.commit` to
     raise a real `sqlalchemy.exc.IntegrityError` for the one commit inside
     the request under test, which is exactly the condition
     `card_service.bulk_delete_cards`'s spec'd `try/except IntegrityError`
     handling is documented to translate into `CardStateChangedError` / `409`.
     This deterministically reproduces the DB-level race the spec describes
     without needing two real concurrent requests or `time.sleep()`.
  3. **True cross-organization isolation** for the merged-children lookup is
     left as a documented gap, identical in cause to the admin-visibility
     skips already present in `test_04`/`test_05`/`test_08*`:
     `02-user-registration` only ever produces `org_id=NULL, role=NULL`
     accounts, and no conftest helper exists yet to put two users through a
     real multi-org flow. The closest available proxy — a cross-*owner*
     (same org-less scope) merged child, mirroring
     `test_08_delete_card.py::test_delete_card_cascades_to_child_owned_by_different_user`
     — is exercised directly below and documented as satisfying the
     "merged-children lookup is unscoped by the deleter's own user_id, but
     still only ever reachable from an already-authorized parent" invariant
     the security review asked about, given the fixtures that actually exist.
"""

from __future__ import annotations

import io
import uuid

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from app.main import app as fastapi_app
from app.models.card_email import CardEmail
from app.models.card_phone import CardPhone
from app.models.visiting_card import VisitingCard
from app.services import storage_service
from app.workers.card_processing import process_card
from conftest import create_verified_user

VALID_PHONE = "+14155552671"
BULK_DELETE_URL = "/cards/bulk-delete"


# --------------------------------------------------------------------------
# Image bytes — real, Pillow-decodable JPEGs, matching this repo's
# established convention (never placeholder bytes for a "valid" case).
# --------------------------------------------------------------------------


def _image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (20, 20), color: str = "purple") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture
def jpeg_bytes() -> bytes:
    return _image_bytes()


# --------------------------------------------------------------------------
# Auth / upload / vision-mocking helpers — copied from this repo's
# established per-file convention (test_04/test_05/test_08*), since there is
# no shared conftest version of these yet.
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


def _upload_n(client: TestClient, jpeg_bytes: bytes, n: int, prefix: str = "card") -> list[str]:
    resp = _upload_files(
        client, [(f"{prefix}{i}.jpg", jpeg_bytes, "image/jpeg") for i in range(n)]
    )
    assert resp.status_code == 201, resp.text
    return [c["card_id"] for c in resp.json()["cards"]]


def _patch_vision(monkeypatch: pytest.MonkeyPatch, *responses) -> list[tuple[bytes, str]]:
    """Each call to `extract_card_fields` consumes the next entry in
    `responses`, in order."""
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


def _set_card_fields(db_session, card_id: str, **fields) -> None:
    """Directly sets fields on a card row via the ORM — used here to
    construct a `merged_into_card_id` relationship deterministically without
    re-driving the full extraction pipeline, matching
    `test_05_parsing_visiting_card.py`'s/`test_08_delete_card.py`'s
    identically-named helper/convention for tests where the merge mechanism
    itself isn't under test."""
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    for key, value in fields.items():
        setattr(card, key, value)
    db_session.commit()


def _bulk_delete(client: TestClient, card_ids: list[str], confirm_cascade: bool | None = None):
    body: dict = {"card_ids": card_ids}
    if confirm_cascade is not None:
        body["confirm_cascade"] = confirm_cascade
    return client.post(BULK_DELETE_URL, json=body)


def _still_exists(db_session, card_id: str) -> bool:
    return db_session.get(VisitingCard, uuid.UUID(card_id)) is not None


# --------------------------------------------------------------------------
# 1. Auth guard / validation.
# --------------------------------------------------------------------------


def test_bulk_delete_without_session_returns_401():
    with TestClient(fastapi_app) as anon_client:
        resp = _bulk_delete(anon_client, [str(uuid.uuid4())])
        assert resp.status_code == 401, resp.text


def test_bulk_delete_empty_card_ids_returns_422(client, fake_otp_provider):
    """Spec: `card_ids: list[UUID] (min_length=1, ...)` — an empty selection
    must be rejected at the schema level, not treated as a zero-length
    best-effort batch."""
    _authenticated_user(client, fake_otp_provider)
    resp = _bulk_delete(client, [])
    assert resp.status_code == 422, resp.text


def test_bulk_delete_missing_card_ids_field_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    resp = client.post(BULK_DELETE_URL, json={})
    assert resp.status_code == 422, resp.text


def test_bulk_delete_more_than_200_card_ids_returns_422(client, fake_otp_provider):
    """Spec: `max_length=200`, 'same cap as CardEnrichRequest/CardScoreRequest
    /CardExportRequest' — 201 ids must be rejected before any DB work."""
    _authenticated_user(client, fake_otp_provider)
    card_ids = [str(uuid.uuid4()) for _ in range(201)]
    resp = _bulk_delete(client, card_ids)
    assert resp.status_code == 422, resp.text


def test_bulk_delete_exactly_200_card_ids_is_schema_valid(client, fake_otp_provider):
    """The boundary itself (200, inclusive) must NOT be rejected by
    validation — distinct from the 201 over-cap case above. None of these ids
    are real/visible, so the request should still succeed with everything
    skipped, proving validation passed and the best-effort path ran."""
    _authenticated_user(client, fake_otp_provider)
    card_ids = [str(uuid.uuid4()) for _ in range(200)]
    resp = _bulk_delete(client, card_ids)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted_count"] == 0
    assert body["skipped_count"] == 200


def test_bulk_delete_malformed_card_id_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    resp = client.post(BULK_DELETE_URL, json={"card_ids": ["not-a-uuid"]})
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------
# 2. Happy path — visible cards, no merged children.
# --------------------------------------------------------------------------


def test_bulk_delete_happy_path_removes_rows_and_cascaded_email_phone_rows(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: a selection of visible cards with no merged children deletes all
    of them and returns {deleted_count, skipped_count: 0}; card_emails/
    card_phones for the extracted card are gone too (DB ON DELETE CASCADE)."""
    _authenticated_user(client, fake_otp_provider)
    card_ids = _upload_n(client, jpeg_bytes, 3)
    extracted_id = card_ids[0]
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Bulk Delete Contact",
            company_name="Bulk Delete Happy Path Co",
            emails=[{"email": "bulk@example.com", "email_type": "work"}],
            phones=[{"phone": VALID_PHONE, "phone_type": "mobile"}],
        ),
    )
    process_card(extracted_id)

    assert db_session.scalars(
        select(CardEmail).where(CardEmail.card_id == uuid.UUID(extracted_id))
    ).all(), "fixture setup: extraction must have produced an email row"
    assert db_session.scalars(
        select(CardPhone).where(CardPhone.card_id == uuid.UUID(extracted_id))
    ).all(), "fixture setup: extraction must have produced a phone row"

    resp = _bulk_delete(client, card_ids)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"deleted_count": 3, "skipped_count": 0}, body

    for cid in card_ids:
        assert not _still_exists(db_session, cid), f"card {cid} must be deleted"
    assert (
        db_session.scalars(select(CardEmail).where(CardEmail.card_id == uuid.UUID(extracted_id))).all()
        == []
    ), "card_emails rows for the deleted card must be gone too"
    assert (
        db_session.scalars(select(CardPhone).where(CardPhone.card_id == uuid.UUID(extracted_id))).all()
        == []
    ), "card_phones rows for the deleted card must be gone too"

    # Also observable through the API, staying implementation-agnostic.
    listing = client.get("/cards")
    assert listing.status_code == 200, listing.text
    listed_ids = {c["card_id"] for c in listing.json()}
    assert listed_ids.isdisjoint(card_ids)


def test_bulk_delete_removes_storage_objects_for_every_deleted_card(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """DoD: bulk-deleting a selection with no merged children deletes their
    storage objects too, not just the DB rows."""
    _authenticated_user(client, fake_otp_provider)
    card_ids = _upload_n(client, jpeg_bytes, 2)
    keys = []
    for cid in card_ids:
        card = db_session.get(VisitingCard, uuid.UUID(cid))
        assert card.image_url
        keys.append(card.image_url)
        # Sanity: the object exists before delete.
        assert storage_service.download_file(card.image_url) == jpeg_bytes

    resp = _bulk_delete(client, card_ids)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted_count": 2, "skipped_count": 0}

    for key in keys:
        with pytest.raises(ClientError):
            storage_service.download_file(key)


# --------------------------------------------------------------------------
# 3. Not-visible / cross-user ids are silently skipped (best-effort batch,
#    and the highest-value tenant-isolation coverage for this endpoint).
# --------------------------------------------------------------------------


def test_bulk_delete_skips_id_not_visible_to_caller_without_failing_batch(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """DoD: a card_ids list containing an id not visible to the current user
    is silently skipped — skipped_count reflects it, response is still 200,
    and the request is not failed outright. Also the highest-value tenant
    isolation test for this resource: another user's card must never be
    deleted as a side effect of being included in someone else's batch, and
    must not affect deleted_count for the caller's own cards."""
    _authenticated_user(client, fake_otp_provider)
    own_ids = _upload_n(client, jpeg_bytes, 2)

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        theirs_id = _upload_one(other_client, jpeg_bytes)

    nonexistent_id = str(uuid.uuid4())

    resp = _bulk_delete(client, [*own_ids, theirs_id, nonexistent_id])

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted_count"] == 2, (
        f"deleted_count must count only the caller's own visible cards, got {body!r}"
    )
    assert body["skipped_count"] == 2, (
        f"skipped_count must reflect both the cross-owner id and the nonexistent id, got {body!r}"
    )

    for cid in own_ids:
        assert not _still_exists(db_session, cid), "the caller's own selected cards must be deleted"
    assert _still_exists(db_session, theirs_id), (
        "a card belonging to another user must never be deleted via someone else's bulk-delete request"
    )


def test_bulk_delete_only_ids_not_visible_to_caller_deletes_nothing(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """A batch made up entirely of ids the caller cannot see must still
    succeed (200) with deleted_count=0 and skipped_count matching every id —
    never a 404/failure for the whole request."""
    _authenticated_user(client, fake_otp_provider)

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        theirs_id = _upload_one(other_client, jpeg_bytes)

    resp = _bulk_delete(client, [theirs_id, str(uuid.uuid4())])

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted_count": 0, "skipped_count": 2}
    assert _still_exists(db_session, theirs_id)


# --------------------------------------------------------------------------
# 4. Cascade confirmation — extra merged children outside the selection.
# --------------------------------------------------------------------------


def test_bulk_delete_with_extra_merged_child_outside_selection_returns_409_and_deletes_nothing(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """DoD: if any selected card has a merged/duplicate child outside the
    selection and confirm_cascade is omitted, the response is 409 with the
    total extra-child count, and no card is deleted — including OTHER
    selected cards in the same batch that have no cascade issue of their
    own (proves the rejection is atomic across the whole batch, not just the
    card with the child)."""
    _authenticated_user(client, fake_otp_provider)
    parent_id = _upload_one(client, jpeg_bytes, filename="parent.jpg")
    child_id = _upload_one(client, jpeg_bytes, filename="child.jpg")
    unrelated_id = _upload_one(client, jpeg_bytes, filename="unrelated.jpg")
    _set_card_fields(db_session, child_id, status="merged", merged_into_card_id=uuid.UUID(parent_id))

    resp = _bulk_delete(client, [parent_id, unrelated_id])

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["detail"]["child_count"] == 1, (
        f"child_count must reflect the one extra child outside the selection, got {body!r}"
    )
    assert "message" in body["detail"], "the 409 body must carry a human-readable message too"

    for cid in (parent_id, child_id, unrelated_id):
        assert _still_exists(db_session, cid), (
            f"a rejected cascade bulk-delete must leave card {cid} untouched, including cards "
            f"in the same batch that had no cascade issue of their own"
        )


def test_bulk_delete_confirm_cascade_explicit_false_still_returns_409(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """Spec: confirm_cascade defaults to false; passing it explicitly as
    false with an extra merged child present must behave identically to
    omitting it."""
    _authenticated_user(client, fake_otp_provider)
    parent_id = _upload_one(client, jpeg_bytes, filename="parent.jpg")
    child_id = _upload_one(client, jpeg_bytes, filename="child.jpg")
    _set_card_fields(db_session, child_id, status="duplicate", merged_into_card_id=uuid.UUID(parent_id))

    resp = _bulk_delete(client, [parent_id], confirm_cascade=False)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["child_count"] == 1
    assert _still_exists(db_session, parent_id)
    assert _still_exists(db_session, child_id)


def test_bulk_delete_child_count_aggregates_across_multiple_selected_parents(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """The 409's child_count is the aggregate extra-child count across the
    WHOLE batch — verified with two different selected parents, each with
    one extra child outside the selection, totalling 2."""
    _authenticated_user(client, fake_otp_provider)
    parent_1_id = _upload_one(client, jpeg_bytes, filename="parent1.jpg")
    parent_2_id = _upload_one(client, jpeg_bytes, filename="parent2.jpg")
    child_1_id = _upload_one(client, jpeg_bytes, filename="child1.jpg")
    child_2_id = _upload_one(client, jpeg_bytes, filename="child2.jpg")
    _set_card_fields(db_session, child_1_id, status="merged", merged_into_card_id=uuid.UUID(parent_1_id))
    _set_card_fields(db_session, child_2_id, status="duplicate", merged_into_card_id=uuid.UUID(parent_2_id))

    resp = _bulk_delete(client, [parent_1_id, parent_2_id])

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["child_count"] == 2, (
        "child_count must sum extra children across every selected parent in the batch, not just one"
    )
    for cid in (parent_1_id, parent_2_id, child_1_id, child_2_id):
        assert _still_exists(db_session, cid)


def test_bulk_delete_resend_with_confirm_cascade_true_deletes_selection_and_extra_children(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """DoD: resending the same request with confirm_cascade=true deletes both
    the originally selected cards and their extra merged children."""
    _authenticated_user(client, fake_otp_provider)
    parent_id = _upload_one(client, jpeg_bytes, filename="parent.jpg")
    child_id = _upload_one(client, jpeg_bytes, filename="child.jpg")
    unrelated_id = _upload_one(client, jpeg_bytes, filename="unrelated.jpg")
    _set_card_fields(db_session, child_id, status="merged", merged_into_card_id=uuid.UUID(parent_id))

    guarded = _bulk_delete(client, [parent_id, unrelated_id])
    assert guarded.status_code == 409, guarded.text

    resp = _bulk_delete(client, [parent_id, unrelated_id], confirm_cascade=True)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted_count"] == 3, (
        f"deleted_count must include the 2 originally-selected cards plus the 1 extra merged "
        f"child, got {body!r}"
    )
    assert body["skipped_count"] == 0

    for cid in (parent_id, child_id, unrelated_id):
        assert not _still_exists(db_session, cid), f"card {cid} must be deleted after confirm_cascade=true"


def test_bulk_delete_confirm_cascade_true_removes_storage_for_extra_children_too(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """Storage cleanup must cover extra cascaded children as well as the
    originally-selected cards, not just the latter."""
    _authenticated_user(client, fake_otp_provider)
    parent_id = _upload_one(client, jpeg_bytes, filename="parent.jpg")
    child_id = _upload_one(client, jpeg_bytes, filename="child.jpg")
    _set_card_fields(db_session, child_id, status="merged", merged_into_card_id=uuid.UUID(parent_id))

    parent_key = db_session.get(VisitingCard, uuid.UUID(parent_id)).image_url
    child_key = db_session.get(VisitingCard, uuid.UUID(child_id)).image_url
    assert parent_key and child_key

    resp = _bulk_delete(client, [parent_id], confirm_cascade=True)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted_count": 2, "skipped_count": 0}

    with pytest.raises(ClientError):
        storage_service.download_file(parent_key)
    with pytest.raises(ClientError):
        storage_service.download_file(child_key)


def test_bulk_delete_confirm_cascade_true_with_no_children_at_all_behaves_like_default(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """confirm_cascade=true on a selection with no merged children of any
    kind must behave identically to the default (200, all deleted), not
    error or no-op — mirrors DELETE /cards/{card_id}'s equivalent rule."""
    _authenticated_user(client, fake_otp_provider)
    card_ids = _upload_n(client, jpeg_bytes, 2)

    resp = _bulk_delete(client, card_ids, confirm_cascade=True)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted_count": 2, "skipped_count": 0}
    for cid in card_ids:
        assert not _still_exists(db_session, cid)


# --------------------------------------------------------------------------
# 5. A child already included in the selection needs no cascade confirmation
#    — the genuinely new piece of logic vs. single-card delete.
# --------------------------------------------------------------------------


def test_bulk_delete_child_already_in_selection_needs_no_cascade_confirmation(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """DoD: a child already included in the selection needs no cascade
    confirmation — selecting both a parent and its merged child together
    must delete both directly, with no 409/cascade prompt at all."""
    _authenticated_user(client, fake_otp_provider)
    parent_id = _upload_one(client, jpeg_bytes, filename="parent.jpg")
    child_id = _upload_one(client, jpeg_bytes, filename="child.jpg")
    _set_card_fields(db_session, child_id, status="merged", merged_into_card_id=uuid.UUID(parent_id))

    resp = _bulk_delete(client, [parent_id, child_id])

    assert resp.status_code == 200, (
        f"selecting a parent and its own merged child together must not require cascade "
        f"confirmation, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body == {"deleted_count": 2, "skipped_count": 0}, body

    assert not _still_exists(db_session, parent_id)
    assert not _still_exists(db_session, child_id)


def test_bulk_delete_child_in_selection_plus_extra_child_outside_still_requires_confirmation_for_the_extra_only(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """When a parent has two children — one included in the selection, one
    not — only the child OUTSIDE the selection should require cascade
    confirmation; child_count must be 1, not 2."""
    _authenticated_user(client, fake_otp_provider)
    parent_id = _upload_one(client, jpeg_bytes, filename="parent.jpg")
    included_child_id = _upload_one(client, jpeg_bytes, filename="included-child.jpg")
    extra_child_id = _upload_one(client, jpeg_bytes, filename="extra-child.jpg")
    _set_card_fields(
        db_session, included_child_id, status="merged", merged_into_card_id=uuid.UUID(parent_id)
    )
    _set_card_fields(
        db_session, extra_child_id, status="merged", merged_into_card_id=uuid.UUID(parent_id)
    )

    resp = _bulk_delete(client, [parent_id, included_child_id])

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["child_count"] == 1, (
        "only the child NOT already in the selection should count toward the cascade confirmation"
    )
    for cid in (parent_id, included_child_id, extra_child_id):
        assert _still_exists(db_session, cid)

    confirmed = _bulk_delete(client, [parent_id, included_child_id], confirm_cascade=True)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json() == {"deleted_count": 3, "skipped_count": 0}
    for cid in (parent_id, included_child_id, extra_child_id):
        assert not _still_exists(db_session, cid)


# --------------------------------------------------------------------------
# 6. Merged-children lookup is unscoped by the deleter's own user_id, but
#    still only ever reachable from an already-authorized/visible parent.
#    Mirrors test_08_delete_card.py's cross-owner precedent.
# --------------------------------------------------------------------------


def test_bulk_delete_cascade_detects_and_deletes_merged_child_owned_by_a_different_user(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """A duplicate/back-of-card match can legitimately span owners (see
    test_08_delete_card.py's identical precedent) — the merged-children
    lookup must NOT be scoped to the deleting user's own visibility, or it
    under-counts a child it doesn't own and the parent delete then hits an
    FK violation. This also demonstrates the lookup can only ever surface
    children of a card the caller was already authorized to select in the
    first place — it never lets an unrelated user's *unmerged* card get
    deleted or counted (that's covered separately by the tenant-isolation
    tests above, which show a plain cross-owner id is only ever skipped,
    never cascaded into)."""
    _authenticated_user(client, fake_otp_provider)
    parent_id = _upload_one(client, jpeg_bytes, filename="parent.jpg")

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        cross_owner_child_id = _upload_one(other_client, jpeg_bytes, filename="child.jpg")

    _set_card_fields(
        db_session, cross_owner_child_id, merged_into_card_id=uuid.UUID(parent_id), status="duplicate"
    )

    # Without confirm_cascade: the cross-owner child must still be detected.
    resp = _bulk_delete(client, [parent_id])
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["child_count"] == 1
    assert _still_exists(db_session, parent_id)
    assert _still_exists(db_session, cross_owner_child_id)

    # With confirm_cascade: both rows are removed, regardless of the owner
    # mismatch — the parent's own visibility check already authorized this.
    confirmed = _bulk_delete(client, [parent_id], confirm_cascade=True)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json() == {"deleted_count": 2, "skipped_count": 0}
    assert not _still_exists(db_session, parent_id)
    assert not _still_exists(db_session, cross_owner_child_id)


@pytest.mark.skip(
    reason=(
        "No conftest helper exists yet to put two users through a real "
        "multi-organization flow (02-user-registration only ever produces "
        "org_id=NULL, role=NULL accounts), so a genuine cross-org merged-child "
        "relationship can't be constructed without guessing at internal query "
        "scoping. test_bulk_delete_cascade_detects_and_deletes_merged_child_owned_by_a_different_user "
        "above exercises the closest available proxy (cross-owner, same "
        "org-less scope) and test_bulk_delete_skips_id_not_visible_to_caller_without_failing_batch "
        "proves a plain (non-merged) cross-owner card is only ever skipped, "
        "never touched — together these cover the spec'd invariant as far as "
        "current fixtures allow. Once an org-invite/admin fixture exists, add "
        "a true cross-org variant here."
    )
)
def test_merged_children_lookup_never_crosses_a_genuine_org_boundary():
    pass


# --------------------------------------------------------------------------
# 7. Concurrent-state 409 (CardStateChangedError) — retryable.
# --------------------------------------------------------------------------


def test_bulk_delete_concurrent_state_change_returns_409_and_deletes_nothing(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Spec: 'A 409 can also occur if a concurrent request changes card state
    between the lookup and the commit (CardStateChangedError) — retryable.'
    Simulated deterministically by forcing the request's own commit to raise
    a real IntegrityError (see module docstring judgment call #2) — the
    condition the spec documents as translating into this 409. Nothing must
    be deleted, and the failure must be reported as retryable (409, not 500),
    proving the handler doesn't let a raw IntegrityError escape as an
    unhandled server error."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)

    def _raise_integrity_error(self, *args, **kwargs):
        raise IntegrityError(
            "INSERT/DELETE", {}, Exception("simulated concurrent merge FK violation")
        )

    monkeypatch.setattr(OrmSession, "commit", _raise_integrity_error)

    resp = _bulk_delete(client, [card_id])

    assert resp.status_code == 409, (
        f"a concurrent state change during commit must be surfaced as a retryable 409, "
        f"not a raw 500, got {resp.status_code}: {resp.text}"
    )

    monkeypatch.undo()  # restore Session.commit before touching db_session below
    assert _still_exists(db_session, card_id), (
        "a delete that failed at commit time (simulated concurrent state change) must not "
        "leave the card deleted"
    )
