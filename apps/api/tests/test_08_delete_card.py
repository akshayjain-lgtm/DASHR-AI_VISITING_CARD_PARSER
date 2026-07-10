"""
Tests for the `08-delete-card` feature (spec: `.claude/specs/08-delete-card.md`).

`DELETE /cards/{card_id}` permanently removes a card. If other cards were
merged into it (back-of-card scans or duplicates, `merged_into_card_id`
pointing at it — see `05-parsing-visiting-card`), the delete cascades to
those children, but only once the caller passes `confirm_cascade=true`;
otherwise the endpoint returns `409` with a `child_count` and deletes
nothing, so a UI can show a second, cascade-specific confirmation before
retrying.

Judgment calls made in the absence of explicit spec text (documented inline
at point of use too):
  1. **Storage assertions use real local MinIO**, not a mocked
     `storage_service`, matching `test_04_visiting_card_bulk_upload.py`'s and
     `test_05_parsing_visiting_card.py`'s established "real local infra over
     mocks" philosophy. `test_delete_card_removes_storage_object` uploads a
     real file, deletes it via the API, then asserts
     `storage_service.download_file` raises `ClientError` for the now-gone
     key — proving actual removal rather than trusting a mocked call.
  2. **Merged-child setup reuses the real front/back merge flow** from
     `test_05_parsing_visiting_card.py::test_back_of_card_merges_onto_front_sibling_fill_gaps_only`
     (`_upload_two` + `_patch_vision` + `process_card`) rather than
     hand-constructing a `merged_into_card_id` row directly, so these tests
     exercise a real merge relationship, not an artificial one.
  3. **Admin-deletes-teammate's-card visibility** is left as a documented gap,
     identical in cause and treatment to the skips already present in
     `test_04_visiting_card_bulk_upload.py` and
     `test_05_parsing_visiting_card.py`: `02-user-registration` only ever
     produces `org_id=NULL, role=NULL` accounts, and no conftest helper
     exists yet to put a user through an org-invite/admin flow.
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
from app.models.visiting_card import VisitingCard
from app.services import storage_service
from app.workers.card_processing import process_card
from conftest import create_verified_user

VALID_PHONE = "+14155552671"


# --------------------------------------------------------------------------
# Image bytes — a real, Pillow-decodable JPEG (never placeholder bytes),
# matching test_04/test_05's established convention.
# --------------------------------------------------------------------------


def _image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (20, 20), color: str = "blue") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture
def jpeg_bytes() -> bytes:
    return _image_bytes()


# --------------------------------------------------------------------------
# Auth / upload / vision-mocking helpers — copied from
# test_05_parsing_visiting_card.py's local (per-file) convention.
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


def _make_merged_pair(
    client: TestClient, jpeg_bytes: bytes, monkeypatch: pytest.MonkeyPatch, company_name: str
) -> tuple[str, str]:
    """Produces a real front/back merge relationship — front gets full
    contact fields, back is flagged `is_back_of_card` with no contact info of
    its own, so the extraction pipeline folds it onto the front and marks it
    `status='merged'` with `merged_into_card_id` pointing at the front.
    Mirrors `test_05_parsing_visiting_card.py`'s
    `test_back_of_card_merges_onto_front_sibling_fill_gaps_only`."""
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


def _set_card_fields(db_session, card_id: str, **fields) -> None:
    """Directly sets fields on a card row via the ORM, for constructing a
    specific pre-existing state the pipeline itself never organically
    reaches within a single test — mirrors
    test_05_parsing_visiting_card.py's identically-named helper."""
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    for key, value in fields.items():
        setattr(card, key, value)
    db_session.commit()


# --------------------------------------------------------------------------
# 1. Happy path — no merge relationship involved.
# --------------------------------------------------------------------------


def test_delete_card_removes_row_and_children_rows(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: deleting a card with no merged children removes the row and its
    card_emails/card_phones (DB-level ON DELETE CASCADE)."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Anita Rao",
            company_name="Delete Card Test Co",
            emails=[{"email": "anita@example.com", "email_type": "work"}],
            phones=[{"phone": VALID_PHONE, "phone_type": "mobile"}],
        ),
    )
    process_card(card_id)

    assert db_session.scalars(select(CardEmail).where(CardEmail.card_id == uuid.UUID(card_id))).all()
    assert db_session.scalars(select(CardPhone).where(CardPhone.card_id == uuid.UUID(card_id))).all()

    resp = client.delete(f"/cards/{card_id}")
    assert resp.status_code == 204, resp.text

    assert db_session.get(VisitingCard, uuid.UUID(card_id)) is None
    assert (
        db_session.scalars(select(CardEmail).where(CardEmail.card_id == uuid.UUID(card_id))).all() == []
    )
    assert (
        db_session.scalars(select(CardPhone).where(CardPhone.card_id == uuid.UUID(card_id))).all() == []
    )


def test_delete_card_removes_storage_object(client, fake_otp_provider, db_session, jpeg_bytes):
    """DoD: after a successful delete, the card's image is no longer
    retrievable from the configured S3/MinIO bucket at its stored key."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    key = card.image_url
    assert key
    # Sanity: the object exists before delete.
    assert storage_service.download_file(key) == jpeg_bytes

    resp = client.delete(f"/cards/{card_id}")
    assert resp.status_code == 204, resp.text

    with pytest.raises(ClientError):
        storage_service.download_file(key)


# --------------------------------------------------------------------------
# 2. Cascade confirmation for merged children.
# --------------------------------------------------------------------------


def test_delete_card_with_merged_child_requires_confirm_cascade(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: deleting a card with a merged child, without confirm_cascade,
    returns 409 with the correct child_count and deletes nothing."""
    _authenticated_user(client, fake_otp_provider)
    front_id, back_id = _make_merged_pair(
        client, jpeg_bytes, monkeypatch, company_name="Cascade Confirm Test Co"
    )

    back = db_session.get(VisitingCard, uuid.UUID(back_id))
    assert back.status == "merged"
    assert str(back.merged_into_card_id) == front_id

    resp = client.delete(f"/cards/{front_id}")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["child_count"] == 1

    assert db_session.get(VisitingCard, uuid.UUID(front_id)) is not None
    assert db_session.get(VisitingCard, uuid.UUID(back_id)) is not None


