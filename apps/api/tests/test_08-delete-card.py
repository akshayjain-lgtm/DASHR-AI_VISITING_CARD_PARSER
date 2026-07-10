"""
Tests for the `08-delete-card` feature (spec: `.claude/specs/08-delete-card.md`).

These tests are written directly against the spec's documented contract, not
against the implementation of `routers/cards.py::delete` or
`services/card_service.py::delete_card`:

- `DELETE /cards/{card_id}?confirm_cascade={bool}` permanently deletes a
  card. Auth-required, same visibility rule as `GET /cards/{card_id}` (owner,
  or org admin for a member's card). `confirm_cascade` defaults to `false`.
  `204 No Content` on success.
- `404` (`CardNotFoundError`) if the card doesn't exist or isn't visible to
  the caller.
- If other cards were merged into the target (`merged_into_card_id` pointing
  at it, from 05's back-of-card/duplicate folding) and `confirm_cascade` is
  not `true`: `409` with body `{"detail": {"message": ..., "child_count": N}}`
  and nothing is deleted.
- If `confirm_cascade=true` and children exist: parent + all children are
  deleted atomically (no FK violation).
- If there are no children, `confirm_cascade` is ignored — the card deletes
  immediately either way.
- After a successful delete, every deleted card's image is no longer
  retrievable from the configured S3/MinIO bucket at its stored key.
- `card_emails`/`card_phones` are removed too (DB-level `ON DELETE CASCADE`).
- `Company`/`CompanySignals` rows are never touched — shared reference data.

Mocking strategy: the only external boundary this feature's own code path
calls is `vision_client.extract_card_fields`, used purely as test setup (via
`process_card`) to produce a real company/emails/phones/merge relationship to
delete — never a real network/Anthropic call, matching every other file in
this suite. Storage assertions run against real local MinIO
(`storage_service.download_file` raising `ClientError` post-delete), matching
this repo's established "real local infra over mocks" philosophy from
`test_04_visiting_card_bulk_upload.py` / `test_05_parsing_visiting_card.py`.

Judgment calls made in the absence of explicit spec text (documented inline
at point of use too):
  1. **Merged-child setup mostly uses `_set_card_fields` directly** (matching
     `test_05_parsing_visiting_card.py`'s own convention for listing-style
     tests where the merge *mechanism* isn't what's under test) rather than
     driving a full front/back extraction merge every time — this feature's
     tests care about `delete_card`'s cascade/child-count/atomicity behavior
     given a `merged_into_card_id` relationship, not about how that
     relationship came to exist. One test (`test_delete_card_with_real_merge_relationship_cascade_removes_storage_for_both`)
     does drive a real merge via `process_card`, to prove the cascade also
     works end-to-end against an organically-produced relationship, not just
     a hand-constructed one.
  2. **Cross-owner merged children** (e.g. an org admin's merge spanning two
     different `user_id`s) are left untested here, for the same documented
     reason as the admin-visibility skip below: `02-user-registration` only
     ever produces `org_id=NULL, role=NULL` accounts, and no conftest helper
     exists to put a user through a real org-invite/admin flow. Fabricating
     that relationship via direct ORM manipulation would require guessing at
     `delete_card`'s internal child-lookup query scoping rather than
     asserting a spec-documented behavior, so it's omitted rather than
     invented.
  3. **`CompanySignals` retention** is proven by seeding a row directly via
     the ORM (07-data-enrichment's model) rather than driving a real
     `enrich_company_task` run, since only the "never deleted as a side
     effect" invariant is under test here, not enrichment itself.
"""

from __future__ import annotations

import io
import uuid

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.main import app as fastapi_app
from app.models.card_email import CardEmail
from app.models.card_phone import CardPhone
from app.models.company import Company
from app.models.company_signals import CompanySignals
from app.models.visiting_card import VisitingCard
from app.services import storage_service
from app.workers.card_processing import process_card
from conftest import create_verified_user

VALID_PHONE = "+14155552671"


# --------------------------------------------------------------------------
# Image bytes — real, Pillow-decodable JPEGs, matching this repo's
# established convention (never placeholder bytes for a "valid" case).
# --------------------------------------------------------------------------


