"""
Tests for the `20-field-correction` feature (spec:
`.claude/specs/20-field-correction.md`).

`POST /cards/{card_id}/corrections` lets a user correct an AI-mis-extracted
or mis-enriched field (full_name, job_title, address, products_offered,
company_name, email, phone, catalog_url), writing an append-only
`FieldCorrection` audit row (original_value + corrected_value) alongside
applying the fix (`card_service.correct_card_field`). Correcting
`company_name` re-matches/creates a `Company` row rather than renaming the
linked one in place (a cross-org cache); correcting `catalog_url` re-triggers
`rerun_indiamart_supplier_profile_task`, which re-runs the IndiaMART
supplier-profile Apify lookup against the corrected URL
(`enrichment_service.rerun_supplier_profile_lookup`) — never billed,
regardless of free-allowance/wallet state, since it's fixing a mistake in an
already-paid-for enrichment, not a new billable action. Also covers the
related free-rescore amendment: once a card has been scored, a field
correction unlocks one free rescore (`card_service.score_card_now`'s
`rescoring` branch), re-checked independently inside `score_card_task`.

Mocking strategy mirrors `test_07_data_enrichment.py`/`test_15_wallet_usage.
py`: vision extraction via `app.services.vision_client.extract_card_fields`;
the IndiaMART Apify provider via `app.services.enrichment_providers.
local_presence_provider.get_local_presence_provider`; Celery `.delay()` calls
mocked at their call-site import location
(`app.services.card_service.rerun_indiamart_supplier_profile_task.delay`);
wallet/allowance helpers copied locally rather than imported cross-file, per
this repo's established test-file convention.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.main import app as fastapi_app
from app.models.card_email import CardEmail
from app.models.card_phone import CardPhone
from app.models.company import Company
from app.models.company_enrichment import CompanyEnrichment
from app.models.company_signals import CompanySignals
from app.models.field_correction import FieldCorrection
from app.models.free_action_allowance import FreeActionAllowance
from app.models.visiting_card import VisitingCard
from app.services import billing
from app.services.enrichment_providers.local_presence_provider import SupplierProfileResult
from app.workers.card_processing import process_card
from app.workers.enrichment_processing import rerun_indiamart_supplier_profile_task
from app.workers.scoring_processing import score_card_task
from conftest import create_verified_user

# --------------------------------------------------------------------------
# Shared helpers (copied, not imported, from test_07_data_enrichment.py /
# test_15_wallet_usage.py — this repo's established per-file convention).
# --------------------------------------------------------------------------


def _unique_company_name(label: str) -> str:
    return f"{label} {uuid.uuid4().hex[:10]} Pvt Ltd"


def _image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (20, 20), color: str = "blue") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture
def jpeg_bytes() -> bytes:
    return _image_bytes("JPEG")


def _login(client: TestClient, user: dict) -> None:
    resp = client.post("/auth/login", json={"email": user["email"], "password": user["password"]})
    assert resp.status_code == 200, resp.text


def _authenticated_user(client: TestClient, fake_otp_provider, **overrides) -> dict:
    user = create_verified_user(client, fake_otp_provider, **overrides)
    _login(client, user)
    return user


def _upload_one(client: TestClient, jpeg_bytes: bytes, filename: str = "card.jpg") -> str:
    resp = client.post(
        "/cards/bulk-upload", data={}, files=[("files", (filename, jpeg_bytes, "image/jpeg"))]
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["cards"][0]["card_id"]


def _patch_vision(monkeypatch: pytest.MonkeyPatch, *responses) -> None:
    queue = list(responses)

    def _fake(image_bytes: bytes, media_type: str):
        return queue.pop(0)

    monkeypatch.setattr("app.services.vision_client.extract_card_fields", _fake)


def _fields(
    *,
    full_name: str | None = "Extracted Contact",
    job_title: str | None = None,
    company_name: str | None = None,
    website: str | None = None,
    address: str | None = None,
    products_offered: str | None = None,
    emails: list[dict] | None = None,
    phones: list[dict] | None = None,
    gst_number: str | None = None,
) -> dict:
    return {
        "is_back_of_card": False,
        "full_name": full_name,
        "job_title": job_title,
        "company_name": company_name,
        "website": website,
        "address": address,
        "products_offered": products_offered,
        "special_remark": None,
        "raw_ocr_text": "verbatim card text",
        "emails": [] if emails is None else emails,
        "phones": [] if phones is None else phones,
        "gst_number": gst_number,
    }


def _fund_wallet(db_session, user_id: uuid.UUID, amount_inr: str) -> None:
    billing.credit_wallet(db_session, user_id, Decimal(amount_inr), "recharge_credit")


def _exhaust_free_allowance(db_session, user_id: uuid.UUID, action_type: str, count: int = 20) -> None:
    for _ in range(count):
        billing.charge_for_action(db_session, user_id, action_type)


def _allowance_used_count(db_session, user_id: uuid.UUID, action_type: str) -> int:
    allowance = db_session.scalar(
        select(FreeActionAllowance).where(
            FreeActionAllowance.user_id == user_id, FreeActionAllowance.action_type == action_type
        )
    )
    return 0 if allowance is None else allowance.used_count


def _patch_rerun_delay(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple, dict]]:
    captured: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "app.services.card_service.rerun_indiamart_supplier_profile_task.delay",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )
    return captured


def _corrections(db_session, card_id: uuid.UUID, field_name: str) -> list[FieldCorrection]:
    return list(
        db_session.scalars(
            select(FieldCorrection)
            .where(FieldCorrection.card_id == card_id, FieldCorrection.field_name == field_name)
            .order_by(FieldCorrection.created_at)
        )
    )


def _extracted_card(
    client: TestClient,
    jpeg_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
    **field_overrides,
) -> str:
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(monkeypatch, _fields(**field_overrides))
    process_card(card_id)
    return card_id


# ==========================================================================
# 1. Plain text-field corrections (full_name / job_title / address /
#    products_offered) — each writes a FieldCorrection row and updates the
#    live column.
# ==========================================================================


@pytest.mark.parametrize(
    "field_name,initial,corrected",
    [
        ("full_name", "OCR Misread Name", "Correct Name"),
        ("job_title", None, "VP Sales"),
        ("address", "123 Wrong St", "456 Right Ave"),
        ("products_offered", "Widgets", "Gadgets"),
    ],
)
def test_correct_text_field_writes_correction_and_updates_card(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch, field_name, initial, corrected
):
    _authenticated_user(client, fake_otp_provider)
    overrides = {"full_name": "Extracted Contact"}
    overrides[field_name] = initial
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, **overrides)

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": field_name, "corrected_value": corrected},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()[field_name] == corrected

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert getattr(card, field_name) == corrected

    rows = _corrections(db_session, uuid.UUID(card_id), field_name)
    assert len(rows) == 1
    assert rows[0].original_value == initial
    assert rows[0].corrected_value == corrected
    assert rows[0].record_id is None


def test_correcting_same_field_twice_writes_two_rows_not_overwrite(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, full_name="First Name")

    resp1 = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "full_name", "corrected_value": "Second Name"},
    )
    assert resp1.status_code == 200, resp1.text
    resp2 = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "full_name", "corrected_value": "Third Name"},
    )
    assert resp2.status_code == 200, resp2.text

    rows = _corrections(db_session, uuid.UUID(card_id), "full_name")
    assert len(rows) == 2
    assert rows[0].original_value == "First Name"
    assert rows[0].corrected_value == "Second Name"
    assert rows[1].original_value == "Second Name"
    assert rows[1].corrected_value == "Third Name"

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.full_name == "Third Name"


def test_correct_job_title_updates_designation_level_immediately(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """designation_level (what scoring._designation_score actually reads —
    see scoring.py:16-17) is derived from job_title via designation.classify,
    not stored independently. A job_title correction must re-derive it
    immediately, or a later (re)score would keep reading the stale
    pre-correction level."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, job_title="Proprietor")
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.designation_level == "c_level", "fixture setup: Proprietor must classify as c_level"

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "job_title", "corrected_value": "Sales"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["designation_level"] == "individual_contributor"

    db_session.expire_all()
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.designation_level == "individual_contributor"


