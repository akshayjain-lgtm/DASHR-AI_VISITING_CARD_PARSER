"""
Tests for the `03-user-login-logout` feature (spec:
`.claude/specs/03-user-login-logout.md`).

These tests are written directly against the spec's documented contract, not
against the implementation of `routers/auth.py`, `services/auth_service.py`,
or `schemas/auth.py`:

- `POST /auth/login` — public — body `{ email, password }` -> on success,
  `200` + `{ user_id, name, email }` + the session cookie set (same shape as
  signup's: httpOnly, `Secure` per config, `SameSite=Lax`). On failure — wrong
  password OR unknown email — the spec's Rules section mandates the *exact
  same* generic `401 "Invalid email or password"` for both, so no cookie is
  ever set and no caller can distinguish "no such account" from "wrong
  password" (no user enumeration). The spec does not document any additional
  phone-verification gate on login, so none is asserted here.
- `POST /auth/logout` — reuses `get_current_user()` from `deps.py` unchanged
  (the same auth guard already covered for `GET /auth/me` in
  `test_02_user_registration.py`) — so no session -> `401`. With a valid
  session it clears the cookie server-side and returns `204`; a forged/
  invalid cookie also gets `401`, same as any other protected route.
- The session cookie contract (httpOnly/Secure/SameSite=Lax) is one contract
  for the whole app — this file asserts login's cookie has the same shape
  already asserted for signup's verify-otp cookie in
  `test_02_user_registration.py`. The test environment pins
  `COOKIE_SECURE=false` (see `conftest.py`) so cookies work over plain HTTP
  in TestClient; the `Secure` attribute is therefore asserted *absent* here,
  reflecting that config value, not a spec violation — production config
  (`COOKIE_SECURE=true`) would flip this per the spec's "Secure per config"
  requirement.

The OTP provider (`app.deps.get_otp_provider`) is mocked for every test via
the `fake_otp_provider` fixture (see `conftest.py`) — needed only to get a
verified account into place as a precondition; no test in this file sends or
inspects an OTP code directly (that's `02-user-registration`'s job).
"""

from __future__ import annotations

import pytest

from app.deps import COOKIE_NAME
from app.models.user import User
from conftest import VALID_PASSWORD, create_verified_user, unique_email


# --------------------------------------------------------------------------
# Cookie-parsing helpers (Set-Cookie header shape assertions)
# --------------------------------------------------------------------------


def _cookie_attrs(set_cookie_header: str) -> set[str]:
    """Lowercased, whitespace-stripped `;`-separated attribute tokens.

    e.g. `"dashr_session=abc; HttpOnly; SameSite=Lax"` -> {"dashr_session=abc",
    "httponly", "samesite=lax"}.
    """
    return {part.strip().lower() for part in set_cookie_header.split(";")}


def _cookie_value(set_cookie_header: str, name: str) -> str:
    """The raw value of cookie `name` from a `Set-Cookie` header."""
    first_part = set_cookie_header.split(";")[0].strip()
    key, _, value = first_part.partition("=")
    assert key == name, f"expected cookie name {name!r} in Set-Cookie, got {key!r}"
    return value


# --------------------------------------------------------------------------
# 1. POST /auth/login — happy path
# --------------------------------------------------------------------------