def _image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (20, 20), color: str = "green") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture
def jpeg_bytes() -> bytes:
    return _image_bytes()


# --------------------------------------------------------------------------
# Auth / upload / vision-mocking helpers — copied from this repo's
# established per-file convention (test_04/test_05), since there is no
# shared conftest version of these yet.
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
    the back-of-card sibling lookup used by `_make_real_merged_pair`."""
    resp = _upload_files(
        client,
        [("front.jpg", jpeg_bytes, "image/jpeg"), ("back.jpg", jpeg_bytes, "image/jpeg")],
    )
    assert resp.status_code == 201, resp.text
    cards = resp.json()["cards"]
    return cards[0]["card_id"], cards[1]["card_id"]


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
    `test_05_parsing_visiting_card.py`'s identically-named helper/convention
    for listing-style tests where the merge mechanism itself isn't under
    test."""
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    for key, value in fields.items():
        setattr(card, key, value)
    db_session.commit()


def _make_real_merged_pair(
    client: TestClient, jpeg_bytes: bytes, monkeypatch: pytest.MonkeyPatch, company_name: str
) -> tuple[str, str]:
    """Drives a real front/back merge via `process_card`, mirroring
    `test_05_parsing_visiting_card.py::test_back_of_card_merges_onto_front_sibling_fill_gaps_only`,
    so at least one cascade test exercises an organically-produced
    `merged_into_card_id` relationship rather than only a hand-constructed
    one."""
    front_id, back_id = _upload_two(client, jpeg_bytes)

    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Karan Mehta",
            job_title="Director - Sales",
            company_name=company_name,
            emails=[{"email": "karan@example.com", "email_type": "work"}],
            phones=[{"phone": VALID_PHONE, "phone_type": "mobile"}],
        ),
    )
    process_card(front_id)

    _patch_vision(
        monkeypatch,
        _fields(is_back_of_card=True, full_name=None, address="Plot 7, Industrial Estate, Pune"),
    )
    process_card(back_id)

    return front_id, back_id


# --------------------------------------------------------------------------
# 1. Auth guard, validation, not-found, tenant isolation.
# --------------------------------------------------------------------------


def test_delete_card_without_session_returns_401():
    with TestClient(fastapi_app) as anon_client:
        resp = anon_client.delete(f"/cards/{uuid.uuid4()}")
        assert resp.status_code == 401, resp.text


def test_delete_card_malformed_card_id_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    resp = client.delete("/cards/not-a-uuid")
    assert resp.status_code == 422, resp.text