def test_correct_job_title_then_rescore_reflects_new_designation_score(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """End-to-end regression for the reported bug: correcting role from
    Proprietor to Sales, then rescoring, must actually move the
    designation_score component — not silently keep scoring off the
    pre-correction title.

    Pinned to v1 (via select_scoring_version) since this test asserts on
    v1's designation_score key specifically — the free rescore this
    correction unlocks stays pinned to that same version automatically
    (see .claude/specs/10-lead-scoring.md "Scoring versioning & A/B
    experimentation"), so no second patch is needed for the rescore call.
    """
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, job_title="Proprietor")

    monkeypatch.setattr("app.services.scoring.select_scoring_version", lambda user_id: "v1")
    score_card_task(card_id)  # real scoring run (bare call, bypassing .delay())
    db_session.expire_all()
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.score_breakdown["designation_score"] == 30, (
        "fixture setup: Proprietor must score the max c_level designation points"
    )

    correct_resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "job_title", "corrected_value": "Sales"},
    )
    assert correct_resp.status_code == 200, correct_resp.text
    assert correct_resp.json()["rescore_available"] is True

    score_card_task(card_id)  # the free rescore this correction unlocked

    db_session.expire_all()
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.score_breakdown["designation_score"] == 6, (
        "after correcting job_title from Proprietor (c_level, 30pts) to Sales "
        "(individual_contributor, 6pts), a rescore must reflect the new, lower "
        "designation_score — this is the exact bug report this test guards against"
    )


