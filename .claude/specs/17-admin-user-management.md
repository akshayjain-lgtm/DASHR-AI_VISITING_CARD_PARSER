# Spec: Admin User Management

## Overview
Today every signed-up user is org-less (`org_id = NULL, role = NULL`) — there is no way for an Organization to ever come into existence, no way for a second person to join one, and no admin-facing surface to see or manage teammates. This feature closes that gap end-to-end: signing up with a company name creates an Organization and makes the signer its admin; that admin can invite teammates by email, see who's in their org, deactivate/reactivate a member's access, and hand off admin ownership to another member. This sits alongside capture/extraction/enrichment/scoring/review — it's the org/team-membership backbone those workflows assume (`org_id`-scoped visibility, admin-vs-member data access) but that has never actually been buildable until now. It intentionally does **not** touch billing: wallets, free-action allowances, and invoices stay strictly per-user, exactly as they are today — this feature only ever writes to `users.org_id`/`users.role`/`users.is_active` and the new invite table.

## Depends on
- **01-database-setup** — `Organization`/`User` models, the `role IN ('admin','member')` + `role <> 'admin' OR org_id IS NOT NULL` check constraints, and the one-admin-per-org partial unique index (`uq_users_org_admin`) this spec relies on rather than re-implements.
- **02-user-registration** — the `/auth/signup` → OTP-verify flow this spec extends with organization creation.
- **03-user-login-logout** — session cookie (`dashr_session`) and `get_current_user` dependency this spec builds admin-gating on top of.

Note: `apps/api/app/services/visibility.py::scope_to_visible_users` already implements the "admin sees every org member's rows, a member sees only their own" query scoping described in 01-database-setup — this spec does not change that helper, it only makes `org_id`/`role` actually reach a non-null state so that helper's admin branch becomes reachable in practice.

## API endpoints (apps/api)

- `POST /auth/signup` — **modified**, public — request gains an optional `company_name: str | None` field (already collected by the frontend today but never sent, see Frontend surface). If `company_name` is a non-blank string, the new user is created as that Organization's admin (`org_id` = new org, `role = "admin"`) in the same transaction as the `User` insert; response shape (`SignupResponse`) is unchanged.
- `GET /orgs/invites/{token}` — public, no auth — returns `{ org_name: str, invitee_email: str, status: str }` for rendering a "you're invited to join {org_name}" banner before the invitee has an account or session. Returns 404 for an unknown token; never exposes `org_id`, `invited_by`, or any other field.
- `POST /orgs/invites` — org-authenticated, **admin only** — body `{ email: EmailStr }`; creates a pending invite for the caller's org (role is always `"member"` — admin seats are transferred, never invited directly), generates an opaque token, sends an invite email via `InviteEmailProvider`, and returns the created invite. 409 if a pending, unexpired invite already exists for that email in this org.
- `GET /orgs/invites` — org-authenticated, admin only — lists every invite (`pending`/`accepted`/`revoked`/`expired`) for the caller's org, newest first.
- `DELETE /orgs/invites/{invite_id}` — org-authenticated, admin only — revokes a `pending` invite (sets `status = "revoked"`); 404 if the invite isn't `pending` or doesn't belong to the caller's org.
- `POST /orgs/invites/{token}/accept` — org-authenticated (any logged-in user, admin gate not required) — attaches the calling user to the invite's org as `role = "member"`. 404 for an unknown/expired/non-pending token; 403 if the invite's `email` doesn't case-insensitively match the caller's `email`; 409 if the caller already has a non-null `org_id`. On success, marks the invite `accepted` and returns `UserOut` with the updated `org_id`/`role`.
- `GET /orgs/members` — org-authenticated, admin only — lists every user with `org_id` = caller's org (`user_id`, `name`, `email`, `role`, `phone_no`, `phone_verified`, `is_active`, `created_at`). Deliberately excludes wallet balance and free-action-allowance fields — this endpoint is a membership/visibility surface, never a spend-authority one.
- `PATCH /orgs/members/{user_id}/deactivate` — org-authenticated, admin only — sets `is_active = false` for a target member. 404 if the target isn't in the caller's org; 400 if the target is the caller or is the org's admin (an admin cannot deactivate themselves or, since there is only ever one, any other admin — self-lockout guard).
- `PATCH /orgs/members/{user_id}/reactivate` — org-authenticated, admin only — sets `is_active = true` for a target member. 404 if the target isn't in the caller's org.
- `POST /orgs/members/{user_id}/make-admin` — org-authenticated, admin only — transactionally demotes the caller to `role = "member"` and promotes the target member to `role = "admin"` (statement order matters — see Rules for implementation). 404 if the target isn't an active member of the caller's org; 400 if the target is already the admin (i.e. targets the caller).

## Frontend surface (apps/web)