def test_login_correct_credentials_returns_200_sets_session_cookie_and_reflects_user(
    client, fake_otp_provider
):
    user = create_verified_user(client, fake_otp_provider, name="Riya Kapoor")

    resp = client.post(
        "/auth/login", json={"email": user["email"], "password": user["password"]}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == user["user_id"]
    assert body["email"] == user["email"]
    assert body["name"] == "Riya Kapoor"
    assert "password" not in body, "the plaintext password must never be echoed back"
    assert "password_hash" not in body, "the password hash must never be echoed back"

    set_cookie = resp.headers.get("set-cookie", "")
    assert set_cookie, "a successful login must set the session cookie"
    assert COOKIE_NAME in set_cookie

    attrs = _cookie_attrs(set_cookie)
    assert "httponly" in attrs, f"session cookie must be httpOnly, got attrs={attrs}"
    assert "samesite=lax" in attrs, f"session cookie must be SameSite=Lax, got attrs={attrs}"
    assert "secure" not in attrs, (
        "test config pins COOKIE_SECURE=false, so the Secure attribute must be absent "
        f"here (would be present under COOKIE_SECURE=true per config) — got attrs={attrs}"
    )

    # The cookie login issued must actually authenticate a follow-up request —
    # not just look right on the wire.
    me = client.get("/auth/me")
    assert me.status_code == 200, "the session cookie issued by login must authenticate /auth/me"
    assert me.json()["user_id"] == user["user_id"]


# --------------------------------------------------------------------------
# 2. POST /auth/login — wrong password
# --------------------------------------------------------------------------


def test_login_wrong_password_returns_401_generic_message_and_sets_no_cookie(
    client, fake_otp_provider
):
    user = create_verified_user(client, fake_otp_provider)

    resp = client.post(
        "/auth/login", json={"email": user["email"], "password": "TotallyWrongPass1!"}
    )

    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Invalid email or password"
    assert "set-cookie" not in resp.headers, "a failed login must never set a session cookie"


# --------------------------------------------------------------------------
# 3. POST /auth/login — unknown email, identical to the wrong-password case
# --------------------------------------------------------------------------


def test_login_unknown_email_returns_401_and_sets_no_cookie(client):
    resp = client.post(
        "/auth/login", json={"email": unique_email(), "password": VALID_PASSWORD}
    )

    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Invalid email or password"
    assert "set-cookie" not in resp.headers, "a failed login must never set a session cookie"


def test_login_wrong_password_and_unknown_email_return_byte_for_byte_identical_bodies(
    client, fake_otp_provider
):
    """Spec's Rules section: don't leak which one was wrong, so the two
    failure bodies must be indistinguishable — asserted here byte-for-byte,
    not just "both 401"."""
    user = create_verified_user(client, fake_otp_provider)

    wrong_password_resp = client.post(
        "/auth/login", json={"email": user["email"], "password": "TotallyWrongPass1!"}
    )
    unknown_email_resp = client.post(
        "/auth/login", json={"email": unique_email(), "password": VALID_PASSWORD}
    )

    assert wrong_password_resp.status_code == 401, wrong_password_resp.text
    assert unknown_email_resp.status_code == 401, unknown_email_resp.text

    wrong_password_detail = wrong_password_resp.json()["detail"]
    unknown_email_detail = unknown_email_resp.json()["detail"]

    assert wrong_password_detail == "Invalid email or password"
    assert wrong_password_detail == unknown_email_detail, (
        "wrong-password and unknown-email failures must return byte-for-byte identical "
        f"detail strings to prevent user enumeration — got {wrong_password_detail!r} vs "
        f"{unknown_email_detail!r}"
    )


# --------------------------------------------------------------------------
# 4. POST /auth/login — edge case: account with no password set
# --------------------------------------------------------------------------


def test_login_user_with_no_password_hash_returns_401_not_500(client, db_session):
    """A row the signup API can never itself produce (e.g. a future
    org-invite-only account) but the service layer must still fail this
    cleanly rather than raising on a None-vs-string bcrypt compare."""
    email = unique_email()
    user = User(name="No Password User", email=email, password_hash=None, phone_no=None)
    db_session.add(user)
    db_session.commit()

    resp = client.post("/auth/login", json={"email": email, "password": VALID_PASSWORD})

    assert resp.status_code == 401, (
        f"a user with no password_hash must fail login cleanly with 401, "
        f"got {resp.status_code}: {resp.text}"
    )
    assert resp.json()["detail"] == "Invalid email or password"
    assert "set-cookie" not in resp.headers


# --------------------------------------------------------------------------
# 5. POST /auth/login — validation errors
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({}, "empty body, missing both email and password"),
        ({"email": "user@example.com"}, "missing password field"),
        ({"password": VALID_PASSWORD}, "missing email field"),
        ({"email": "user@example.com", "password": 12345}, "password wrong type (int, not str)"),
        ({"email": 12345, "password": VALID_PASSWORD}, "email wrong type (int, not str)"),
        ({"email": "user@example.com", "password": None}, "password explicitly null"),
    ],
)
def test_login_validation_errors_return_422_and_set_no_cookie(client, payload, reason):
    resp = client.post("/auth/login", json=payload)

    assert resp.status_code == 422, f"expected 422 for {reason}, got {resp.status_code}: {resp.text}"
    assert "detail" in resp.json(), "FastAPI validation errors must include a useful `detail` payload"
    assert "set-cookie" not in resp.headers, f"no cookie should be set for a validation failure ({reason})"