# ==========================================================================
# 2. Email / phone corrections — record_id targeting, validation, duplicates.
# ==========================================================================


def test_correct_email_updates_row_and_writes_correction(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(
        client, jpeg_bytes, monkeypatch,
        emails=[{"email": "wrong@example.com", "email_type": "work"}],
    )
    email_row = db_session.scalar(select(CardEmail).where(CardEmail.card_id == uuid.UUID(card_id)))

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={
            "field_name": "email",
            "corrected_value": "right@example.com",
            "record_id": str(email_row.email_id),
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["emails"][0]["email"] == "right@example.com"

    db_session.expire_all()
    refreshed = db_session.get(CardEmail, email_row.email_id)
    assert refreshed.email == "right@example.com"

    rows = _corrections(db_session, uuid.UUID(card_id), "email")
    assert len(rows) == 1
    assert rows[0].original_value == "wrong@example.com"
    assert rows[0].corrected_value == "right@example.com"
    assert rows[0].record_id == email_row.email_id


def test_correct_phone_updates_row_and_writes_correction(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(
        client, jpeg_bytes, monkeypatch,
        phones=[{"phone": "+919876500001", "phone_type": "mobile"}],
    )
    phone_row = db_session.scalar(select(CardPhone).where(CardPhone.card_id == uuid.UUID(card_id)))

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={
            "field_name": "phone",
            "corrected_value": "+919876500002",
            "record_id": str(phone_row.phone_id),
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["phones"][0]["phone_e164"] == "+919876500002"

    rows = _corrections(db_session, uuid.UUID(card_id), "phone")
    assert len(rows) == 1
    assert rows[0].original_value == "+919876500001"
    assert rows[0].corrected_value == "+919876500002"


def test_correct_email_missing_record_id_returns_422(client, fake_otp_provider, jpeg_bytes, monkeypatch):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch)

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "email", "corrected_value": "x@example.com"},
    )
    assert resp.status_code == 422, resp.text


def test_correct_full_name_with_record_id_returns_422(client, fake_otp_provider, jpeg_bytes, monkeypatch):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch)

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={
            "field_name": "full_name",
            "corrected_value": "Someone",
            "record_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 422, resp.text


def test_correct_email_nonexistent_record_id_returns_404_writes_nothing(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(
        client, jpeg_bytes, monkeypatch, emails=[{"email": "a@example.com", "email_type": None}]
    )

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={
            "field_name": "email",
            "corrected_value": "b@example.com",
            "record_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 404, resp.text
    assert _corrections(db_session, uuid.UUID(card_id), "email") == []


def test_correct_email_record_id_from_different_card_returns_404(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_a = _extracted_card(
        client, jpeg_bytes, monkeypatch, emails=[{"email": "a@example.com", "email_type": None}]
    )
    card_b = _extracted_card(
        client, jpeg_bytes, monkeypatch, emails=[{"email": "b@example.com", "email_type": None}]
    )
    email_on_b = db_session.scalar(select(CardEmail).where(CardEmail.card_id == uuid.UUID(card_b)))

    resp = client.post(
        f"/cards/{card_a}/corrections",
        json={
            "field_name": "email",
            "corrected_value": "hijack@example.com",
            "record_id": str(email_on_b.email_id),
        },
    )
    assert resp.status_code == 404, resp.text


def test_correct_email_malformed_returns_400_writes_nothing(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(
        client, jpeg_bytes, monkeypatch, emails=[{"email": "valid@example.com", "email_type": None}]
    )
    email_row = db_session.scalar(select(CardEmail).where(CardEmail.card_id == uuid.UUID(card_id)))

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={
            "field_name": "email",
            "corrected_value": "not-an-email",
            "record_id": str(email_row.email_id),
        },
    )
    assert resp.status_code == 400, resp.text
    db_session.expire_all()
    assert db_session.get(CardEmail, email_row.email_id).email == "valid@example.com"
    assert _corrections(db_session, uuid.UUID(card_id), "email") == []


def test_correct_phone_unparseable_returns_400_writes_nothing(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(
        client, jpeg_bytes, monkeypatch, phones=[{"phone": "+919876500003", "phone_type": None}]
    )
    phone_row = db_session.scalar(select(CardPhone).where(CardPhone.card_id == uuid.UUID(card_id)))

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "phone", "corrected_value": "abc", "record_id": str(phone_row.phone_id)},
    )
    assert resp.status_code == 400, resp.text
    db_session.expire_all()
    assert db_session.get(CardPhone, phone_row.phone_id).phone_e164 == "+919876500003"
    assert _corrections(db_session, uuid.UUID(card_id), "phone") == []


def test_correct_email_duplicate_collision_returns_400_and_rolls_back(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(
        client, jpeg_bytes, monkeypatch,
        emails=[
            {"email": "one@example.com", "email_type": None},
            {"email": "two@example.com", "email_type": None},
        ],
    )
    rows = db_session.scalars(
        select(CardEmail).where(CardEmail.card_id == uuid.UUID(card_id)).order_by(CardEmail.email)
    ).all()
    one, two = rows[0], rows[1]
    assert {one.email, two.email} == {"one@example.com", "two@example.com"}

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "email", "corrected_value": two.email, "record_id": str(one.email_id)},
    )
    assert resp.status_code == 400, resp.text

    db_session.expire_all()
    assert db_session.get(CardEmail, one.email_id).email == "one@example.com"
    assert db_session.get(CardEmail, two.email_id).email == "two@example.com"
    assert _corrections(db_session, uuid.UUID(card_id), "email") == []


def test_correct_phone_duplicate_collision_returns_400_and_rolls_back(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(
        client, jpeg_bytes, monkeypatch,
        phones=[
            {"phone": "+919876500010", "phone_type": None},
            {"phone": "+919876500011", "phone_type": None},
        ],
    )
    rows = db_session.scalars(
        select(CardPhone).where(CardPhone.card_id == uuid.UUID(card_id)).order_by(CardPhone.phone_e164)
    ).all()
    one, two = rows[0], rows[1]

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={
            "field_name": "phone",
            "corrected_value": two.phone_e164,
            "record_id": str(one.phone_id),
        },
    )
    assert resp.status_code == 400, resp.text
    db_session.expire_all()
    assert db_session.get(CardPhone, one.phone_id).phone_e164 == one.phone_e164
    assert _corrections(db_session, uuid.UUID(card_id), "phone") == []


# ==========================================================================
# 3. company_name corrections — re-match/create, never rename in place.
# ==========================================================================


def test_correct_company_name_matches_existing_company_and_repoints_card(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    existing_name = _unique_company_name("Existing Target Co")
    existing_card_id = _extracted_card(client, jpeg_bytes, monkeypatch, company_name=existing_name)
    existing_card = db_session.get(VisitingCard, uuid.UUID(existing_card_id))
    existing_company_id = existing_card.company_id
    assert existing_company_id is not None

    other_card_id = _extracted_card(
        client, jpeg_bytes, monkeypatch, company_name=_unique_company_name("To Be Renamed")
    )

    resp = client.post(
        f"/cards/{other_card_id}/corrections",
        json={"field_name": "company_name", "corrected_value": existing_name},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["company"]["company_id"] == str(existing_company_id)

    db_session.expire_all()
    other_card = db_session.get(VisitingCard, uuid.UUID(other_card_id))
    assert other_card.company_id == existing_company_id, "must repoint to the existing matched Company"

    unchanged = db_session.get(Company, existing_company_id)
    assert unchanged.name == existing_name, "the matched Company row must never be renamed in place"


def test_correct_company_name_creates_new_company_when_no_match(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, company_name=_unique_company_name("Old Name Co"))
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    old_company_id = card.company_id

    new_name = _unique_company_name("Brand New Co")
    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "company_name", "corrected_value": new_name},
    )
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    refreshed = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert refreshed.company_id != old_company_id
    new_company = db_session.get(Company, refreshed.company_id)
    assert new_company.name == new_name


def test_correct_company_name_on_one_org_card_does_not_leak_to_another_orgs_card(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """The core anti-cross-tenant-leak assertion: two different users' cards
    matched to the same shared Company row — correcting one must never
    change what the other user's card shows."""
    shared_name = _unique_company_name("Shared Cache Co")

    user_a = _authenticated_user(client, fake_otp_provider)
    card_a = _extracted_card(client, jpeg_bytes, monkeypatch, company_name=shared_name)

    with TestClient(fastapi_app) as client_b:
        _authenticated_user(client_b, fake_otp_provider)
        card_b_id = _upload_one(client_b, jpeg_bytes)
        _patch_vision(monkeypatch, _fields(full_name="Their Contact", company_name=shared_name))
        process_card(card_b_id)

        card_a_row = db_session.get(VisitingCard, uuid.UUID(card_a))
        card_b_row = db_session.get(VisitingCard, uuid.UUID(card_b_id))
        assert card_a_row.company_id == card_b_row.company_id, (
            "fixture setup: both cards must have matched the same shared Company row"
        )

        resp = client.post(
            f"/cards/{card_a}/corrections",
            json={"field_name": "company_name", "corrected_value": "Renamed By User A Only"},
        )
        assert resp.status_code == 200, resp.text

        b_detail = client_b.get(f"/cards/{card_b_id}")
        assert b_detail.status_code == 200, b_detail.text
        assert b_detail.json()["company"]["name"] == shared_name, (
            "another user's card, still pointed at the original Company row, must show the "
            "unchanged name — never affected by another org's correction"
        )


def test_correct_field_for_another_users_card_returns_404(
    client, fake_otp_provider, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        their_card_id = _extracted_card(other_client, jpeg_bytes, monkeypatch, full_name="Owner Contact")

        resp = client.post(
            f"/cards/{their_card_id}/corrections",
            json={"field_name": "full_name", "corrected_value": "Hijacked Name"},
        )

    assert resp.status_code == 404, resp.text


# ==========================================================================
# 4. catalog_url corrections — validation, billing gate, re-fetch trigger.
# ==========================================================================


def test_correct_catalog_url_non_indiamart_returns_400_writes_nothing(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, company_name=_unique_company_name("Bad URL Co"))

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "catalog_url", "corrected_value": "https://example.com/catalogue"},
    )
    assert resp.status_code == 400, resp.text
    assert _corrections(db_session, uuid.UUID(card_id), "catalog_url") == []


def test_correct_catalog_url_no_linked_company_returns_400(client, fake_otp_provider, jpeg_bytes, monkeypatch):
    _authenticated_user(client, fake_otp_provider)
    # No company_name -> extraction never links a company.
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, company_name=None)

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={
            "field_name": "catalog_url",
            "corrected_value": "https://www.indiamart.com/some-company/",
        },
    )
    assert resp.status_code == 400, resp.text


