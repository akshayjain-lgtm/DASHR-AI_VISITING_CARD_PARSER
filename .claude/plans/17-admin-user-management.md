# Implementation Plan: Admin User Management (spec 17-admin-user-management.md)

## Context
Every signed-up `User` today is permanently org-less: `auth_service.signup()` (`apps/api/app/services/auth_service.py:27-51`) hardcodes `org_id=None, role=None`, there is no `users`/`orgs` router, and no admin-gating dependency exists anywhere (`apps/api/app/deps.py` only has `get_current_user`/`get_db`/`get_otp_provider`). `apps/api/app/services/visibility.py::scope_to_visible_users` already implements the "admin sees every org member's rows" query scoping described in `01-database-setup`, but its admin branch is currently unreachable — nothing ever sets `role="admin"`. Spec `.claude/specs/17-admin-user-management.md` (on branch `feature/admin-user-management`) closes this: signup gains an optional `company_name` that creates an `Organization` and makes the signer its admin; a new `org_invites` table + `/orgs` router let that admin invite teammates by email, list members, deactivate/reactivate a member's access, and transfer admin ownership. A new `users.is_active` column backs deactivation. Wallet/billing tables are never touched — this plan only ever writes `users.org_id`/`role`/`is_active` and `org_invites`.

This plan follows the `.claude/plans/16-dashboard-analytics.md` precedent for structure. Testing scope is dev-level verification only (`pytest`/`vitest` + the `dashr-test-runner` subagent per CLAUDE.md's subagent policy) — the user triggers `/test-feature` separately.

## Step 1 — Migrations
- `apps/api/migrations/versions/0014_user_active_status.py` — `ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT true`; downgrade drops it.
- `apps/api/migrations/versions/0015_org_invites.py` — creates `org_invites`: `invite_id` UUID PK (`server_default=gen_random_uuid()`), `org_id` UUID `NOT NULL` FK→`organizations.org_id` `ON DELETE CASCADE`, `email` `NOT NULL`, `role` `NOT NULL CHECK (role = 'member')`, `token` `NOT NULL UNIQUE`, `status` `NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','revoked','expired'))`, `invited_by_user_id` UUID `NOT NULL` FK→`users.user_id`, `accepted_by_user_id` UUID nullable FK→`users.user_id`, `created_at`/`expires_at`/`accepted_at` TIMESTAMPTZ, plus a partial unique index `uq_org_invites_org_email_pending` on `(org_id, email)` WHERE `status = 'pending'` (mirrors `uq_users_org_admin`'s pattern in `user.py:16-21`).

## Step 2 — Models
- `apps/api/app/models/user.py` — add `is_active: Mapped[bool] = mapped_column(server_default=text("true"))`.
- `apps/api/app/models/org_invite.py` — new `OrgInvite` model matching Step 1's schema exactly, same style as `apps/api/app/models/organization.py`/`user.py` (UUID PK via `gen_random_uuid()`, `Mapped`/`mapped_column`, `__table_args__` for the check constraint + partial unique index).
- `apps/api/app/models/__init__.py` — register `OrgInvite` alongside existing model imports (confirm the exact registration pattern used there before editing).

## Step 3 — Config + exceptions
- `apps/api/app/core/config.py` — add `frontend_url: str = Field(default="http://localhost:3000", alias="FRONTEND_URL")`, used to build the invite accept link (`{frontend_url}/login?invite={token}`).
- `apps/api/app/services/exceptions.py` — add `UserDeactivatedError`, `InviteNotFoundError`, `InviteEmailMismatchError`, `AlreadyInOrganizationError`, `DuplicatePendingInviteError`, `CannotModifyAdminError`, `CannotTargetSelfError` — one-line docstrings each, matching the file's existing style.

## Step 4 — `deps.py`: session + admin gating
- `get_current_user` — after `db.get(User, user_id)`, add `if not user.is_active: raise HTTPException(401, "Not authenticated")` so a deactivated user's *existing* session cookie stops working immediately, not just their next login.
- New `get_current_admin(user: User = Depends(get_current_user)) -> User` — `403` if `user.role != "admin"` or `user.org_id is None`.
- New `get_invite_email_provider()` — mirrors `get_otp_provider`'s prod-refusal guard exactly (raises in production until a real provider is wired), returns `ConsoleInviteEmailProvider` otherwise.

## Step 5 — `invite_email_provider.py`
`apps/api/app/services/invite_email_provider.py` — `InviteEmailProvider` Protocol (`send(self, to_email: str, org_name: str, accept_url: str) -> None`) + `ConsoleInviteEmailProvider`, structurally identical to `apps/api/app/services/otp_provider.py`'s `OtpProvider`/`ConsoleOtpProvider` (module logger, no PII beyond what's needed logged at INFO).

## Step 6 — `org_service.py`
New `apps/api/app/services/org_service.py`, all functions take `db: Session` first:
- `INVITE_EXPIRY = timedelta(days=7)` as a named module constant (mirrors `scoring.py`'s constants-not-inline-branches style referenced in the 16-dashboard-analytics plan).
- `create_org_with_admin(db, user, company_name) -> None` — no-ops if `company_name` is blank/`None`; otherwise creates `Organization(name=company_name)`, sets `user.org_id`/`user.role="admin"` on the same `User` instance being flushed by the caller. Called from `auth_service.signup()` before its `db.commit()` — does not commit itself, so it stays in the same transaction as the `User` insert.
- `create_invite(db, admin, email, provider) -> OrgInvite` — raises `DuplicatePendingInviteError` on the partial-unique-index conflict (catch `IntegrityError`, mirroring `auth_service.signup`'s pattern at lines 42-48); generates `token = secrets.token_urlsafe(32)`; sends via `provider.send(...)` using `frontend_url` from settings.
- `list_invites(db, admin) -> list[OrgInvite]` — `WHERE org_id = admin.org_id ORDER BY created_at DESC`.
- `revoke_invite(db, admin, invite_id) -> None` — `InviteNotFoundError` unless `org_id = admin.org_id` and `status = "pending"`.
- `get_invite_preview(db, token) -> OrgInvite` — joins `Organization` for the name; `InviteNotFoundError` for missing/expired token (expiry checked live against `expires_at`, not a separate sweep job).
- `accept_invite(db, current_user, token) -> User` — re-fetches the invite row, validates `status == "pending"` and not expired (`InviteNotFoundError` otherwise), validates `invite.email.lower() == current_user.email.lower()` (`InviteEmailMismatchError`), validates `current_user.org_id is None` (`AlreadyInOrganizationError`); sets `current_user.org_id/role="member"`, `invite.status="accepted"`, `invite.accepted_by_user_id`/`accepted_at`; commits.
- `list_members(db, admin) -> list[User]` — `WHERE org_id = admin.org_id`.
- `_get_target_member(db, admin, user_id) -> User` — shared lookup used by the three mutators below; raises `UserNotFoundError` unless the target's `org_id == admin.org_id` (re-fetched fresh, not from a stale reference, per the spec's concurrency rule).
- `deactivate_member(db, admin, user_id) -> User` — `CannotTargetSelfError` if `user_id == admin.user_id`; `CannotModifyAdminError` if target `role == "admin"`; else `is_active=False`, commit.
- `reactivate_member(db, admin, user_id) -> User` — `is_active=True`, commit.
- `make_admin(db, admin, user_id) -> None` — `CannotTargetSelfError` if `user_id == admin.user_id`; within one transaction: **first** `admin.role = "member"`, `db.flush()`, **then** target `.role = "admin"`, `db.commit()` — this order is load-bearing: the partial unique index `uq_users_org_admin` is checked per-statement (not deferred), so demoting first is what keeps the promoting statement from colliding with it (documented inline as a comment, matching this codebase's habit of explaining non-obvious ordering — see `auth_service.py:20-24`).

## Step 7 — `auth_service.py` + `schemas/auth.py` changes
- `schemas/auth.py` — `SignupRequest` gains `company_name: str | None = Field(default=None, max_length=200)`; `UserOut` gains `is_active: bool`.
- `auth_service.signup()` — after constructing `user` and before `db.add(user)`/commit, call `org_service.create_org_with_admin(db, user, data.company_name)`.
- `auth_service.login()` — after the existing password/`phone_verified` checks, add `if not user.is_active: raise UserDeactivatedError()`.
- `apps/api/app/routers/auth.py::login` — catch `UserDeactivatedError` → `403 "Account has been deactivated"`.

## Step 8 — `schemas/orgs.py`
`InviteCreate {email: EmailStr}`, `InviteOut {invite_id, email, role, status, created_at, expires_at, accepted_at}`, `InvitePreviewOut {org_name, invitee_email, status}`, `OrgMemberOut {user_id, name, email, role, phone_no, phone_verified, is_active, created_at}` — `from_attributes=True` on the ORM-backed ones, matching `schemas/profile.py`'s pattern. `OrgMemberOut` deliberately has no wallet/balance field.

## Step 9 — `routers/orgs.py`
`router = APIRouter(prefix="/orgs", tags=["orgs"])`, thin handlers only (translate `org_service` exceptions → HTTP status, no business logic in the router — matching every other router in this codebase):
- `GET /orgs/invites/{token}` — no auth dependency — 404 on `InviteNotFoundError`.
- `POST /orgs/invites` (`Depends(get_current_admin)`, `Depends(get_invite_email_provider)`) — 409 on `DuplicatePendingInviteError`.
- `GET /orgs/invites` (`Depends(get_current_admin)`).
- `DELETE /orgs/invites/{invite_id}` (`Depends(get_current_admin)`) — 404 on `InviteNotFoundError`.
- `POST /orgs/invites/{token}/accept` (`Depends(get_current_user)`) — 404/`InviteNotFoundError`, 403/`InviteEmailMismatchError`, 409/`AlreadyInOrganizationError`.
- `GET /orgs/members` (`Depends(get_current_admin)`).
- `PATCH /orgs/members/{user_id}/deactivate` (`Depends(get_current_admin)`) — 404/`UserNotFoundError`, 400/`CannotTargetSelfError` or `CannotModifyAdminError`.
- `PATCH /orgs/members/{user_id}/reactivate` (`Depends(get_current_admin)`).
- `POST /orgs/members/{user_id}/make-admin` (`Depends(get_current_admin)`) — 404/`UserNotFoundError`, 400/`CannotTargetSelfError`.

`apps/api/app/main.py` — import + `app.include_router(orgs_router)`.

## Step 10 — Backend tests: `apps/api/tests/test_17-admin-user-management.py`
Sync `TestClient` + real Postgres `dashr_test` DB (matching every other test file's setup — confirm exact fixture usage from `conftest.py` before writing). Coverage:
- Signup with `company_name` → org created, signer is admin; signup without it → unchanged `org_id=None,role=None` (regression guard against `test_02_user_registration.py`).
- Invite create → preview (unauthenticated) → accept (matching email) → member's `org_id`/`role` set, invite `status="accepted"`.
- Accept with mismatched email → 403, invite still `pending`.
- Duplicate pending invite → 409.
- `GET /orgs/members` excludes wallet fields (assert response keys) and 403s for a non-admin caller.
- Deactivate → deactivated user's existing session cookie gets 401 on the very next request (not just login) → reactivate restores access.
- Cannot deactivate self or the org's admin.
- `make-admin` swap leaves exactly one admin (assert both users' roles post-transaction; assert the DB constraint never raised).
- Two-tenant isolation: an admin of org A can't see/act on org B's members/invites (same tenant-isolation pattern as `test_analytics.py`).

## Step 11 — Frontend: types + API client
`apps/web/lib/api.ts` — extend `signup()`'s payload type with `company_name?: string`; add `UserOut.is_active`; add types + functions `InvitePreview/getInvitePreview(token)`, `InviteOut/createInvite(email)/listInvites()/revokeInvite(inviteId)/acceptInvite(token)`, `OrgMember/listOrgMembers()/deactivateMember(userId)/reactivateMember(userId)/makeAdmin(userId)` — following the file's existing function-per-endpoint pattern exactly (see `getWallet`/`listWalletTransactions` for the plainest analog).

## Step 12 — Frontend: `settings/page.tsx`
`apps/web/app/settings/page.tsx` — client component, `Sidebar active="home"` initially (relabel to `"settings"`, see Step 13). On mount, `me()` (existing `/auth/me` client fn) to get the current user's `role`/`org_id`:
- **Admin**: invite form (email input + submit → `createInvite`), pending-invites table (`listInvites`, revoke button → `revokeInvite`), members table (`listOrgMembers`, row actions → `deactivateMember`/`reactivateMember`/`makeAdmin` gated by `is_active`/`role`), each mutating action followed by a refetch of the relevant list (no optimistic state, matching this codebase's existing pages' simplicity).
- **Member** (`role !== "admin"`, `org_id` set): read-only panel, org name (needs `GET /orgs/members` to be admin-gated, so fetch org name from `me()`'s response instead — note: `UserOut` doesn't carry org name today; either add `org_name` to `UserOut` via a join in `auth.py::me`, or accept "Member of your organization" without the name — **decide during implementation, default to adding `org_name: str | None` to `UserOut`/`me()` since it's a one-line join and meaningfully better UX**).
- **Org-less** (`org_id is None`): "You're not part of an organization yet" placeholder, no controls.
- A confirm step for Deactivate (reuse `apps/web/components/confirm-dialog.tsx`, same as the existing card-delete flow) — this is an access-revocation action, not idempotent-feeling enough for a bare click.

## Step 13 — Frontend: sidebar + login page
- `apps/web/components/sidebar.tsx:23` — `{ id: "settings", label: "Settings", icon: Settings, path: "/settings" }` (id change too, so the active-highlight logic still works once callers pass `active="settings"`).
- `apps/web/app/login/page.tsx` — wire `company` state into `signup({..., company_name: company || undefined})`; read `useSearchParams().get("invite")`; if present, `getInvitePreview(token)` on mount to show a banner (`"You're invited to join {org_name}"`) and hide the Company Name field in signup mode; after a successful `login()` or post-OTP-verify `verifyOtp()`, if an invite token is present, call `acceptInvite(token)` before `router.push("/dashboard")` (swallow/report a stale-invite error without blocking the redirect — the user is still logged in either way).

## Step 14 — Frontend tests: `apps/web/__tests__/17-admin-user-management.test.tsx`
Mock `useRouter`/`useSearchParams`, stub `global.fetch` branches for `/orgs/members`, `/orgs/invites`, `/orgs/invites/:token`, `/orgs/invites/:token/accept`. Cover: admin view renders invite form + tables and invite/deactivate/make-admin actions call the right endpoints; member view renders read-only panel with no controls; login page shows the invite banner and calls `acceptInvite` post-login when `?invite=` is present.

## Sequencing
Migrations (1) → Models (2) → Config/exceptions (3) → deps.py (4) → invite email provider (5) → org_service (6) → auth_service/schemas (7) → orgs schemas (8) → orgs router + main.py (9) → run migrations, manual `curl` sanity pass on `/orgs/*` → Backend tests (10) → Frontend client (11) → Settings page (12) → Sidebar/login (13) → Frontend tests (14) → manual browser walkthrough (signup-with-company → invite → accept-in-new-browser-session → deactivate → make-admin) → Definition of Done pass against the spec.

## Verification
- `cd apps/api && alembic upgrade head` (via the dev stack's Postgres) succeeds cleanly on both new migrations, and `alembic downgrade -2` / `upgrade head` round-trips without error.
- `cd apps/api && pytest tests/test_17-admin-user-management.py -v`
- `cd apps/web && npx vitest run 17-admin-user-management.test.tsx`
- Manual, via `/launch-website`: create account A with a Company Name (becomes admin) → invite `b@example.com` from Settings → console log shows the invite link → open it in an incognito window, sign up as B with that email, land on `/dashboard` → back in A's Settings, see B listed, deactivate B → confirm B's next request 401s → reactivate → make-admin(B) → confirm A now shows `role="member"` via `/auth/me`.
- `dashr-test-runner` subagent invoked after both automated test runs (dev-level verification only, per CLAUDE.md's subagent policy — `/test-feature` is triggered separately by the user).
- `dashr-security-reviewer`/`dashr-quality-reviewer` via `/code-review-feature 17-admin-user-management` before considering the branch done, since this feature touches auth/session/RBAC surface directly.

## Critical files
- `apps/api/migrations/versions/0014_user_active_status.py`, `0015_org_invites.py`
- `apps/api/app/models/user.py`, `apps/api/app/models/org_invite.py`
- `apps/api/app/deps.py`, `apps/api/app/core/config.py`, `apps/api/app/services/exceptions.py`
- `apps/api/app/services/invite_email_provider.py`, `apps/api/app/services/org_service.py`
- `apps/api/app/services/auth_service.py`, `apps/api/app/schemas/auth.py`, `apps/api/app/schemas/orgs.py`
- `apps/api/app/routers/orgs.py`, `apps/api/app/routers/auth.py`, `apps/api/app/main.py`
- `apps/web/lib/api.ts`, `apps/web/app/settings/page.tsx`, `apps/web/app/login/page.tsx`, `apps/web/components/sidebar.tsx`
- `apps/api/tests/test_17-admin-user-management.py`, `apps/web/__tests__/17-admin-user-management.test.tsx`
- `.claude/specs/17-admin-user-management.md`, `.claude/plans/17-admin-user-management.md`