- **New page**: `apps/web/app/settings/page.tsx` — the "Team" page, replacing the currently dead `Settings → /` sidebar link (`apps/web/components/sidebar.tsx:23`). Admins see: an invite form (email input), a pending-invites table (email/status/expires/revoke), and a members table (name/email/role/status/joined, with Deactivate/Reactivate/Make Admin row actions). Non-admin members and org-less users see a read-only panel — org name + "You are a Member" for the former, "You're not part of an organization yet" for the latter — no management controls, since a member has no authority over org membership.
- **Modified**: `apps/web/components/sidebar.tsx:23` — `Settings` nav item's `path` changes from `/` to `/settings`.
- **Modified**: `apps/web/app/login/page.tsx` — the existing `company` field (line 25, currently collected but never sent — see line 65-70) is wired into the `signup()` call as `company_name`. The page also reads an `?invite=<token>` query param: if present, it calls `GET /orgs/invites/{token}` to show an "You're invited to join {org_name}" banner, hides the Company Name field in signup mode (joining an existing org, not creating one), and after a successful login or OTP-verified signup, calls `POST /orgs/invites/{token}/accept` before redirecting to `/dashboard`.
- **Modified**: `apps/web/lib/api.ts` — add typed client functions: `getInvitePreview`, `createInvite`, `listInvites`, `revokeInvite`, `acceptInvite`, `listOrgMembers`, `deactivateMember`, `reactivateMember`, `makeAdmin`; extend the existing `signup()` function's payload type with `company_name?: string`.

## Database changes

- **`users`** — add `is_active BOOLEAN NOT NULL DEFAULT true` (migration `0014_user_active_status.py`). Checked against the current model (`apps/api/app/models/user.py:11-45`): no soft-delete/status field of any kind exists today, so this is additive, not a rename. `login()` and `get_current_user()` both gate on it (see Rules).
- **New table `org_invites`** (migration `0015_org_invites.py`), org-scoped via `org_id`:
  - `invite_id` UUID PK (`server_default=gen_random_uuid()`)
  - `org_id` UUID, FK → `organizations.org_id`, `NOT NULL`, `ON DELETE CASCADE`
  - `email` — invitee's email, `NOT NULL`
  - `role` — `NOT NULL`, `CHECK (role = 'member')` (admin seats are transferred via `make-admin`, never invited)
  - `token` — unique opaque string, `NOT NULL`
  - `status` — `NOT NULL, DEFAULT 'pending'`, `CHECK (status IN ('pending','accepted','revoked','expired'))`
  - `invited_by_user_id` UUID, FK → `users.user_id`, `NOT NULL`
  - `accepted_by_user_id` UUID, FK → `users.user_id`, nullable
  - `created_at` TIMESTAMPTZ, `server_default=now()`
  - `expires_at` TIMESTAMPTZ, `NOT NULL` (7 days from creation)
  - `accepted_at` TIMESTAMPTZ, nullable
  - Partial unique index on `(org_id, email)` WHERE `status = 'pending'` — mirrors the `uq_users_org_admin` pattern in `user.py`, prevents duplicate pending invites to the same email without blocking a re-invite after the first is revoked/expired/accepted.

## Background jobs
No new background job changes. Invite emails are sent synchronously within the request via a provider Protocol, mirroring how `apps/api/app/services/otp_service.py` sends OTP SMS synchronously today — not a Celery task, consistent with the existing convention for this kind of single, fast, external-notification call (as opposed to bulk card processing, which is the thing Celery is reserved for).

## Files to change
- `apps/api/app/models/user.py` — add `is_active` column
- `apps/api/app/schemas/auth.py` — `SignupRequest.company_name`, `UserOut.is_active`
- `apps/api/app/services/auth_service.py` — `signup()` creates `Organization` + sets admin when `company_name` given; `login()` rejects with a new exception when `is_active` is false
- `apps/api/app/services/exceptions.py` — add `UserDeactivatedError`, `InviteNotFoundError`, `InviteEmailMismatchError`, `AlreadyInOrganizationError`, `DuplicatePendingInviteError`, `CannotModifyAdminError`, `CannotTargetSelfError`
- `apps/api/app/routers/auth.py` — map `UserDeactivatedError` to 403 in `login`
- `apps/api/app/deps.py` — `get_current_user` rejects (401) a session whose user is now `is_active = false`, so deactivation cuts off a live session immediately, not just future logins; add `get_current_admin` dependency
- `apps/api/app/core/config.py` — add `frontend_url` setting (default `http://localhost:3000`), used to build the invite accept link in the email
- `apps/web/components/sidebar.tsx` — fix `Settings` nav path
- `apps/web/app/login/page.tsx` — send `company_name`, handle `?invite=` flow
- `apps/web/lib/api.ts` — new client functions, extend `signup()` payload type