def test_correct_catalog_url_succeeds_at_zero_balance_with_allowance_exhausted_never_billed(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Correcting catalog_url must never be billed — it fixes a mistake in
    an already-paid-for enrichment, not a new billable action. Must succeed
    even with the "enrichment" free allowance fully exhausted and a zero
    wallet balance, and must never touch the allowance counter at all."""
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, company_name=_unique_company_name("Free URL Co"))
    card = db_session.get(VisitingCard, uuid.UUID(card_id))

    _exhaust_free_allowance(db_session, user_id, "enrichment")
    captured = _patch_rerun_delay(monkeypatch)
    new_url = "https://www.indiamart.com/free-correction-co/"

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "catalog_url", "corrected_value": new_url},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["company"]["catalog_url"] == new_url

    db_session.expire_all()
    signals = db_session.get(CompanySignals, card.company_id)
    assert signals is not None
    assert signals.catalog_url == new_url
    assert len(_corrections(db_session, uuid.UUID(card_id), "catalog_url")) == 1
    assert len(captured) == 1
    # Unchanged from before the call — this correction never touched billing.
    assert _allowance_used_count(db_session, user_id, "enrichment") == 20


def test_correct_catalog_url_success_updates_signals_and_enqueues_task(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, company_name=_unique_company_name("Good URL Co"))
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    company_id = card.company_id
    new_url = "https://www.indiamart.com/corrected-company/"
    captured = _patch_rerun_delay(monkeypatch)

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "catalog_url", "corrected_value": new_url},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["company"]["catalog_url"] == new_url

    db_session.expire_all()
    signals = db_session.get(CompanySignals, company_id)
    assert signals is not None
    assert signals.catalog_url == new_url

    rows = _corrections(db_session, uuid.UUID(card_id), "catalog_url")
    assert len(rows) == 1
    assert rows[0].corrected_value == new_url

    assert len(captured) == 1
    args, kwargs = captured[0]
    assert args == (str(company_id), new_url, str(card.card_id))
    assert kwargs == {}  # never billed — no billed= kwarg at all


def test_rerun_indiamart_supplier_profile_task_updates_signals_and_writes_audit_row(
    db_session, monkeypatch
):
    company = Company(
        name=_unique_company_name("Task Level Co"),
        normalized_name=_unique_company_name("task level co").lower(),
    )
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)

    class _StaticSupplierProfileProvider:
        def lookup_supplier_profile(self, catalog_url: str) -> SupplierProfileResult:
            return SupplierProfileResult(
                marketplace_verified_badge=True,
                indiamart_rating=4.2,
                indiamart_rating_count=50,
                indiamart_member_since_year=2018,
                indiamart_business_type="Manufacturer",
                indiamart_employee_count_band="11 to 25 People",
                indiamart_annual_turnover_band="1 - 5 Cr",
                indiamart_year_established="2015",
                indiamart_gst_number="27AAAAA0000A1Z5",
                indiamart_gst_registration_year=2019,
                indiamart_call_response_rate="90%",
                source_tag="indiamart_supplier_profile",
                raw_payload={"items": [{"companyName": "Task Level Co"}]},
            )

    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.get_local_presence_provider",
        lambda: _StaticSupplierProfileProvider(),
    )

    rerun_indiamart_supplier_profile_task(
        str(company.company_id), "https://www.indiamart.com/task-level-co/", str(uuid.uuid4())
    )

    db_session.expire_all()
    signals = db_session.get(CompanySignals, company.company_id)
    assert signals is not None
    assert signals.indiamart_rating == Decimal("4.2")
    assert signals.indiamart_member_since_year == 2018
    assert signals.marketplace_vintage_years == datetime.now(timezone.utc).year - 2018

    audit_rows = db_session.scalars(
        select(CompanyEnrichment).where(CompanyEnrichment.company_id == company.company_id)
    ).all()
    assert len(audit_rows) == 1
    assert audit_rows[0].source == "indiamart_supplier_profile"


# ==========================================================================
# 4b. No-op correction guard (all field types) + catalog_url cooldown —
#     closes two abuse vectors surfaced by /code-review-feature: identical
#     resubmission (free-rescore spam on any field; trivial Apify-spam on
#     catalog_url) and, for catalog_url specifically, cycling between two
#     distinct real URLs (which the no-op guard alone can't catch).
# ==========================================================================


def test_correct_text_field_with_identical_value_returns_400_writes_nothing(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, full_name="Same Name")

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "full_name", "corrected_value": "Same Name"},
    )
    assert resp.status_code == 400, resp.text
    assert _corrections(db_session, uuid.UUID(card_id), "full_name") == []


def test_correct_company_name_with_identical_value_returns_400_writes_nothing(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    name = _unique_company_name("Same Co")
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, company_name=name)

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "company_name", "corrected_value": name},
    )
    assert resp.status_code == 400, resp.text
    assert _corrections(db_session, uuid.UUID(card_id), "company_name") == []


def test_correct_email_with_identical_normalized_value_returns_400_writes_nothing(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(
        client, jpeg_bytes, monkeypatch, emails=[{"email": "same@example.com", "email_type": None}]
    )
    email_row = db_session.scalar(select(CardEmail).where(CardEmail.card_id == uuid.UUID(card_id)))

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={
            "field_name": "email",
            "corrected_value": "same@example.com",
            "record_id": str(email_row.email_id),
        },
    )
    assert resp.status_code == 400, resp.text
    assert _corrections(db_session, uuid.UUID(card_id), "email") == []


def test_correct_catalog_url_with_identical_value_returns_400_never_reenqueues(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """The primary security fix: resubmitting the same catalog_url must
    never re-trigger the paid Apify re-fetch."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, company_name=_unique_company_name("NoOp URL Co"))
    url = "https://www.indiamart.com/no-op-co/"
    captured = _patch_rerun_delay(monkeypatch)

    first = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "catalog_url", "corrected_value": url},
    )
    assert first.status_code == 200, first.text
    assert len(captured) == 1

    second = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "catalog_url", "corrected_value": url},
    )
    assert second.status_code == 400, second.text
    assert len(captured) == 1, "resubmitting the identical URL must never enqueue a second re-fetch"
    assert len(_corrections(db_session, uuid.UUID(card_id), "catalog_url")) == 1