# --------------------------------------------------------------------------
# 6. POST /auth/logout — auth guard (no session)
# --------------------------------------------------------------------------


def test_logout_without_session_returns_401(client):
    resp = client.post("/auth/logout")

    assert resp.status_code == 401, resp.text
    assert "set-cookie" not in resp.headers or COOKIE_NAME not in resp.headers.get(
        "set-cookie", ""
    ), "an unauthenticated logout attempt must not itself set/clear a session cookie"


def test_logout_with_forged_cookie_returns_401(client):
    client.cookies.set(COOKIE_NAME, "not-a-real-jwt-token")

    resp = client.post("/auth/logout")

    assert resp.status_code == 401, resp.text


# --------------------------------------------------------------------------
# 7. POST /auth/logout — happy path: clears the cookie, ends the session
# --------------------------------------------------------------------------


def test_logout_with_valid_session_returns_204_clears_cookie_and_ends_session(
    client, fake_otp_provider
):
    user = create_verified_user(client, fake_otp_provider)
    login = client.post(
        "/auth/login", json={"email": user["email"], "password": user["password"]}
    )
    assert login.status_code == 200, login.text

    resp = client.post("/auth/logout")

    assert resp.status_code == 204, resp.text
    assert resp.content == b"", "a 204 response must have an empty body"

    set_cookie = resp.headers.get("set-cookie", "")
    assert set_cookie, "logout must send a Set-Cookie header to clear the session cookie server-side"
    assert COOKIE_NAME in set_cookie

    cookie_value = _cookie_value(set_cookie, COOKIE_NAME)
    attrs = _cookie_attrs(set_cookie)
    assert cookie_value == "" or "max-age=0" in attrs or any(
        attr.startswith("expires=") for attr in attrs
    ), f"logout must clear the cookie via an empty value or Max-Age=0/Expires-in-the-past, got {set_cookie!r}"

    # The real proof the session actually ended, not just that headers "look"
    # like a clear: a follow-up request on the SAME client must now be
    # unauthenticated.
    me = client.get("/auth/me")
    assert me.status_code == 401, "GET /auth/me after logout must return 401 on the same client"


def test_logout_called_again_after_already_logged_out_returns_401(client, fake_otp_provider):
    """Confirms the clear is a real server-side invalidation, not decorative:
    once the client's cookie jar picks up the cleared cookie from the first
    logout, a second logout call has nothing valid to authenticate with."""
    user = create_verified_user(client, fake_otp_provider)
    login = client.post(
        "/auth/login", json={"email": user["email"], "password": user["password"]}
    )
    assert login.status_code == 200, login.text

    first_logout = client.post("/auth/logout")
    assert first_logout.status_code == 204, first_logout.text

    second_logout = client.post("/auth/logout")

    assert second_logout.status_code == 401, (
        "logging out a second time, after the session cookie was already cleared, must be "
        "unauthenticated — confirms logout genuinely ends the session rather than being a no-op"
    )