## Files to create
- `apps/api/migrations/versions/0014_user_active_status.py`
- `apps/api/migrations/versions/0015_org_invites.py`
- `apps/api/app/models/org_invite.py`
- `apps/api/app/schemas/orgs.py` — `InviteCreate`, `InviteOut`, `InvitePreviewOut`, `OrgMemberOut`
- `apps/api/app/services/org_service.py` — invite creation/listing/revocation/acceptance, member listing/deactivation/reactivation/admin-transfer; the one place `org_id`/`role`/`is_active` get written outside signup
- `apps/api/app/services/invite_email_provider.py` — `InviteEmailProvider` Protocol + `ConsoleInviteEmailProvider` dev implementation, mirroring `apps/api/app/services/otp_provider.py`'s `OtpProvider`/`ConsoleOtpProvider` pattern; wired via a new `get_invite_email_provider` dependency in `deps.py` with the same prod-refusal guard `get_otp_provider` uses
- `apps/api/app/routers/orgs.py`
- `apps/web/app/settings/page.tsx`

## New dependencies
No new dependencies. Invite tokens use `secrets.token_urlsafe`, already available via Python's standard library (same source as this codebase's existing token/OTP generation).

## Rules for implementation
- Every query in `org_service.py` filters by `org_id` — `GET /orgs/members`, `GET /orgs/invites`, and every mutation must scope to the caller's own `org_id`, never trust a client-supplied org id.
- No raw SQL string interpolation — SQLAlchemy query builder or bound params only.
- Business logic lives in `org_service.py`, not in `apps/api/app/routers/orgs.py` — the router only translates exceptions to HTTP status codes, matching every other router in this codebase.
- `make-admin` must issue the demote-then-promote `UPDATE`s in that order within one transaction: PostgreSQL checks a non-deferrable unique index immediately after each statement, not at commit, so demoting the current admin to `member` first (removing it from `uq_users_org_admin`) before promoting the target is what keeps the second `UPDATE` from colliding with the first — promote-then-demote will violate the partial unique index and roll back.
- `deactivate`/`reactivate`/`make-admin` must re-fetch and re-validate the target user's `org_id` and current `role`/`is_active` inside the same transaction as the write (not just from a stale value read earlier in the request) — two concurrent admin actions on the same target must not race past each other's validation.
- `get_current_user` (`apps/api/app/deps.py`) must reject a request with 401 the moment `user.is_active` is false, regardless of how recently the session cookie was issued — a deactivation must cut off an already-logged-in session, not just block the next login.
- Never let `POST /orgs/invites/{token}/accept` write anything to `users.org_id`/`role` unless the invite's `email` matches the authenticated caller's `email` case-insensitively — this is the only check standing between an invite link and a hijacked org membership.
- Never invite, promote, or otherwise create a second `admin` in one org — `make-admin` is the only path to the admin role after signup, and it always demotes the prior admin in the same transaction the DB's partial unique index would otherwise reject.
- Never let any endpoint in `orgs.py` read or expose `wallets.balance_inr`, `free_action_allowances`, or `WalletTransaction` rows for another user — admin visibility here is membership/visibility only, never spend authority, per CLAUDE.md's wallet-scoping rules.
- Never let `POST /auth/signup`'s new `company_name` path do anything but create a brand-new `Organization` — it must never attach the signer to an existing org (that's what invite-accept is for) and must never run when `company_name` is blank/omitted, preserving today's org-less-by-default signup for anyone who didn't type a company name.
- API contracts are Pydantic models (`app/schemas/orgs.py`) — no hand-duplicated TS types; `packages/shared-types` regenerates from them as with every other endpoint.

## Definition of done
- Signing up with a non-blank Company Name creates an `Organization` row and the new user has `role = "admin"`, `org_id` set, verifiable via `GET /auth/me` after OTP verification.
- Signing up with a blank Company Name still yields `org_id = NULL, role = NULL`, matching today's behavior exactly (no regression for the existing registration spec's test coverage).
- An admin can `POST /orgs/invites` for a teammate's email, see it in `GET /orgs/invites` as `pending`, and the console invite-email log line contains a link with the invite token.
- Visiting `/settings?invite=<token>` as a logged-out visitor shows the org name from `GET /orgs/invites/{token}` without requiring auth.
- Completing signup+OTP-verify (or logging in with an existing account matching the invite's email) while `?invite=<token>` is present results in that user's `org_id`/`role` reflecting org membership, and the invite's status flips to `accepted` in `GET /orgs/invites`.
- Attempting to accept an invite with a different logged-in email than the invite's `email` returns 403 and leaves the invite `pending`.
- `GET /orgs/members` as the admin lists every member of the org, and the response contains no wallet/balance/free-allowance fields.
- `PATCH /orgs/members/{user_id}/deactivate` flips `is_active` to false, and that user's next request with their existing session cookie gets 401 immediately (not just their next login attempt).
- `PATCH /orgs/members/{user_id}/reactivate` restores login/session access for a previously deactivated member.
- `POST /orgs/members/{user_id}/make-admin` results in exactly one admin per org both before and after — verifiable by the partial unique index never rejecting the transaction, and the prior admin now reading `role = "member"` via `GET /auth/me`.
- A non-admin member calling any `/orgs/members/*` or `/orgs/invites` (POST/GET/DELETE) admin-only endpoint gets 403.
- `apps/web/app/settings/page.tsx` renders the full Team UI for an admin and the read-only panel for a member, reachable via the sidebar's `Settings` link (no longer routing to `/`).
