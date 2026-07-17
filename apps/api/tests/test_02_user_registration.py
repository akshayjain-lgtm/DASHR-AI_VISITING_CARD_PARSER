"""
Tests for the `02-user-registration` feature (spec:
`.claude/specs/02-user-registration.md`).

These tests are written directly against the spec's documented contract, not
against the implementation:

- Two-step signup (`POST /auth/signup` -> `POST /auth/signup/verify-otp`); no
  session cookie is issued until the phone number is verified.
- `POST /auth/signup/verify-otp` returns the SAME generic
  `400 "Invalid or expired code"` for a wrong code, an expired code, or a
  code whose attempt cap (5) has been exhausted — the spec's Rules section
  states this exact string, so it is asserted verbatim.
- OTP resend is rate-limited to one per 30 seconds per user, checked against
  `phone_otp_verifications.created_at` (spec's Rules section) — tests
  backdate that column directly rather than sleeping, per the "never use
  time.sleep()" rule.
- A verified phone number is unique across accounts (partial unique index on
  `users.phone_no WHERE phone_verified = true`); unverified accounts may
  share a phone number in-flight. Since 17-admin-user-management, signup
  itself rejects a phone number already verified on another account with a
  `409` before creating a user row or sending an OTP — the older contract
  (only *verification* enforcing this) still holds for the genuine race
  where both accounts sign up before either verifies; that case still fails
  cleanly with `409` at verify-otp time, never a raw 500.
- Users created here always have `org_id = NULL, role = NULL` — org
  creation/invites are out of scope for this feature, so there is no
  tenant-isolation test in this file; instead we assert org_id/role are None.

The OTP provider (`app.deps.get_otp_provider`) is mocked for every test via a
`FakeOtpProvider` dependency override (see `conftest.py`) — no test ever
sends/logs a real code or talks to a real SMS gateway.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from app.deps import COOKIE_NAME
from app.models.phone_otp_verification import PhoneOtpVerification
from app.models.user import User
from conftest import VALID_PASSWORD
from conftest import signup_payload as _signup_payload
from conftest import unique_email as _unique_email
from conftest import unique_phone as _unique_phone


# --------------------------------------------------------------------------
# Test data helpers
# --------------------------------------------------------------------------


def _wrong_code(real_code: str) -> str:
    """A 6-digit code guaranteed to differ from `real_code`."""
    digits = list(real_code)
    digits[0] = "9" if digits[0] != "9" else "8"
    return "".join(digits)


def _to_uuid(value: str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(value)


def _backdate_otp_created_at(db_session, user_id: str, seconds: int) -> None:
    """Simulate elapsed time for the resend cooldown, without `time.sleep()`.

    Only shifts `created_at` (what the spec says the cooldown is checked
    against) — `expires_at` is left alone so this never accidentally makes
    the code look expired too.
    """
    now = datetime.now(timezone.utc)
    db_session.execute(
        update(PhoneOtpVerification)
        .where(PhoneOtpVerification.user_id == _to_uuid(user_id))
        .where(PhoneOtpVerification.verified_at.is_(None))
        .values(created_at=now - timedelta(seconds=seconds))
    )
    db_session.commit()


def _expire_otp(db_session, user_id: str) -> None:
    """Simulate an expired OTP by backdating `expires_at` into the past."""
    now = datetime.now(timezone.utc)
    db_session.execute(
        update(PhoneOtpVerification)
        .where(PhoneOtpVerification.user_id == _to_uuid(user_id))
        .where(PhoneOtpVerification.verified_at.is_(None))
        .values(expires_at=now - timedelta(minutes=1))
    )
    db_session.commit()


def _latest_otp_row(db_session, user_id: str) -> PhoneOtpVerification:
    return db_session.execute(
        select(PhoneOtpVerification)
        .where(PhoneOtpVerification.user_id == _to_uuid(user_id))
        .order_by(PhoneOtpVerification.created_at.desc())
    ).scalars().first()


# --------------------------------------------------------------------------
# 1. Happy path signup
# --------------------------------------------------------------------------


def test_signup_new_user_creates_account_no_session_org_and_role_null(client, fake_otp_provider, db_session):
    payload = _signup_payload()

    resp = client.post("/auth/signup", json=payload)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["phone_no"] == payload["phone_no"]
    assert body.get("user_id"), "signup must return the new user_id"
    assert "set-cookie" not in resp.headers, (
        "signup alone must never issue a session cookie — only verify-otp does"
    )

    user_row = db_session.execute(
        select(User).where(User.email == payload["email"])
    ).scalar_one()
    assert str(user_row.user_id) == body["user_id"]
    assert user_row.org_id is None, "a freshly signed-up user must have org_id = NULL"
    assert user_row.role is None, "a freshly signed-up user must have role = NULL"
    assert user_row.phone_verified is False, "phone must not be verified before OTP verification"
    assert user_row.password_hash is not None
    assert user_row.password_hash != payload["password"], "password must be hashed, never stored in plaintext"

    otp_row = _latest_otp_row(db_session, body["user_id"])
    assert otp_row is not None, "signup must create a phone_otp_verifications row"
    assert otp_row.attempts == 0
    assert otp_row.verified_at is None
    assert otp_row.phone_no == payload["phone_no"]

    real_code = fake_otp_provider.latest_code_for(payload["phone_no"])
    assert len(real_code) == 4 and real_code.isdigit(), "OTP sent to the provider must be a 4-digit code"
    assert otp_row.otp_code_hash != real_code, "the OTP code must be hashed at rest, never stored raw"


# --------------------------------------------------------------------------
# 2. Duplicate email
# --------------------------------------------------------------------------


def test_signup_duplicate_email_returns_409_and_creates_no_second_row(client, db_session):
    email = _unique_email()
    first = client.post("/auth/signup", json=_signup_payload(email=email))
    assert first.status_code == 201, first.text

    second = client.post(
        "/auth/signup", json=_signup_payload(email=email, phone_no=_unique_phone())
    )

    assert second.status_code == 409, second.text
    assert "detail" in second.json()
    assert "set-cookie" not in second.headers

    count = db_session.execute(
        select(func.count()).select_from(User).where(User.email == email)
    ).scalar_one()
    assert count == 1, "a duplicate-email signup must not create a second users row"


# --------------------------------------------------------------------------
# 3. Correct OTP verifies, sets session, GET /auth/me reflects it
# --------------------------------------------------------------------------


def test_verify_otp_correct_code_logs_in_user_and_me_reflects_it(client, fake_otp_provider):
    payload = _signup_payload()
    signup = client.post("/auth/signup", json=payload)
    user_id = signup.json()["user_id"]
    code = fake_otp_provider.latest_code_for(payload["phone_no"])

    resp = client.post(
        "/auth/signup/verify-otp", json={"user_id": user_id, "otp_code": code}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == user_id
    assert body["email"] == payload["email"]
    assert body["phone_no"] == payload["phone_no"]
    assert body["phone_verified"] is True
    assert body["org_id"] is None
    assert body["role"] is None

    set_cookie = resp.headers.get("set-cookie", "")
    assert set_cookie, "verify-otp success must set the session cookie"
    assert COOKIE_NAME in set_cookie
    assert "httponly" in set_cookie.lower(), "session cookie must be httpOnly"
    assert "samesite=lax" in set_cookie.lower(), "session cookie must be SameSite=Lax"

    me = client.get("/auth/me")
    assert me.status_code == 200, me.text
    me_body = me.json()
    assert me_body["user_id"] == user_id
    assert me_body["phone_verified"] is True
    assert me_body["org_id"] is None
    assert me_body["role"] is None


# --------------------------------------------------------------------------
# 4. Wrong OTP code
# --------------------------------------------------------------------------


def test_verify_otp_wrong_code_increments_attempts_and_returns_generic_400(
    client, fake_otp_provider, db_session
):
    payload = _signup_payload()
    signup = client.post("/auth/signup", json=payload)
    user_id = signup.json()["user_id"]
    real_code = fake_otp_provider.latest_code_for(payload["phone_no"])
    bad_code = _wrong_code(real_code)

    resp = client.post(
        "/auth/signup/verify-otp", json={"user_id": user_id, "otp_code": bad_code}
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Invalid or expired code", (
        "spec mandates the exact same generic message for wrong/expired/exhausted codes"
    )
    assert "set-cookie" not in resp.headers

    otp_row = _latest_otp_row(db_session, user_id)
    assert otp_row.attempts == 1, "a failed verify attempt must increment the attempts counter"
    assert otp_row.verified_at is None


# --------------------------------------------------------------------------
# 5. 5 wrong attempts locks out even the correct code
# --------------------------------------------------------------------------


def test_five_wrong_attempts_locks_out_even_the_correct_code(client, fake_otp_provider, db_session):
    payload = _signup_payload()
    signup = client.post("/auth/signup", json=payload)
    user_id = signup.json()["user_id"]
    real_code = fake_otp_provider.latest_code_for(payload["phone_no"])
    bad_code = _wrong_code(real_code)

    for attempt in range(1, 6):
        resp = client.post(
            "/auth/signup/verify-otp", json={"user_id": user_id, "otp_code": bad_code}
        )
        assert resp.status_code == 400, f"wrong-code attempt #{attempt} should return 400"

    otp_row = _latest_otp_row(db_session, user_id)
    assert otp_row.attempts >= 5, "attempts must reach the 5-attempt cap after 5 failed tries"
    assert otp_row.verified_at is None

    locked_out = client.post(
        "/auth/signup/verify-otp", json={"user_id": user_id, "otp_code": real_code}
    )
    assert locked_out.status_code == 400, (
        "the correct code must be rejected once the attempt cap is exhausted, requiring a resend"
    )
    assert "set-cookie" not in locked_out.headers

    me = client.get("/auth/me")
    assert me.status_code == 401, "no session should ever be issued for a locked-out account"


# --------------------------------------------------------------------------
# 6. Expired OTP
# --------------------------------------------------------------------------


def test_verify_otp_expired_code_returns_400(client, fake_otp_provider, db_session):
    payload = _signup_payload()
    signup = client.post("/auth/signup", json=payload)
    user_id = signup.json()["user_id"]
    real_code = fake_otp_provider.latest_code_for(payload["phone_no"])

    _expire_otp(db_session, user_id)

    resp = client.post(
        "/auth/signup/verify-otp", json={"user_id": user_id, "otp_code": real_code}
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Invalid or expired code"
    assert "set-cookie" not in resp.headers


# --------------------------------------------------------------------------
# 7. Resend OTP: cooldown, then success once the cooldown has elapsed
# --------------------------------------------------------------------------


def test_resend_otp_within_cooldown_returns_429(client, fake_otp_provider, db_session):
    payload = _signup_payload()
    signup = client.post("/auth/signup", json=payload)
    user_id = signup.json()["user_id"]
    phone = payload["phone_no"]

    # Backdate the signup-created OTP well past the cooldown window so the
    # first resend call below is unambiguously allowed — isolating what
    # we're actually testing: a SECOND call within 30s of that first one.
    _backdate_otp_created_at(db_session, user_id, seconds=60)

    first = client.post("/auth/signup/resend-otp", json={"user_id": user_id})
    assert first.status_code < 300, (
        f"a resend issued well after the cooldown window should succeed, got "
        f"{first.status_code}: {first.text}"
    )
    assert fake_otp_provider.count_sent(phone) == 2, "resend must trigger exactly one new OTP send"

    second = client.post("/auth/signup/resend-otp", json={"user_id": user_id})

    assert second.status_code == 429, "a resend within 30 seconds of the previous one must be rate-limited"
    assert fake_otp_provider.count_sent(phone) == 2, "a rate-limited resend must not send another code"


def test_resend_otp_success_replaces_the_pending_row_and_the_new_code_works(
    client, fake_otp_provider, db_session
):
    """`generate_otp_code()` is intentionally hardcoded to "1234" until a real
    SMS provider is wired up (see `core/security.py`), so the resend-issued
    code is not expected to differ in *value* from the one it replaces —
    only in *row identity*. What's actually verifiable here is that resend
    deletes the old pending row and inserts a fresh one (never leaves two
    pending rows behind), and that the freshly issued row is what
    verify-otp checks against."""
    payload = _signup_payload()
    signup = client.post("/auth/signup", json=payload)
    user_id = signup.json()["user_id"]
    phone = payload["phone_no"]
    original_row = db_session.scalar(
        select(PhoneOtpVerification).where(
            PhoneOtpVerification.user_id == _to_uuid(user_id)
        )
    )
    original_otp_id = original_row.otp_id

    _backdate_otp_created_at(db_session, user_id, seconds=60)

    resend = client.post("/auth/signup/resend-otp", json={"user_id": user_id})
    assert resend.status_code < 300, resend.text

    pending_rows = db_session.scalars(
        select(PhoneOtpVerification).where(
            PhoneOtpVerification.user_id == _to_uuid(user_id),
            PhoneOtpVerification.verified_at.is_(None),
        )
    ).all()
    assert len(pending_rows) == 1, "resend must replace the pending OTP row, never append a second one"
    assert pending_rows[0].otp_id != original_otp_id, (
        "resend must issue a new row rather than reusing the original one"
    )

    new_code = fake_otp_provider.latest_code_for(phone)
    fresh = client.post(
        "/auth/signup/verify-otp", json={"user_id": user_id, "otp_code": new_code}
    )
    assert fresh.status_code == 200, "the newly issued code from resend-otp must work"
    assert fresh.json()["phone_verified"] is True


# --------------------------------------------------------------------------
# 8. Cross-account phone reuse
# --------------------------------------------------------------------------


def test_signup_with_already_verified_phone_returns_409_and_sends_no_otp(
    client, fake_otp_provider, db_session
):
    """Since 17-admin-user-management: a phone number already verified on
    another account is rejected at signup time — before a second user row
    is ever created or a second OTP ever sent — rather than only surfacing
    at verify-otp time. This is the common (non-racing) case; the genuine
    race where both accounts sign up before either verifies is covered
    separately below, since signup itself can't reject that one."""
    shared_phone = _unique_phone()

    signup_a = client.post(
        "/auth/signup", json=_signup_payload(phone_no=shared_phone, name="Account A")
    )
    assert signup_a.status_code == 201, signup_a.text
    user_a_id = signup_a.json()["user_id"]
    code_a = fake_otp_provider.latest_code_for(shared_phone)

    verify_a = client.post(
        "/auth/signup/verify-otp", json={"user_id": user_a_id, "otp_code": code_a}
    )
    assert verify_a.status_code == 200, verify_a.text
    assert verify_a.json()["phone_verified"] is True

    sent_before = fake_otp_provider.count_sent(shared_phone)

    signup_b = client.post(
        "/auth/signup", json=_signup_payload(phone_no=shared_phone, name="Account B")
    )

    assert signup_b.status_code == 409, (
        f"signup with an already-verified phone number must fail immediately, "
        f"got {signup_b.status_code}: {signup_b.text}"
    )
    assert "detail" in signup_b.json()
    assert fake_otp_provider.count_sent(shared_phone) == sent_before, (
        "no OTP should ever be sent for a signup rejected before account creation"
    )

    rows = db_session.execute(select(User).where(User.phone_no == shared_phone)).scalars().all()
    assert len(rows) == 1, "no second user row should exist for the rejected signup"
    assert str(rows[0].user_id) == user_a_id


