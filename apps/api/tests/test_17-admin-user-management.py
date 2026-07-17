"""
Tests for the `17-admin-user-management` feature (spec:
`.claude/specs/17-admin-user-management.md`).

Written directly against the spec's documented contract:

- `POST /auth/signup` gains an optional `company_name`. Non-blank -> creates
  an `Organization` and makes the signer its admin. Blank/omitted -> the
  signer stays org-less, exactly as before this feature (regression guard
  against `02-user-registration`).
- `GET /orgs/invites/{token}` (public), `POST /orgs/invites`,
  `GET /orgs/invites`, `DELETE /orgs/invites/{invite_id}` (all admin-only),
  and `POST /orgs/invites/{token}/accept` (any authenticated user) implement
  invite create/list/revoke/preview/accept.
- `GET /orgs/my-invites` (any authenticated user) lists pending invites
  addressed to the caller's own email, including each invite's token — so an
  invitee can discover and accept an invite from their own account, not only
  via the out-of-band accept link.
- `GET /orgs/members` (admin-only) lists org members and must never expose
  wallet/balance fields.
- `PATCH /orgs/members/{user_id}/deactivate` and `.../reactivate` (admin-only)
  toggle `is_active`; a deactivated user's *existing* session must 401 on its
  very next request, not just on a future login attempt.
- `POST /orgs/members/{user_id}/make-admin` (admin-only) atomically transfers
  the single admin seat, leaving exactly one admin in the org.
- Every `/orgs/*` admin endpoint is scoped to the caller's own `org_id` —
  cross-tenant reads/writes must be invisible/blocked, not just filtered.

The OTP and invite-email providers are mocked for every test via the
`fake_otp_provider`/`fake_invite_email_provider` fixtures (see `conftest.py`).
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from conftest import create_verified_user, unique_email


def _login(client: TestClient, user: dict) -> None:
    resp = client.post("/auth/login", json={"email": user["email"], "password": user["password"]})
    assert resp.status_code == 200, resp.text


def _signup_and_login(client: TestClient, fake_otp_provider, **overrides) -> dict:
    user = create_verified_user(client, fake_otp_provider, **overrides)
    _login(client, user)
    return user


def _create_org_admin(client: TestClient, fake_otp_provider, company_name="Acme Manufacturing", **overrides) -> dict:
    return _signup_and_login(client, fake_otp_provider, company_name=company_name, **overrides)


def _me(client: TestClient) -> dict:
    resp = client.get("/auth/me")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_invite(client: TestClient, email: str) -> dict:
    resp = client.post("/orgs/invites", json={"email": email})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ==========================================================================
# 1. Signup + company_name -> org creation / admin assignment
# ==========================================================================


def test_signup_with_company_name_creates_org_and_makes_signer_admin(client, fake_otp_provider):
    admin = _create_org_admin(client, fake_otp_provider, company_name="Test Org Inc")

    me = _me(client)
    assert me["role"] == "admin"
    assert me["org_id"] is not None
    assert me["is_active"] is True


def test_signup_without_company_name_stays_org_less(client, fake_otp_provider):
    """Regression guard: 02-user-registration's documented org_id=None,
    role=None behavior must be unchanged for a signup that doesn't opt into
    creating an organization."""
    _signup_and_login(client, fake_otp_provider)

    me = _me(client)
    assert me["org_id"] is None
    assert me["role"] is None


def test_signup_with_blank_company_name_stays_org_less(client, fake_otp_provider):
    _signup_and_login(client, fake_otp_provider, company_name="   ")

    me = _me(client)
    assert me["org_id"] is None
    assert me["role"] is None


def test_signup_with_company_name_auto_populates_seller_profile(client, fake_otp_provider):
    """The company name typed at signup should already show up on /profile
    without the admin having to re-enter it."""
    _create_org_admin(client, fake_otp_provider, company_name="Acme Manufacturing Ltd")

    profile = client.get("/profile")
    assert profile.status_code == 200, profile.text
    assert profile.json()["company_name"] == "Acme Manufacturing Ltd"


# ==========================================================================
# 1b. Accepting an invite syncs the admin's company profile onto the member
# ==========================================================================


def test_accept_invite_copies_admin_company_profile_onto_member(
    client, fake_otp_provider, fake_invite_email_provider
):
    _create_org_admin(client, fake_otp_provider, company_name="Acme Manufacturing")
    # Admin fills in a fuller profile beyond just the signup-time company name.
    update = client.put(
        "/profile",
        json={
            "industry": "Industrial Machinery",
            "product_lines": "Conveyors, Hydraulics",
            "target_customer_description": "Mid-size factories",
            "target_regions": "North India",
            "gst_no": "ADMINGST123",
            "billing_address": "Admin HQ, Pune",
        },
    )
    assert update.status_code == 200, update.text

    teammate_email = unique_email()
    _create_invite(client, teammate_email)
    token = fake_invite_email_provider.latest_token_for(teammate_email)

    with TestClient(fastapi_app) as teammate_client:
        _signup_and_login(teammate_client, fake_otp_provider, email=teammate_email)
        teammate_client.post(f"/orgs/invites/{token}/accept")

        member_profile = teammate_client.get("/profile")
        assert member_profile.status_code == 200, member_profile.text
        body = member_profile.json()

        assert body["company_name"] == "Acme Manufacturing"
        assert body["industry"] == "Industrial Machinery"
        assert body["product_lines"] == "Conveyors, Hydraulics"
        assert body["target_customer_description"] == "Mid-size factories"
        assert body["target_regions"] == "North India"

        # Billing fields are per-user and must never be inherited from the org.
        assert body["gst_no"] is None
        assert body["billing_address"] is None


def test_accept_invite_when_admin_has_no_profile_leaves_member_profile_empty(
    client, fake_otp_provider, fake_invite_email_provider
):
    """No profile to copy from -> accepting must not crash, and the member
    ends up with the same empty-profile sentinel as any freshly signed-up
    user, not a partially-populated row."""
    _create_org_admin(client, fake_otp_provider, company_name="Acme Manufacturing")
    # Note: the admin's own SellerProfile.company_name was already seeded by
    # signup's company_name (see test above) -- so "no profile" here really
    # means "no *additional* fields were ever set", which is the normal case.

    teammate_email = unique_email()
    _create_invite(client, teammate_email)
    token = fake_invite_email_provider.latest_token_for(teammate_email)

    with TestClient(fastapi_app) as teammate_client:
        _signup_and_login(teammate_client, fake_otp_provider, email=teammate_email)
        accept = teammate_client.post(f"/orgs/invites/{token}/accept")
        assert accept.status_code == 200, accept.text

        member_profile = teammate_client.get("/profile")
        assert member_profile.status_code == 200, member_profile.text
        assert member_profile.json()["company_name"] == "Acme Manufacturing"
        assert member_profile.json()["industry"] is None


# ==========================================================================
# 1c. admin_name / admin_email surfaced on UserOut
# ==========================================================================


def test_member_sees_admin_contact_details_admin_sees_none(
    client, fake_otp_provider, fake_invite_email_provider
):
    _create_org_admin(client, fake_otp_provider, company_name="Acme Manufacturing")
    admin_me = _me(client)

    teammate_email = unique_email()
    _create_invite(client, teammate_email)
    token = fake_invite_email_provider.latest_token_for(teammate_email)

    with TestClient(fastapi_app) as teammate_client:
        _signup_and_login(teammate_client, fake_otp_provider, email=teammate_email)
        teammate_client.post(f"/orgs/invites/{token}/accept")

        member_me = _me(teammate_client)
        assert member_me["admin_name"] == admin_me["name"]
        assert member_me["admin_email"] == admin_me["email"]

    admin_me_after = _me(client)
    assert admin_me_after["admin_name"] is None
    assert admin_me_after["admin_email"] is None


# ==========================================================================
# 2. Invite create / list / preview / accept happy path
# ==========================================================================


def test_invite_create_list_preview_and_accept_happy_path(
    client, fake_otp_provider, fake_invite_email_provider
):
    admin = _create_org_admin(client, fake_otp_provider, company_name="Acme Inc")
    teammate_email = unique_email()

    invite = _create_invite(client, teammate_email)
    assert invite["status"] == "pending"
    assert invite["role"] == "member"

    listed = client.get("/orgs/invites")
    assert listed.status_code == 200, listed.text
    assert [i["invite_id"] for i in listed.json()] == [invite["invite_id"]]

    token = fake_invite_email_provider.latest_token_for(teammate_email)

    preview = client.get(f"/orgs/invites/{token}")
    assert preview.status_code == 200, preview.text
    assert preview.json() == {
        "org_name": "Acme Inc",
        "invitee_email": teammate_email,
        "status": "pending",
    }

    with TestClient(fastapi_app) as teammate_client:
        teammate = _signup_and_login(teammate_client, fake_otp_provider, email=teammate_email)

        accept = teammate_client.post(f"/orgs/invites/{token}/accept")
        assert accept.status_code == 200, accept.text

        accepted_me = _me(teammate_client)
        assert accepted_me["role"] == "member"
        assert accepted_me["org_id"] == _me(client)["org_id"]

    invites_after = client.get("/orgs/invites").json()
    assert invites_after[0]["status"] == "accepted"


def test_accept_invite_with_mismatched_email_returns_403_and_invite_stays_pending(
    client, fake_otp_provider, fake_invite_email_provider
):
    _create_org_admin(client, fake_otp_provider, company_name="Acme Inc")
    invited_email = unique_email()
    _create_invite(client, invited_email)
    token = fake_invite_email_provider.latest_token_for(invited_email)

    with TestClient(fastapi_app) as other_client:
        # Different account entirely, not the invited email.
        _signup_and_login(other_client, fake_otp_provider)

        resp = other_client.post(f"/orgs/invites/{token}/accept")
        assert resp.status_code == 403, resp.text

    invites = client.get("/orgs/invites").json()
    assert invites[0]["status"] == "pending"


def test_accept_invite_unknown_token_returns_404(client, fake_otp_provider):
    _signup_and_login(client, fake_otp_provider)

    resp = client.post("/orgs/invites/not-a-real-token/accept")

    assert resp.status_code == 404, resp.text


def test_accept_invite_when_already_in_an_organization_returns_409(
    client, fake_otp_provider, fake_invite_email_provider
):
    _create_org_admin(client, fake_otp_provider, company_name="Org A")
    invited_email = unique_email()
    _create_invite(client, invited_email)
    token = fake_invite_email_provider.latest_token_for(invited_email)

    with TestClient(fastapi_app) as already_member_client:
        # This account already has its own org (as its own admin).
        _create_org_admin(already_member_client, fake_otp_provider, company_name="Org B", email=invited_email)

        resp = already_member_client.post(f"/orgs/invites/{token}/accept")
        assert resp.status_code == 409, resp.text


def test_duplicate_pending_invite_to_same_email_returns_409(client, fake_otp_provider):
    _create_org_admin(client, fake_otp_provider, company_name="Acme Inc")
    email = unique_email()
    _create_invite(client, email)

    resp = client.post("/orgs/invites", json={"email": email})

    assert resp.status_code == 409, resp.text


def test_revoke_invite_then_recreate_for_same_email_succeeds(client, fake_otp_provider):
    _create_org_admin(client, fake_otp_provider, company_name="Acme Inc")
    email = unique_email()
    invite = _create_invite(client, email)

    revoke = client.delete(f"/orgs/invites/{invite['invite_id']}")
    assert revoke.status_code == 204, revoke.text

    recreated = client.post("/orgs/invites", json={"email": email})
    assert recreated.status_code == 201, recreated.text


def test_preview_unknown_invite_token_returns_404(client):
    resp = client.get("/orgs/invites/not-a-real-token")

    assert resp.status_code == 404, resp.text


# ==========================================================================
# 2b. GET /orgs/my-invites — invitee discovers + accepts from their own account
# ==========================================================================


def test_my_invites_lists_pending_invite_and_accept_by_token_works(
    client, fake_otp_provider
):
    admin = _create_org_admin(client, fake_otp_provider, company_name="Acme Inc")
    invitee_email = unique_email()
    _create_invite(client, invitee_email)

    with TestClient(fastapi_app) as invitee_client:
        _signup_and_login(invitee_client, fake_otp_provider, email=invitee_email)

        mine = invitee_client.get("/orgs/my-invites")
        assert mine.status_code == 200, mine.text
        body = mine.json()
        assert len(body) == 1
        assert body[0]["org_name"] == "Acme Inc"
        assert "token" in body[0] and body[0]["token"]

        accept = invitee_client.post(f"/orgs/invites/{body[0]['token']}/accept")
        assert accept.status_code == 200, accept.text

        after_accept = invitee_client.get("/orgs/my-invites")
        assert after_accept.json() == [], "an accepted invite must no longer show up as pending"


def test_my_invites_only_shows_invites_for_the_callers_own_email(
    client, fake_otp_provider
):
    _create_org_admin(client, fake_otp_provider, company_name="Acme Inc")
    _create_invite(client, unique_email())  # addressed to someone else entirely

    with TestClient(fastapi_app) as other_client:
        _signup_and_login(other_client, fake_otp_provider)  # different, unrelated email

        mine = other_client.get("/orgs/my-invites")
        assert mine.status_code == 200, mine.text
        assert mine.json() == []


def test_my_invites_requires_authentication(client):
    resp = client.get("/orgs/my-invites")

    assert resp.status_code == 401, resp.text


# ==========================================================================
# 3. Admin gating on /orgs/* endpoints
# ==========================================================================


def test_non_admin_cannot_create_list_or_revoke_invites_or_list_members(
    client, fake_otp_provider
):
    """Covers an org-less user (no admin gate reachable at all) — the
    stronger of the two non-admin cases, since a member-of-someone-else's-org
    variant is exercised by the tenant-isolation tests below."""
    _signup_and_login(client, fake_otp_provider)  # org_id=None, role=None

    assert client.post("/orgs/invites", json={"email": unique_email()}).status_code == 403
    assert client.get("/orgs/invites").status_code == 403
    assert client.delete(f"/orgs/invites/{uuid.uuid4()}").status_code == 403
    assert client.get("/orgs/members").status_code == 403
    assert client.patch(f"/orgs/members/{uuid.uuid4()}/deactivate").status_code == 403
    assert client.patch(f"/orgs/members/{uuid.uuid4()}/reactivate").status_code == 403
    assert client.post(f"/orgs/members/{uuid.uuid4()}/make-admin").status_code == 403


# ==========================================================================
# 4. GET /orgs/members — shape (no wallet fields) + tenant isolation
# ==========================================================================


def test_list_members_excludes_wallet_and_allowance_fields(client, fake_otp_provider):
    _create_org_admin(client, fake_otp_provider, company_name="Acme Inc")

    resp = client.get("/orgs/members")

    assert resp.status_code == 200, resp.text
    member = resp.json()[0]
    forbidden_keys = {"balance_inr", "wallet", "wallet_id", "free_action_allowance", "allowance"}
    assert not (forbidden_keys & member.keys()), (
        f"OrgMemberOut must never expose wallet/allowance fields, got keys={list(member.keys())}"
    )


def test_admin_of_org_a_cannot_see_or_act_on_org_bs_members_or_invites(
    client, fake_otp_provider, fake_invite_email_provider
):
    admin_a = _create_org_admin(client, fake_otp_provider, company_name="Org A")

    with TestClient(fastapi_app) as client_b:
        admin_b = _create_org_admin(client_b, fake_otp_provider, company_name="Org B")
        invite_b = _create_invite(client_b, unique_email())

        # Org A's admin must not see org B's members.
        members_seen_by_a = client.get("/orgs/members").json()
        assert all(m["user_id"] != admin_b["user_id"] for m in members_seen_by_a)

        # Org A's admin cannot deactivate/reactivate/promote org B's admin.
        assert client.patch(f"/orgs/members/{admin_b['user_id']}/deactivate").status_code == 404
        assert client.patch(f"/orgs/members/{admin_b['user_id']}/reactivate").status_code == 404
        assert client.post(f"/orgs/members/{admin_b['user_id']}/make-admin").status_code == 404

        # Org A's admin cannot revoke org B's invite.
        assert client.delete(f"/orgs/invites/{invite_b['invite_id']}").status_code == 404


# ==========================================================================
# 5. Deactivate / reactivate — session cutoff + self/admin guards
# ==========================================================================


def test_deactivate_member_cuts_off_existing_session_immediately(
    client, fake_otp_provider, fake_invite_email_provider
):
    _create_org_admin(client, fake_otp_provider, company_name="Acme Inc")
    teammate_email = unique_email()
    _create_invite(client, teammate_email)
    token = fake_invite_email_provider.latest_token_for(teammate_email)

    with TestClient(fastapi_app) as teammate_client:
        teammate = _signup_and_login(teammate_client, fake_otp_provider, email=teammate_email)
        teammate_client.post(f"/orgs/invites/{token}/accept")

        deactivate = client.patch(f"/orgs/members/{teammate['user_id']}/deactivate")
        assert deactivate.status_code == 200, deactivate.text
        assert deactivate.json()["is_active"] is False

        # The teammate's *existing* session cookie, issued before
        # deactivation, must stop working on its very next request.
        still_logged_in = teammate_client.get("/auth/me")
        assert still_logged_in.status_code == 401, (
            "a deactivated user's existing session must 401 immediately, not just block future logins"
        )

        # And a fresh login attempt must also be rejected.
        fresh_login = teammate_client.post(
            "/auth/login", json={"email": teammate_email, "password": teammate["password"]}
        )
        assert fresh_login.status_code == 403, fresh_login.text

    reactivate = client.patch(f"/orgs/members/{teammate['user_id']}/reactivate")
    assert reactivate.status_code == 200, reactivate.text
    assert reactivate.json()["is_active"] is True

    with TestClient(fastapi_app) as teammate_client_2:
        relogin = teammate_client_2.post(
            "/auth/login", json={"email": teammate_email, "password": teammate["password"]}
        )
        assert relogin.status_code == 200, relogin.text


def test_admin_cannot_deactivate_self(client, fake_otp_provider):
    """There is only ever one admin per org (uq_users_org_admin), and
    get_current_admin guarantees the caller is that admin — so "deactivate
    the org's admin" and "deactivate self" are the same case by
    construction. This self-check is the complete admin-immunity guard."""
    admin = _create_org_admin(client, fake_otp_provider, company_name="Acme Inc")

    resp = client.patch(f"/orgs/members/{admin['user_id']}/deactivate")

    assert resp.status_code == 400, resp.text


# ==========================================================================
# 6. make-admin — ownership transfer leaves exactly one admin
# ==========================================================================


def test_make_admin_transfers_ownership_and_leaves_exactly_one_admin(
    client, fake_otp_provider, fake_invite_email_provider
):
    old_admin = _create_org_admin(client, fake_otp_provider, company_name="Acme Inc")
    teammate_email = unique_email()
    _create_invite(client, teammate_email)
    token = fake_invite_email_provider.latest_token_for(teammate_email)

    with TestClient(fastapi_app) as teammate_client:
        teammate = _signup_and_login(teammate_client, fake_otp_provider, email=teammate_email)
        teammate_client.post(f"/orgs/invites/{token}/accept")

        transfer = client.post(f"/orgs/members/{teammate['user_id']}/make-admin")
        assert transfer.status_code == 204, transfer.text

        old_admin_now = _me(client)
        new_admin_now = _me(teammate_client)

        assert old_admin_now["role"] == "member"
        assert new_admin_now["role"] == "admin"
        assert old_admin_now["org_id"] == new_admin_now["org_id"]

        members = teammate_client.get("/orgs/members").json()
        admins = [m for m in members if m["role"] == "admin"]
        assert len(admins) == 1, f"expected exactly one admin after transfer, got {admins}"


def test_make_admin_targeting_self_returns_400(client, fake_otp_provider):
    admin = _create_org_admin(client, fake_otp_provider, company_name="Acme Inc")

    resp = client.post(f"/orgs/members/{admin['user_id']}/make-admin")

    assert resp.status_code == 400, resp.text


def test_make_admin_targeting_deactivated_member_returns_404(
    client, fake_otp_provider, fake_invite_email_provider
):
    """Promoting a deactivated user would leave the org with zero usable
    admins (the new admin can't log in; the old admin already demoted
    themselves) — must 404, matching every other not-found/not-eligible
    case in this file, rather than silently creating that dead end."""
    old_admin = _create_org_admin(client, fake_otp_provider, company_name="Acme Inc")
    teammate_email = unique_email()
    _create_invite(client, teammate_email)
    token = fake_invite_email_provider.latest_token_for(teammate_email)

    with TestClient(fastapi_app) as teammate_client:
        teammate = _signup_and_login(teammate_client, fake_otp_provider, email=teammate_email)
        teammate_client.post(f"/orgs/invites/{token}/accept")

        deactivate = client.patch(f"/orgs/members/{teammate['user_id']}/deactivate")
        assert deactivate.status_code == 200, deactivate.text

        promote = client.post(f"/orgs/members/{teammate['user_id']}/make-admin")
        assert promote.status_code == 404, promote.text

        # The old admin must still be the org's one and only admin.
        old_admin_now = _me(client)
        assert old_admin_now["role"] == "admin"
        assert old_admin_now["user_id"] == old_admin["user_id"]