def test_correct_catalog_url_second_distinct_url_within_cooldown_returns_429(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """The secondary security fix: even a genuinely different URL is
    throttled within the per-user cooldown window — closes the residual
    "cycle between two real URLs" Apify-spam loop the no-op guard alone
    can't catch."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, company_name=_unique_company_name("Cooldown Co"))
    captured = _patch_rerun_delay(monkeypatch)

    first = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "catalog_url", "corrected_value": "https://www.indiamart.com/cooldown-co-a/"},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "catalog_url", "corrected_value": "https://www.indiamart.com/cooldown-co-b/"},
    )
    assert second.status_code == 429, second.text
    assert len(captured) == 1, "the cooldown-blocked correction must never enqueue a re-fetch"
    assert len(_corrections(db_session, uuid.UUID(card_id), "catalog_url")) == 1


def test_correct_catalog_url_succeeds_again_after_cooldown_elapses(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(
        client, jpeg_bytes, monkeypatch, company_name=_unique_company_name("Cooldown Elapsed Co")
    )
    captured = _patch_rerun_delay(monkeypatch)

    first = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "catalog_url", "corrected_value": "https://www.indiamart.com/elapsed-co-a/"},
    )
    assert first.status_code == 200, first.text

    # Backdate the correction row past the cooldown window — simulates
    # elapsed time without a real sleep, same convention other test files
    # use for time-based logic (e.g. OTP expiry backdating).
    row = db_session.scalar(
        select(FieldCorrection).where(
            FieldCorrection.card_id == uuid.UUID(card_id), FieldCorrection.field_name == "catalog_url"
        )
    )
    row.created_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    db_session.commit()

    second = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "catalog_url", "corrected_value": "https://www.indiamart.com/elapsed-co-b/"},
    )
    assert second.status_code == 200, second.text
    assert len(captured) == 2


# ==========================================================================
# 5. GET /cards/{card_id} regression — emails/phones now expose their ids.
# ==========================================================================


def test_get_card_detail_exposes_email_id_and_phone_id(
    client, fake_otp_provider, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(
        client, jpeg_bytes, monkeypatch,
        emails=[{"email": "id-check@example.com", "email_type": None}],
        phones=[{"phone": "+919876500099", "phone_type": None}],
    )

    resp = client.get(f"/cards/{card_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["emails"][0]["email_id"]
    assert body["phones"][0]["phone_id"]


# ==========================================================================
# 6. Free rescore after a field correction — once a card has been scored,
#    correcting any field unlocks exactly one free rescore (never billed,
#    never counted against the free allowance), re-checked independently at
#    the task level too, not just the router/service gate.
# ==========================================================================


def _mark_scored(db_session, card_id: uuid.UUID, scored_at: datetime | None = None) -> None:
    """Directly sets a card's score fields via the ORM — this section tests
    rescore *eligibility*, not scoring computation itself, so it skips the
    real scoring.calculate_score pipeline entirely."""
    card = db_session.get(VisitingCard, card_id)
    card.lead_score = 42
    card.score_breakdown = {
        "designation_score": 10, "company_size_score": 10, "industry_fit_score": 10,
        "momentum_signal_score": 10, "remark_signal_score": 2, "total": 42, "version": "v1",
    }
    card.scored_at = scored_at or datetime.now(timezone.utc)
    db_session.commit()


def _patch_score_delay(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple, dict]]:
    captured: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "app.services.card_service.score_card_task.delay",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )
    return captured


def test_score_card_already_scored_no_correction_since_still_returns_409(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Regression: the pre-existing one-shot rule must still hold when
    nothing was corrected after the last score."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, full_name="Never Corrected")
    _mark_scored(db_session, uuid.UUID(card_id))
    captured = _patch_score_delay(monkeypatch)

    resp = client.post(f"/cards/{card_id}/score")
    assert resp.status_code == 409, resp.text
    assert captured == []


def test_correcting_scored_card_with_identical_value_does_not_unlock_rescore(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """The no-op guard closes the other half of the abuse vector: resubmitting
    the same value repeatedly must never keep unlocking new free rescores."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, full_name="Locked Name")
    _mark_scored(db_session, uuid.UUID(card_id))

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "full_name", "corrected_value": "Locked Name"},
    )
    assert resp.status_code == 400, resp.text

    detail = client.get(f"/cards/{card_id}")
    assert detail.json()["rescore_available"] is False