def test_delete_card_with_confirm_cascade_deletes_parent_and_children(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: DELETE ?confirm_cascade=true on a card with a merged child
    returns 204 and removes both rows, with no FK violation."""
    _authenticated_user(client, fake_otp_provider)
    front_id, back_id = _make_merged_pair(
        client, jpeg_bytes, monkeypatch, company_name="Cascade Confirmed Test Co"
    )

    resp = client.delete(f"/cards/{front_id}", params={"confirm_cascade": "true"})
    assert resp.status_code == 204, resp.text

    assert db_session.get(VisitingCard, uuid.UUID(front_id)) is None
    assert db_session.get(VisitingCard, uuid.UUID(back_id)) is None


def test_delete_card_confirm_cascade_true_on_childless_card_still_deletes(
    client, fake_otp_provider, db_session, jpeg_bytes
):
    """DoD: 'If there are no children, confirm_cascade is ignored and the
    card is deleted immediately' — passing confirm_cascade=true on a card
    with no merged children must behave identically to the default."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)

    resp = client.delete(f"/cards/{card_id}", params={"confirm_cascade": "true"})
    assert resp.status_code == 204, resp.text
    assert db_session.get(VisitingCard, uuid.UUID(card_id)) is None


def test_delete_card_cascades_to_child_owned_by_different_user(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """A duplicate/back-of-card match can legitimately span owners within an
    org (extraction_service's duplicate search is scoped to the *uploader's*
    own visibility, which is org-wide for an admin) — so a card's
    merged_into_card_id child isn't guaranteed to share the parent's
    user_id. delete_card's children lookup must NOT be scoped to the
    deleting user's own visibility, or it under-counts a child it doesn't
    own, and the parent delete then hits an FK violation once it reaches the
    DB. This constructs that cross-owner relationship directly (bypassing
    the real admin/org upload flow, which no conftest fixture supports yet —
    see the module-level admin skip below) to prove the fix at the
    delete_card level, independent of that missing fixture."""
    _authenticated_user(client, fake_otp_provider)
    parent_id = _upload_one(client, jpeg_bytes, filename="parent.jpg")
    _patch_vision(
        monkeypatch,
        _fields(full_name="Owner Contact", company_name="Cross Owner Test Co"),
    )
    process_card(parent_id)

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        child_id = _upload_one(other_client, jpeg_bytes, filename="child.jpg")

    _set_card_fields(
        db_session, child_id, merged_into_card_id=uuid.UUID(parent_id), status="duplicate"
    )

    # Without confirm_cascade: the cross-owner child must still be detected
    # and rejected, not silently missed and left to fail as an FK violation.
    resp = client.delete(f"/cards/{parent_id}")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["child_count"] == 1
    assert db_session.get(VisitingCard, uuid.UUID(parent_id)) is not None
    assert db_session.get(VisitingCard, uuid.UUID(child_id)) is not None

    # With confirm_cascade: both rows are removed regardless of the owner
    # mismatch — the parent's own visibility check already authorized this.
    resp = client.delete(f"/cards/{parent_id}", params={"confirm_cascade": "true"})
    assert resp.status_code == 204, resp.text
    assert db_session.get(VisitingCard, uuid.UUID(parent_id)) is None
    assert db_session.get(VisitingCard, uuid.UUID(child_id)) is None


# --------------------------------------------------------------------------
# 3. Visibility / not-found.
# --------------------------------------------------------------------------


def test_delete_card_for_another_users_card_returns_404(client, fake_otp_provider, db_session, jpeg_bytes):
    _authenticated_user(client, fake_otp_provider)
    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        theirs_id = _upload_one(other_client, jpeg_bytes)

    resp = client.delete(f"/cards/{theirs_id}")
    assert resp.status_code == 404, resp.text
    assert db_session.get(VisitingCard, uuid.UUID(theirs_id)) is not None


def test_delete_card_nonexistent_id_returns_404(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    resp = client.delete(f"/cards/{uuid.uuid4()}")
    assert resp.status_code == 404, resp.text


def test_delete_card_without_session_returns_401():
    with TestClient(fastapi_app) as anon_client:
        resp = anon_client.delete(f"/cards/{uuid.uuid4()}")
        assert resp.status_code == 401, resp.text


# --------------------------------------------------------------------------
# 4. Admin/org visibility — documented gap, see module docstring judgment
#    call #3 and the identical skips in test_04/test_05.
# --------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "No conftest helper exists yet to put a user through an org-invite/admin "
        "flow (02-user-registration only ever produces org_id=NULL, role=NULL "
        "accounts) — same documented gap as test_04/test_05's admin-visibility "
        "skips. delete_card reuses get_visible_card/scope_to_visible_users "
        "unmodified, so it inherits whatever admin-sees-org-member coverage "
        "those get once the fixture exists."
    )
)
def test_admin_can_delete_teammates_card():
    pass