def test_delete_card_nonexistent_id_returns_404(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    resp = client.delete(f"/cards/{uuid.uuid4()}")
    assert resp.status_code == 404, resp.text


def test_delete_card_for_another_users_card_returns_404_and_leaves_row_untouched(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """Highest-value test class for this resource: a user must never be able
    to delete another user's card, and a rejected cross-tenant attempt must
    leave the row fully intact."""
    _authenticated_user(client, fake_otp_provider)

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        theirs_id = _upload_one(other_client, jpeg_bytes)

    resp = client.delete(f"/cards/{theirs_id}")

    assert resp.status_code == 404, (
        f"a user must never be able to delete another user's card, got {resp.status_code}: {resp.text}"
    )
    assert db_session.get(VisitingCard, uuid.UUID(theirs_id)) is not None, (
        "a rejected cross-tenant delete must leave the target card row untouched"
    )


# --------------------------------------------------------------------------
# 2. Happy path — single card, no merged children.
# --------------------------------------------------------------------------


def test_delete_card_happy_path_removes_row_and_cascaded_email_phone_rows(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: deleting a card owned by the caller with no merged children
    returns 204, and the row plus its card_emails/card_phones are gone from
    the database (DB-level ON DELETE CASCADE)."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Anita Rao",
            company_name="Delete Card Happy Path Co",
            emails=[{"email": "anita@example.com", "email_type": "work"}],
            phones=[{"phone": VALID_PHONE, "phone_type": "mobile"}],
        ),
    )
    process_card(card_id)

    # Sanity: the rows this test proves get cleaned up actually exist first.
    assert db_session.scalars(select(CardEmail).where(CardEmail.card_id == uuid.UUID(card_id))).all()
    assert db_session.scalars(select(CardPhone).where(CardPhone.card_id == uuid.UUID(card_id))).all()

    resp = client.delete(f"/cards/{card_id}")

    assert resp.status_code == 204, resp.text
    assert resp.content == b"", "a 204 No Content response must carry no body"

    assert db_session.get(VisitingCard, uuid.UUID(card_id)) is None, (
        "the visiting_cards row must be permanently removed"
    )
    assert db_session.scalars(select(CardEmail).where(CardEmail.card_id == uuid.UUID(card_id))).all() == [], (
        "card_emails rows for the deleted card must be gone too"
    )
    assert db_session.scalars(select(CardPhone).where(CardPhone.card_id == uuid.UUID(card_id))).all() == [], (
        "card_phones rows for the deleted card must be gone too"
    )

    # Also observable through the API, staying implementation-agnostic.
    listing = client.get("/cards")
    assert listing.status_code == 200, listing.text
    assert card_id not in {c["card_id"] for c in listing.json()}

    detail = client.get(f"/cards/{card_id}")
    assert detail.status_code == 404, "a deleted card must no longer be fetchable"


def test_delete_card_removes_storage_object_from_bucket(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """DoD: after a successful delete, the card's image is no longer
    retrievable from the configured S3/MinIO bucket at its stored key."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    key = card.image_url
    assert key, "fixture setup: an uploaded card must have a stored image key"
    # Sanity: the object is actually retrievable before delete.
    assert storage_service.download_file(key) == jpeg_bytes

    resp = client.delete(f"/cards/{card_id}")
    assert resp.status_code == 204, resp.text

    with pytest.raises(ClientError):
        storage_service.download_file(key)


# --------------------------------------------------------------------------
# 3. Cascade confirmation for merged children.
# --------------------------------------------------------------------------


def test_delete_card_with_merged_child_without_confirm_cascade_returns_409_and_deletes_nothing(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """DoD: deleting a card that has a merged child, called without
    confirm_cascade, returns 409 with child_count matching the actual number
    of children, and neither the parent nor the child row is deleted."""
    _authenticated_user(client, fake_otp_provider)
    parent_id = _upload_one(client, jpeg_bytes, filename="parent.jpg")
    child_id = _upload_one(client, jpeg_bytes, filename="child.jpg")
    _set_card_fields(db_session, child_id, status="merged", merged_into_card_id=uuid.UUID(parent_id))

    resp = client.delete(f"/cards/{parent_id}")

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["detail"]["child_count"] == 1, (
        f"child_count must match the actual number of merged children, got {body!r}"
    )
    assert "message" in body["detail"], "the 409 body must carry a human-readable message too"

    assert db_session.get(VisitingCard, uuid.UUID(parent_id)) is not None, "parent must not be deleted"
    assert db_session.get(VisitingCard, uuid.UUID(child_id)) is not None, "child must not be deleted"


def test_delete_card_with_merged_child_confirm_cascade_explicit_false_still_returns_409(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """Spec: confirm_cascade defaults to false; passing it explicitly as
    false with a merged child present must behave identically to omitting
    it — still 409, still deletes nothing."""
    _authenticated_user(client, fake_otp_provider)
    parent_id = _upload_one(client, jpeg_bytes, filename="parent.jpg")
    child_id = _upload_one(client, jpeg_bytes, filename="child.jpg")
    _set_card_fields(db_session, child_id, status="duplicate", merged_into_card_id=uuid.UUID(parent_id))

    resp = client.delete(f"/cards/{parent_id}", params={"confirm_cascade": "false"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["child_count"] == 1

    assert db_session.get(VisitingCard, uuid.UUID(parent_id)) is not None
    assert db_session.get(VisitingCard, uuid.UUID(child_id)) is not None


def test_delete_card_child_count_matches_multiple_merged_children(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """DoD's child_count must reflect the *actual* count — verified here
    with two children folded onto the same parent, not just one."""
    _authenticated_user(client, fake_otp_provider)
    parent_id = _upload_one(client, jpeg_bytes, filename="parent.jpg")
    child_1_id = _upload_one(client, jpeg_bytes, filename="child1.jpg")
    child_2_id = _upload_one(client, jpeg_bytes, filename="child2.jpg")
    _set_card_fields(db_session, child_1_id, status="duplicate", merged_into_card_id=uuid.UUID(parent_id))
    _set_card_fields(db_session, child_2_id, status="merged", merged_into_card_id=uuid.UUID(parent_id))

    resp = client.delete(f"/cards/{parent_id}")

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["child_count"] == 2, (
        "child_count must count every merged/duplicate row pointing at the target, not just one"
    )

    for cid in (parent_id, child_1_id, child_2_id):
        assert db_session.get(VisitingCard, uuid.UUID(cid)) is not None, (
            f"a rejected cascade delete must leave card {cid} untouched"
        )


def test_delete_card_confirm_cascade_true_deletes_parent_and_child_atomically(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """DoD: DELETE ?confirm_cascade=true on a card with a merged child
    returns 204, and both the parent and the child card row are gone, with
    no FK violation."""
    _authenticated_user(client, fake_otp_provider)
    parent_id = _upload_one(client, jpeg_bytes, filename="parent.jpg")
    child_id = _upload_one(client, jpeg_bytes, filename="child.jpg")
    _set_card_fields(db_session, child_id, status="merged", merged_into_card_id=uuid.UUID(parent_id))

    resp = client.delete(f"/cards/{parent_id}", params={"confirm_cascade": "true"})

    assert resp.status_code == 204, resp.text
    assert db_session.get(VisitingCard, uuid.UUID(parent_id)) is None
    assert db_session.get(VisitingCard, uuid.UUID(child_id)) is None


def test_delete_card_with_real_merge_relationship_cascade_removes_storage_for_both(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """End-to-end variant of the cascade-confirmed case, using a real
    front/back merge relationship produced by `process_card` (rather than a
    hand-constructed one) — confirms both the DB rows AND both cards' S3
    objects are cleaned up after a confirmed cascade delete."""
    _authenticated_user(client, fake_otp_provider)
    front_id, back_id = _make_real_merged_pair(
        client, jpeg_bytes, monkeypatch, company_name="Real Merge Cascade Co"
    )

    front = db_session.get(VisitingCard, uuid.UUID(front_id))
    back = db_session.get(VisitingCard, uuid.UUID(back_id))
    assert back.status == "merged" and str(back.merged_into_card_id) == front_id, (
        "fixture setup: the back scan must be genuinely merged onto the front card"
    )
    front_key, back_key = front.image_url, back.image_url
    assert front_key and back_key

    # Without confirm_cascade first, proving the guard still applies to a
    # real (not hand-constructed) merge relationship.
    guarded = client.delete(f"/cards/{front_id}")
    assert guarded.status_code == 409, guarded.text
    assert guarded.json()["detail"]["child_count"] == 1

    resp = client.delete(f"/cards/{front_id}", params={"confirm_cascade": "true"})
    assert resp.status_code == 204, resp.text

    # front/back were fetched into this session's identity map above, before
    # the delete (committed by the API's own separate session) happened —
    # without expiring them, Session.get() would return the stale cached
    # objects instead of re-querying Postgres. Same pattern already used in
    # test_05_parsing_visiting_card.py after a cross-session mutation.
    db_session.expire_all()

    assert db_session.get(VisitingCard, uuid.UUID(front_id)) is None
    assert db_session.get(VisitingCard, uuid.UUID(back_id)) is None

    with pytest.raises(ClientError):
        storage_service.download_file(front_key)
    with pytest.raises(ClientError):
        storage_service.download_file(back_key)


def test_delete_card_confirm_cascade_true_on_childless_card_still_deletes_normally(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """Spec: 'If there are no children, confirm_cascade is ignored and the
    card is deleted immediately' — passing confirm_cascade=true on a card
    with no merged children must behave identically to the default (204,
    row gone), not error or no-op."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)

    resp = client.delete(f"/cards/{card_id}", params={"confirm_cascade": "true"})

    assert resp.status_code == 204, resp.text
    assert db_session.get(VisitingCard, uuid.UUID(card_id)) is None


def test_delete_card_child_count_is_freshly_recomputed_not_remembered_across_calls(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """Spec: 'The child_count check must happen server-side against a fresh
    query every call — never trust a count the frontend remembers from an
    earlier response.' Verified behaviorally: a first call reports
    child_count=2 and is rejected; after both children are removed via
    independent DELETE calls, a second call for the SAME parent (still no
    confirm_cascade) succeeds, proving the check re-queries rather than
    reusing a stale count from the first call."""
    _authenticated_user(client, fake_otp_provider)
    parent_id = _upload_one(client, jpeg_bytes, filename="parent.jpg")
    child_1_id = _upload_one(client, jpeg_bytes, filename="child1.jpg")
    child_2_id = _upload_one(client, jpeg_bytes, filename="child2.jpg")
    _set_card_fields(db_session, child_1_id, status="duplicate", merged_into_card_id=uuid.UUID(parent_id))
    _set_card_fields(db_session, child_2_id, status="merged", merged_into_card_id=uuid.UUID(parent_id))

    first = client.delete(f"/cards/{parent_id}")
    assert first.status_code == 409, first.text
    assert first.json()["detail"]["child_count"] == 2

    for cid in (child_1_id, child_2_id):
        resp = client.delete(f"/cards/{cid}")
        assert resp.status_code == 204, resp.text

    second = client.delete(f"/cards/{parent_id}")
    assert second.status_code == 204, (
        f"once both children are independently removed, re-deleting the parent without "
        f"confirm_cascade must succeed (fresh count = 0), got {second.status_code}: {second.text}"
    )
    assert db_session.get(VisitingCard, uuid.UUID(parent_id)) is None


# --------------------------------------------------------------------------
# 4. Shared reference data (Company/CompanySignals) is never touched.
# --------------------------------------------------------------------------


def test_delete_card_never_deletes_company_or_company_signals_rows(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Spec: 'Company/CompanySignals rows are never touched/deleted (shared
    reference data)' — verified for both tables, including one already
    carrying enrichment signals."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    # Unique per run (not just per file): companies/company_signals are never
    # truncated between test runs against the persistent dashr_test DB (see
    # module docstring), and Company is get-or-create by normalized_name, so
    # a repeat run reusing this name would hit the same pre-existing Company
    # row and collide on this test's own manual CompanySignals insert below.
    company_name = f"Retained Reference Co Manufacturing {uuid.uuid4().hex[:8]}"
    _patch_vision(
        monkeypatch,
        _fields(full_name="Company Retention Contact", company_name=company_name),
    )
    process_card(card_id)

    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    company_id = row.company_id
    assert company_id is not None, "fixture setup: extraction must have linked a company"

    # Seed a company_signals row directly, so the assertion also covers the
    # child enrichment table, not just companies itself.
    db_session.add(CompanySignals(company_id=company_id, cin="U12345MH2020PTC000000"))
    db_session.commit()

    resp = client.delete(f"/cards/{card_id}")
    assert resp.status_code == 204, resp.text

    assert db_session.get(VisitingCard, uuid.UUID(card_id)) is None

    company = db_session.get(Company, company_id)
    assert company is not None, "deleting a card must never delete the shared Company row it references"

    signals = db_session.get(CompanySignals, company_id)
    assert signals is not None, "deleting a card must never delete the shared CompanySignals row"


# --------------------------------------------------------------------------
# 5. Admin/org visibility — documented gap, matching the identical skips
#    already present in test_04/test_05/test_06/test_07 for this repo.
# --------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "No conftest helper exists yet to put a user through an org-invite/admin "
        "flow (02-user-registration only ever produces org_id=NULL, role=NULL "
        "accounts) — same documented gap as this suite's other admin-visibility "
        "skips. DELETE /cards/{card_id} is spec'd to reuse the same visibility "
        "rule as GET /cards/{card_id}, so it inherits whatever admin-sees-org-member "
        "coverage that gets once the fixture exists."
    )
)
def test_admin_can_delete_teammates_card():
    pass