def test_correction_after_score_marks_rescore_available_in_responses(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, full_name="Pre Score Name")
    _mark_scored(db_session, uuid.UUID(card_id))

    detail = client.get(f"/cards/{card_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["rescore_available"] is False, "no correction yet — must not offer a rescore"

    resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "full_name", "corrected_value": "Post Score Name"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rescore_available"] is True


def test_score_card_after_correction_allows_free_rescore_never_billed(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, full_name="Original Scored Name")
    _mark_scored(db_session, uuid.UUID(card_id))
    before_used = _allowance_used_count(db_session, user_id, "scoring")

    correct_resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "full_name", "corrected_value": "Corrected Scored Name"},
    )
    assert correct_resp.status_code == 200, correct_resp.text

    captured = _patch_score_delay(monkeypatch)
    resp = client.post(f"/cards/{card_id}/score")
    assert resp.status_code == 200, resp.text

    assert len(captured) == 1
    args, kwargs = captured[0]
    assert args == (str(card_id),)
    assert kwargs == {"billed": False}
    # Unchanged — this rescore never called billing.charge_for_action at all.
    assert _allowance_used_count(db_session, user_id, "scoring") == before_used


def test_score_card_after_correction_at_zero_balance_and_exhausted_allowance_still_free(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """The free-rescore carve-out must hold even when the user has no free
    scoring allowance left and no wallet balance — it was never a billable
    action to begin with, so there's nothing to block."""
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, full_name="Broke User Name")
    _mark_scored(db_session, uuid.UUID(card_id))
    client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "full_name", "corrected_value": "Broke User Name Fixed"},
    )
    _exhaust_free_allowance(db_session, user_id, "scoring")
    captured = _patch_score_delay(monkeypatch)

    resp = client.post(f"/cards/{card_id}/score")
    assert resp.status_code == 200, resp.text
    assert len(captured) == 1
    assert captured[0][1] == {"billed": False}


def test_score_card_task_skips_rescore_when_no_correction_since_last_score(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, full_name="Task Guard Name")
    scored_at = datetime.now(timezone.utc)
    _mark_scored(db_session, uuid.UUID(card_id), scored_at=scored_at)

    score_card_task(card_id)  # bare call, bypassing .delay() — no correction since, must no-op

    db_session.expire_all()
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.scored_at == scored_at, "one-shot rule: no correction since last score, must not re-run"
    assert card.lead_score == 42


def test_score_card_task_allows_rescore_when_correction_postdates_score(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, full_name="Task Rescore Name")
    scored_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    _mark_scored(db_session, uuid.UUID(card_id), scored_at=scored_at)

    correct_resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "full_name", "corrected_value": "Task Rescore Name Fixed"},
    )
    assert correct_resp.status_code == 200, correct_resp.text

    score_card_task(card_id)  # bare call — a correction postdates the score, must actually rescore

    db_session.expire_all()
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.scored_at is not None and card.scored_at > scored_at, (
        "a correction postdating the last score must unlock an actual rescore run"
    )
