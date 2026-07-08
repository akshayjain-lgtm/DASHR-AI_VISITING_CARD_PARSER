"""
Tests for the 06-company-profile-backend feature (spec:
.claude/specs/06-company-profile-backend.md).

Covers GET /profile / PUT /profile against seller_profiles:
- get-or-null on first load: 200 with profile_id: null and every other
  field null for a user who has never saved a profile — never a 404.
- upsert on save: the first PUT creates exactly one row per user_id; every
  subsequent PUT updates that same row (never a second one) and advances
  updated_at while leaving created_at unchanged.
- omitting a field from a PUT body leaves the existing stored value for that
  column unchanged rather than nulling it out.
- every route requires a valid session (401 without one).
- strict user_id scoping: one user's PUT can never create or modify another
  user's row.

The client fixture's cookie jar is already authenticated immediately after
create_verified_user() returns (verify-otp sets the session cookie) — no
extra login step is needed.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import app as fastapi_app
from app.models.seller_profile import SellerProfile
from conftest import create_verified_user

FULL_PROFILE_PAYLOAD = {
    "company_name": "Thermax Limited",
    "industry": "Process Equipment & Heat Exchangers",
    "product_lines": "Industrial boilers, heat recovery systems, absorption chillers",
    "last_year_revenue": "125000000.50",
    "revenue_currency": "USD",
    "target_customer_description": (
        "Plant engineers and procurement heads in chemical, pharma, and food processing"
    ),
    "target_regions": "Pan India, Middle East",
}

PROFILE_OUT_FIELDS = [
    "company_name",
    "industry",
    "product_lines",
    "last_year_revenue",
    "revenue_currency",
    "target_customer_description",
    "target_regions",
]


# --------------------------------------------------------------------------
# 1. Auth guard
# --------------------------------------------------------------------------


def test_get_profile_without_session_returns_401(client):
    resp = client.get("/profile")
    assert resp.status_code == 401


def test_put_profile_without_session_returns_401(client):
    resp = client.put("/profile", json=FULL_PROFILE_PAYLOAD)
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# 2. GET /profile for a user who has never saved one
# --------------------------------------------------------------------------


def test_get_profile_never_saved_returns_200_with_all_nulls(client, fake_otp_provider):
    create_verified_user(client, fake_otp_provider)

    resp = client.get("/profile")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["profile_id"] is None, "an unsaved profile must have a null profile_id, not a 404"
    for field in PROFILE_OUT_FIELDS:
        assert body[field] is None, f"{field} must be null for a never-saved profile"


# --------------------------------------------------------------------------
# 3. PUT /profile — first save creates exactly one row
# --------------------------------------------------------------------------


def test_put_profile_first_save_creates_exactly_one_row(client, fake_otp_provider, db_session):
    user = create_verified_user(client, fake_otp_provider)

    resp = client.put("/profile", json=FULL_PROFILE_PAYLOAD)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["profile_id"] is not None, "a saved profile must return a non-null profile_id"
    assert body["company_name"] == FULL_PROFILE_PAYLOAD["company_name"]
    assert body["industry"] == FULL_PROFILE_PAYLOAD["industry"]
    assert body["product_lines"] == FULL_PROFILE_PAYLOAD["product_lines"]
    assert body["revenue_currency"] == FULL_PROFILE_PAYLOAD["revenue_currency"]
    assert body["target_customer_description"] == FULL_PROFILE_PAYLOAD["target_customer_description"]
    assert body["target_regions"] == FULL_PROFILE_PAYLOAD["target_regions"]

    rows = (
        db_session.execute(
            select(SellerProfile).where(SellerProfile.user_id == uuid.UUID(user["user_id"]))
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, "a first PUT /profile must create exactly one seller_profiles row"
    assert str(rows[0].profile_id) == body["profile_id"]
    assert str(rows[0].user_id) == user["user_id"]


# --------------------------------------------------------------------------
# 4. GET /profile after a save returns the saved values
# --------------------------------------------------------------------------


def test_get_profile_after_save_returns_saved_values(client, fake_otp_provider):
    create_verified_user(client, fake_otp_provider)
    put_resp = client.put("/profile", json=FULL_PROFILE_PAYLOAD)
    assert put_resp.status_code == 200, put_resp.text

    resp = client.get("/profile")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["profile_id"] == put_resp.json()["profile_id"]
    for field in PROFILE_OUT_FIELDS:
        if field == "last_year_revenue":
            assert float(body[field]) == float(FULL_PROFILE_PAYLOAD[field])
        else:
            assert body[field] == FULL_PROFILE_PAYLOAD[field]


# --------------------------------------------------------------------------
# 5. Second PUT updates the same row (never a second one); updated_at advances
# --------------------------------------------------------------------------


def test_put_profile_second_time_updates_same_row_and_advances_updated_at(
    client, fake_otp_provider, db_session
):
    user = create_verified_user(client, fake_otp_provider)
    user_uuid = uuid.UUID(user["user_id"])

    first = client.put("/profile", json=FULL_PROFILE_PAYLOAD)
    assert first.status_code == 200, first.text
    first_body = first.json()

    first_row = db_session.execute(
        select(SellerProfile).where(SellerProfile.user_id == user_uuid)
    ).scalar_one()
    first_created_at = first_row.created_at
    first_updated_at = first_row.updated_at
    db_session.expire_all()  # force a fresh read below, not this session's identity-mapped cache

    updated_payload = dict(FULL_PROFILE_PAYLOAD, company_name="Thermax Renamed Pvt Ltd")
    second = client.put("/profile", json=updated_payload)
    assert second.status_code == 200, second.text
    second_body = second.json()

    assert second_body["profile_id"] == first_body["profile_id"], (
        "a second PUT /profile for the same user must update the same row, identified "
        "by the same profile_id, not create a new one"
    )
    assert second_body["company_name"] == "Thermax Renamed Pvt Ltd"

    second_row = db_session.execute(
        select(SellerProfile).where(SellerProfile.user_id == user_uuid)
    ).scalar_one()
    assert second_row.profile_id == first_row.profile_id
    assert second_row.created_at == first_created_at, "created_at must never change on update"
    assert second_row.updated_at > first_updated_at, (
        "updated_at must advance on every subsequent PUT /profile"
    )

    count = db_session.execute(
        select(func.count()).select_from(SellerProfile).where(SellerProfile.user_id == user_uuid)
    ).scalar_one()
    assert count == 1, "seller_profiles must still have exactly one row for this user_id after a second PUT"


# --------------------------------------------------------------------------
# 6. Omitting a field on PUT leaves the existing value unchanged
# --------------------------------------------------------------------------


def test_put_profile_omitting_target_regions_leaves_it_unchanged(client, fake_otp_provider):
    create_verified_user(client, fake_otp_provider)
    first = client.put("/profile", json=FULL_PROFILE_PAYLOAD)
    assert first.status_code == 200, first.text

    partial_payload = {k: v for k, v in FULL_PROFILE_PAYLOAD.items() if k != "target_regions"}
    second = client.put("/profile", json=partial_payload)

    assert second.status_code == 200, second.text
    assert second.json()["target_regions"] == FULL_PROFILE_PAYLOAD["target_regions"], (
        "omitting target_regions from a PUT body must leave the existing stored value "
        "unchanged, not null it out"
    )

    # Confirmed again via a fresh GET, independent of the PUT response itself.
    get_resp = client.get("/profile")
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["target_regions"] == FULL_PROFILE_PAYLOAD["target_regions"]


# --------------------------------------------------------------------------
# 7. Cross-user isolation
# --------------------------------------------------------------------------


def test_put_profile_never_touches_another_users_row(client, fake_otp_provider, db_session):
    user_a = create_verified_user(client, fake_otp_provider)
    a_resp = client.put("/profile", json=FULL_PROFILE_PAYLOAD)
    assert a_resp.status_code == 200, a_resp.text
    a_profile_id = a_resp.json()["profile_id"]

    with TestClient(fastapi_app) as other_client:
        user_b = create_verified_user(other_client, fake_otp_provider)
        b_payload = dict(FULL_PROFILE_PAYLOAD, company_name="A Totally Different Company")
        b_resp = other_client.put("/profile", json=b_payload)
        assert b_resp.status_code == 200, b_resp.text
        b_profile_id = b_resp.json()["profile_id"]

    assert b_profile_id != a_profile_id, "each user's PUT /profile must own a distinct row"

    a_row = db_session.execute(
        select(SellerProfile).where(SellerProfile.user_id == uuid.UUID(user_a["user_id"]))
    ).scalar_one()
    b_row = db_session.execute(
        select(SellerProfile).where(SellerProfile.user_id == uuid.UUID(user_b["user_id"]))
    ).scalar_one()

    assert str(a_row.profile_id) == a_profile_id
    assert str(b_row.profile_id) == b_profile_id
    assert a_row.company_name == FULL_PROFILE_PAYLOAD["company_name"], (
        "user B's PUT /profile must never modify user A's row"
    )
    assert b_row.company_name == "A Totally Different Company"

    total = db_session.execute(select(func.count()).select_from(SellerProfile)).scalar_one()
    assert total == 2, "exactly one seller_profiles row must exist per user"

    # A re-fetch on A's own session must still show A's own data, unaffected by B's write.
    a_get = client.get("/profile")
    assert a_get.status_code == 200, a_get.text
    assert a_get.json()["profile_id"] == a_profile_id
    assert a_get.json()["company_name"] == FULL_PROFILE_PAYLOAD["company_name"]


# --------------------------------------------------------------------------
# 8. revenue_currency defaults to 'INR' and is never nulled out when omitted
#    ("Rules for implementation": revenue_currency defaults to 'INR' at the
#    DB level; SellerProfileUpdate treats it as optional and upsert_profile
#    never overwrites it with null if omitted.)
# --------------------------------------------------------------------------


def test_put_profile_first_save_without_revenue_currency_defaults_to_inr(
    client, fake_otp_provider
):
    create_verified_user(client, fake_otp_provider)
    payload = {k: v for k, v in FULL_PROFILE_PAYLOAD.items() if k != "revenue_currency"}

    resp = client.put("/profile", json=payload)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["profile_id"] is not None
    assert body["revenue_currency"] == "INR", (
        "omitting revenue_currency on a first-ever save must fall back to the "
        "seller_profiles DB default 'INR', not null"
    )


def test_put_profile_omitting_revenue_currency_on_update_leaves_existing_value_unchanged(
    client, fake_otp_provider
):
    create_verified_user(client, fake_otp_provider)
    first = client.put("/profile", json=FULL_PROFILE_PAYLOAD)
    assert first.status_code == 200, first.text
    assert first.json()["revenue_currency"] == "USD", "sanity check on the seeded payload"

    payload_without_currency = {
        k: v for k, v in FULL_PROFILE_PAYLOAD.items() if k != "revenue_currency"
    }
    second = client.put("/profile", json=payload_without_currency)

    assert second.status_code == 200, second.text
    assert second.json()["revenue_currency"] == "USD", (
        "omitting revenue_currency from a PUT body on an existing profile must leave "
        "the previously stored currency unchanged, never reset it to the 'INR' default "
        "and never null it out"
    )

    get_resp = client.get("/profile")
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["revenue_currency"] == "USD"


# --------------------------------------------------------------------------
# 9. General partial-update rule: any single field present in the PUT body
#    updates only that field; every other previously-saved field is left
#    untouched (spec: "A field omitted from the request body leaves that
#    column unchanged" applies to every column, not just target_regions).
# --------------------------------------------------------------------------


def test_put_profile_partial_update_single_field_leaves_all_other_fields_unchanged(
    client, fake_otp_provider
):
    create_verified_user(client, fake_otp_provider)
    first = client.put("/profile", json=FULL_PROFILE_PAYLOAD)
    assert first.status_code == 200, first.text

    resp = client.put("/profile", json={"industry": "Renewable Energy Equipment"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["industry"] == "Renewable Energy Equipment"
    for field in PROFILE_OUT_FIELDS:
        if field == "industry":
            continue
        if field == "last_year_revenue":
            assert float(body[field]) == float(FULL_PROFILE_PAYLOAD[field]), (
                f"{field} must be unaffected by a PUT that only sets industry"
            )
        else:
            assert body[field] == FULL_PROFILE_PAYLOAD[field], (
                f"{field} must be unaffected by a PUT that only sets industry"
            )

    # Confirmed independently via a fresh GET.
    get_resp = client.get("/profile")
    assert get_resp.status_code == 200, get_resp.text
    get_body = get_resp.json()
    assert get_body["industry"] == "Renewable Energy Equipment"
    assert get_body["company_name"] == FULL_PROFILE_PAYLOAD["company_name"]
    assert get_body["target_regions"] == FULL_PROFILE_PAYLOAD["target_regions"]


# --------------------------------------------------------------------------
# 10. Validation: last_year_revenue must be numeric; a malformed value is a
#     422, not a silently-accepted/garbled write.
# --------------------------------------------------------------------------


def test_put_profile_with_non_numeric_last_year_revenue_returns_422(client, fake_otp_provider):
    create_verified_user(client, fake_otp_provider)
    payload = dict(FULL_PROFILE_PAYLOAD, last_year_revenue="not-a-number")

    resp = client.put("/profile", json=payload)

    assert resp.status_code == 422, (
        f"a malformed last_year_revenue must be rejected with 422, got {resp.status_code}: "
        f"{resp.text}"
    )

    # And no row must have been created/corrupted by the rejected request.
    get_resp = client.get("/profile")
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["profile_id"] is None, (
        "a rejected PUT must not leave behind a partially-written seller_profiles row"
    )


# --------------------------------------------------------------------------
# 11. profile_id is never accepted as a request parameter: a client-supplied
#     profile_id in the PUT body must never be used to select or create a
#     row — the row is always looked up by the authenticated user_id only.
# --------------------------------------------------------------------------


def test_put_profile_ignores_client_supplied_profile_id(client, fake_otp_provider, db_session):
    user = create_verified_user(client, fake_otp_provider)
    user_uuid = uuid.UUID(user["user_id"])

    first = client.put("/profile", json=FULL_PROFILE_PAYLOAD)
    assert first.status_code == 200, first.text
    real_profile_id = first.json()["profile_id"]

    foreign_profile_id = str(uuid.uuid4())
    tampered_payload = dict(
        FULL_PROFILE_PAYLOAD,
        profile_id=foreign_profile_id,
        company_name="Renamed Via Tampered Request",
    )
    second = client.put("/profile", json=tampered_payload)

    # Whether the server ignores the unrecognized `profile_id` field (200) or
    # rejects it outright, the one outcome that must never happen is a row
    # keyed by the client-supplied profile_id, or a second row for this user.
    rows_for_user = (
        db_session.execute(select(SellerProfile).where(SellerProfile.user_id == user_uuid))
        .scalars()
        .all()
    )
    assert len(rows_for_user) == 1, (
        "a client-supplied profile_id must never cause a second row to be created "
        "for the same user_id"
    )
    assert str(rows_for_user[0].profile_id) == real_profile_id, (
        "the caller's own row must still be keyed by its original profile_id, never "
        "by a client-supplied value"
    )
    assert str(rows_for_user[0].profile_id) != foreign_profile_id

    foreign_id_rows = db_session.execute(
        select(func.count())
        .select_from(SellerProfile)
        .where(SellerProfile.profile_id == uuid.UUID(foreign_profile_id))
    ).scalar_one()
    assert foreign_id_rows == 0, (
        "the client-supplied profile_id must never be used to create or address a row"
    )

    if second.status_code == 200:
        assert second.json()["profile_id"] == real_profile_id, (
            "if the request is accepted, it must still operate on the caller's own "
            "existing row, identified by its real profile_id"
        )