def test_verify_otp_cross_account_phone_reuse_race_fails_cleanly_not_500(
    client, fake_otp_provider, db_session
):
    """The genuine race: both accounts sign up while the phone is still
    unverified (allowed in-flight, per spec — signup's early check can't
    catch this since neither is verified yet). Whichever verifies second
    must fail cleanly with 409, and — since the phone conflict is checked
    before verify_otp() marks the code used — the loser's own OTP must
    survive unconsumed rather than being burned on a doomed attempt."""
    shared_phone = _unique_phone()

    signup_a = client.post(
        "/auth/signup", json=_signup_payload(phone_no=shared_phone, name="Account A")
    )
    assert signup_a.status_code == 201, signup_a.text
    user_a_id = signup_a.json()["user_id"]
    code_a = fake_otp_provider.latest_code_for(shared_phone)

    signup_b = client.post(
        "/auth/signup", json=_signup_payload(phone_no=shared_phone, name="Account B")
    )
    assert signup_b.status_code == 201, (
        "signup itself must not be blocked by an in-flight (unverified) phone-number collision"
    )
    user_b_id = signup_b.json()["user_id"]
    code_b = fake_otp_provider.latest_code_for(shared_phone)

    verify_a = client.post(
        "/auth/signup/verify-otp", json={"user_id": user_a_id, "otp_code": code_a}
    )
    assert verify_a.status_code == 200, verify_a.text

    verify_b = client.post(
        "/auth/signup/verify-otp", json={"user_id": user_b_id, "otp_code": code_b}
    )

    assert verify_b.status_code == 409, (
        f"cross-account verified-phone reuse must fail cleanly with 409, "
        f"got {verify_b.status_code}: {verify_b.text}"
    )
    assert "detail" in verify_b.json()
    assert "set-cookie" not in verify_b.headers, "a failed cross-account verify must not issue a session"

    # Exactly one account (A) should end up phone_verified for this number —
    # B's failed verification must not have partially applied.
    verified_rows = db_session.execute(
        select(User).where(User.phone_no == shared_phone, User.phone_verified.is_(True))
    ).scalars().all()
    assert len(verified_rows) == 1
    assert str(verified_rows[0].user_id) == user_a_id

    user_b_row = db_session.execute(
        select(User).where(User.user_id == _to_uuid(user_b_id))
    ).scalar_one()
    assert user_b_row.phone_verified is False

    # B's own OTP must not have been consumed by the doomed attempt — the
    # phone-conflict check runs before verify_otp() marks it used.
    otp_b_row = db_session.execute(
        select(PhoneOtpVerification).where(PhoneOtpVerification.user_id == _to_uuid(user_b_id))
    ).scalar_one()
    assert otp_b_row.verified_at is None, (
        "a rejected cross-account verify must not burn the caller's own OTP code"
    )


# --------------------------------------------------------------------------
# 9. GET /auth/me auth guard
# --------------------------------------------------------------------------


def test_me_without_cookie_returns_401(client):
    resp = client.get("/auth/me")
    assert resp.status_code == 401


def test_me_with_invalid_cookie_returns_401(client):
    client.cookies.set(COOKIE_NAME, "not-a-real-jwt-token")

    resp = client.get("/auth/me")

    assert resp.status_code == 401


# --------------------------------------------------------------------------
# 10. Validation errors
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"phone_no": "9876543210"}, "phone number missing the +91 country code"),
        ({"phone_no": "+91512345678"}, "phone number's first digit outside the valid 6-9 range"),
        ({"phone_no": "+9298765432"}, "wrong country code / wrong digit count"),
        ({"password": "short1"}, "password under 8 characters"),
        ({"name": "A" * 250}, "name far exceeding the allowed length"),
    ],
)
def test_signup_validation_errors_return_422(client, overrides, reason):
    payload = _signup_payload(**overrides)

    resp = client.post("/auth/signup", json=payload)

    assert resp.status_code == 422, f"expected 422 for {reason}, got {resp.status_code}: {resp.text}"
    assert "detail" in resp.json(), "FastAPI validation errors must include a useful `detail` payload"
