# Spec: User Registration

## Overview
Stands up the first real slice of `apps/api` (FastAPI app entrypoint, `core/`, `routers/`, `services/`, `schemas/`, `deps.py` — none of which exist yet) and implements account signup: a new visitor becomes a DASHR AI user. Signup is a **two-step flow** — submit name/email/phone/password, then verify a one-time code sent to that phone — so a session is only issued once the phone number is proven reachable. This is the very first API feature and the foundation every later auth step (login/logout in `03-user-login-logout`, and everything after — seller profile, card upload, dashboard) builds on. A user created here has `org_id = NULL, role = NULL` — org creation/invites are a separate future step, out of scope here.

## Depends on
`01-database-setup` — needs `users.email`, `users.password_hash`, `users.org_id`, `users.role`, `users.phone_no` to already exist (they do).

## API endpoints (apps/api)
- `POST /auth/signup` — public — body `{ name, email, phone_no, password }` → creates a `users` row (`org_id = NULL`, `role = NULL`, `phone_verified = false`), hashes the password, generates a 6-digit OTP, hashes and stores it in `phone_otp_verifications` with a 10-minute expiry, sends it to `phone_no` via the OTP provider. Returns `201` with `{ user_id, phone_no }` — **no session cookie yet**, the account isn't usable until the phone is verified
- `POST /auth/signup/verify-otp` — public — body `{ user_id, otp_code }` → checks the code against the latest unexpired, unverified OTP row for that user. On match: marks it verified, sets `users.phone_verified = true`, sets the session cookie, returns `{ user_id, name, email, phone_no, org_id, role, phone_verified }` (now fully logged in). On mismatch: increments the attempt counter and returns a generic `400`. Expired code, or 5+ failed attempts on the current code, also returns `400` and requires a resend
- `POST /auth/signup/resend-otp` — public — body `{ user_id }` → invalidates any still-pending OTP for that user, generates and sends a new one. Rate-limited to one resend per 30 seconds per user; returns `429` if called again before the cooldown elapses
- `GET /auth/me` — org-authenticated — returns the current user's `{ user_id, name, email, phone_no, org_id, role, phone_verified }` from the session, or `401` if not authenticated. Built here (not in the login spec) because it's the only way to verify signup actually left the caller in a logged-in state, and it's shared infrastructure every later auth step reuses as-is

No login, logout, password-reset, email-verification, or org invite/join flow in this step — login/logout is `03-user-login-logout`; the rest are separate future specs. Real SMS delivery is also out of scope — see "OTP delivery" under Rules below.

## Frontend surface (apps/web)
- **New**: `lib/api.ts` — thin `fetch` wrapper for calling `apps/api`, always with `credentials: "include"` so the session cookie round-trips; base URL from `NEXT_PUBLIC_API_URL`. Generic — `03-user-login-logout` reuses this as-is for its login/logout calls
- **Modified**: `app/login/page.tsx` — the form already exists (`mode: "login" | "signup"`, email/password/company fields) but currently just does `router.push("/dashboard")` on submit with no real request, and has **no phone number field**. This step:
  - Adds a required "Phone Number" input to signup mode, alongside the existing Company Name / Work Email / Password fields
  - Adds a third local mode, `"verify-otp"`: after `POST /auth/signup` succeeds, the form switches to a 6-digit code input (auto-advancing per-digit or a single text input — implementer's call, keep it in the existing card design) plus a "Resend code" link and a "Verify" button
  - `mode === "signup"` submit calls `POST /auth/signup`, then transitions to `"verify-otp"` on success, or shows an inline error (e.g. duplicate email) and stays put on failure
  - `"verify-otp"` submit calls `POST /auth/signup/verify-otp`; on success `router.push("/dashboard")`; on failure (wrong/expired code) shows an inline error and keeps the code input open; "Resend code" calls `POST /auth/signup/resend-otp` and disables itself for 30 seconds
  - `mode === "login"` submit stays as today's placeholder until `03-user-login-logout` wires it
- Company Name field in signup mode is currently collected but goes nowhere — this step still doesn't do anything with it (no org creation yet); leave the field in the UI but note in the PR that it's inert until org signup is built
- No route protection/middleware in this step — that's `03-user-login-logout`, paired with login existing so a redirected-out user has somewhere to go back in through

## Database changes
Two changes, both scoped to this step:

- `users.phone_verified` — `BOOLEAN NOT NULL DEFAULT false`. True once the phone's OTP has been verified at least once
- New table `phone_otp_verifications`:

| Column | Type | Notes |
|---|---|---|
| otp_id | UUID | PK, `gen_random_uuid()` |
| user_id | FK → users | not null, `ON DELETE CASCADE` |
| phone_no | TEXT | snapshot of the number the code was sent to (in case the user's phone changes later) |
| otp_code_hash | TEXT | hashed, same scheme as `password_hash` — never store the raw code |
| expires_at | TIMESTAMPTZ | not null, `created_at + 10 minutes` |
| attempts | INTEGER | default `0`, incremented on each failed verify, capped at 5 |
| verified_at | TIMESTAMPTZ | nullable, set on successful verification |
| created_at | TIMESTAMPTZ | default `now()` |

- **Partial unique index** on `users.phone_no` where `phone_verified = true` — once a phone number is verified on one account, no other account can claim and verify that same number. Unverified accounts can still share a phone number in-flight (e.g. two signups racing before either verifies) — the index only bites at the moment of verification, matching the `companies.domain` partial-unique pattern from `01-database-setup`.

This needs a new Alembic revision on top of `01-database-setup`'s migrations (`0004_...` — the next free number after `0001`–`0003`).

## Background jobs
No background job changes. OTP sending happens synchronously in the request (it's a single fast provider call, not bulk/long-running work in the sense CLAUDE.md means for Celery).

## Files to change
- `apps/api/requirements.txt` — add auth-related dependencies
- `apps/web/app/login/page.tsx` — add the phone field, wire the signup-mode submit, add the `verify-otp` mode

## Files to create
- `apps/api/app/main.py` — FastAPI app instance, CORS config (allow the `apps/web` origin, `allow_credentials=True` since the session travels as a cookie), mounts `routers/auth.py`
- `apps/api/app/core/__init__.py`
- `apps/api/app/core/config.py` — `Settings` (Pydantic settings): `DATABASE_URL`, `JWT_SECRET`, `JWT_EXPIRE_MINUTES`, `CORS_ORIGINS`, `OTP_EXPIRE_MINUTES`, `OTP_MAX_ATTEMPTS`, `OTP_RESEND_COOLDOWN_SECONDS`, loaded from env
- `apps/api/app/core/security.py` — password hashing (`passlib` + bcrypt), JWT encode/decode, OTP hashing (reuses the same bcrypt-style hash, not a new scheme)
- `apps/api/app/models/phone_otp_verification.py` — SQLAlchemy model for `phone_otp_verifications`
- `apps/api/migrations/versions/0004_phone_verification.py` — adds `users.phone_verified`, the partial unique index, and the `phone_otp_verifications` table
- `apps/api/app/routers/__init__.py`
- `apps/api/app/routers/auth.py` — `POST /auth/signup`, `POST /auth/signup/verify-otp`, `POST /auth/signup/resend-otp`, `GET /auth/me` for this step (`03-user-login-logout` extends this same file with login/logout, not a new one)
- `apps/api/app/services/__init__.py`
- `apps/api/app/services/auth_service.py` — signup business logic (create user, hash password, issue JWT), used by the router
- `apps/api/app/services/otp_service.py` — generate/hash/verify OTP codes, enforce expiry/attempt-limit/resend-cooldown, calls the OTP provider to actually send
- `apps/api/app/services/otp_provider.py` — `OtpProvider` interface + a `ConsoleOtpProvider` default implementation (logs the code server-side instead of sending a real SMS) — see Rules below
- `apps/api/app/schemas/__init__.py`
- `apps/api/app/schemas/auth.py` — Pydantic request/response models (`SignupRequest`, `VerifyOtpRequest`, `ResendOtpRequest`, `UserOut`)
- `apps/api/app/deps.py` — `get_db()` request dependency, `get_current_user()` dependency that reads the session cookie, verifies the JWT, and loads the `User` row (401 if missing/invalid/expired) — shared infra `03-user-login-logout` reuses unchanged
- `apps/web/lib/api.ts`

## New dependencies
No new dependencies beyond what a bare FastAPI auth stack needs:
- `fastapi` — the API framework itself (not yet a dependency — `apps/api` has only had DB/migration deps so far)
- `uvicorn[standard]` — ASGI server to run `apps/api` locally
- `passlib[bcrypt]` — password hashing (and OTP hashing, reusing the same scheme)
- `pyjwt` — JWT encode/decode
- `pydantic-settings` — typed env config for `core/config.py`

OTP codes are generated with the standard library's `secrets` module — no extra package. Real SMS delivery would need a provider SDK (e.g. an SMS gateway package) but that's explicitly deferred — see Rules below.

No new npm packages — `fetch` covers everything on the frontend.

## Rules for implementation
- Passwords are hashed with `passlib`'s bcrypt scheme before touching the DB — never store or log a plaintext password
- OTP codes are hashed at rest the same way — a leaked DB should not hand out usable codes, even though they're short-lived
- OTP codes are generated with `secrets.randbelow` (or equivalent) — never `random`, and never derived from anything guessable (timestamp, user_id, etc.)
- **OTP delivery is a pluggable seam, not a real SMS integration in this step**: `services/otp_provider.py` defines an `OtpProvider` protocol (`send(phone_no: str, code: str) -> None`) with a `ConsoleOtpProvider` default that logs the code server-side. Wiring a real SMS gateway is a future step — `auth_service`/`otp_service` must depend only on the `OtpProvider` interface, never import a concrete provider directly, so swapping it later is a one-file change
- The session token is a JWT set as an **httpOnly, `Secure`, `SameSite=Lax`** cookie — never returned in a JSON response body, never stored in `localStorage`. It is only issued after OTP verification succeeds — `POST /auth/signup` itself never sets it
- `POST /auth/signup/verify-otp` returns the same generic `400 Invalid or expired code` whether the code is wrong, expired, or attempts are exhausted — don't tell the caller which one, so brute-forcing doesn't get free signal
- Enforce the 5-attempt cap and 30-second resend cooldown at the DB/service layer (checked against `phone_otp_verifications.attempts`/`created_at`), not just in the frontend
- `get_current_user()` in `deps.py` is the single place JWT verification happens — routers never decode the token themselves; build it once here so `03-user-login-logout` and every step after just imports it
- Every new router handler stays thin: parse request → call `services/auth_service.py` or `services/otp_service.py` → return response. No password/OTP hashing, JWT logic, or DB queries directly in `routers/auth.py`
- No raw SQL string interpolation — SQLAlchemy query builder / bound params only
- API contracts are Pydantic models in `schemas/auth.py` — the frontend's `lib/api.ts` types are hand-written to match for now (no codegen pipeline exists yet — that's a future step per CLAUDE.md, not this one)
- CORS is explicitly scoped to the known `apps/web` origin(s) from config — never `allow_origins=["*"]` alongside `allow_credentials=True`
- Design `routers/auth.py` and `services/auth_service.py` so `03-user-login-logout` can add login/logout to them without restructuring anything this step builds

## Definition of done
- [ ] `alembic upgrade head` (revision `0004`) adds `users.phone_verified`, the partial unique index on `phone_no`, and creates `phone_otp_verifications` cleanly on top of `0001`–`0003`
- [ ] `uvicorn app.main:app --reload` starts `apps/api` with no errors
- [ ] `POST /auth/signup` with a new email+phone creates a `users` row (`phone_verified = false`) with a bcrypt hash (not plaintext) in `password_hash`, creates a `phone_otp_verifications` row with a bcrypt-hashed code, and does **not** set a session cookie
- [ ] `POST /auth/signup` with an already-registered email returns a clear `409`, not a raw DB constraint error
- [ ] `POST /auth/signup/verify-otp` with the correct code sets `phone_verified = true`, sets the session cookie, and `GET /auth/me` afterward returns the user with `phone_verified: true`
- [ ] `POST /auth/signup/verify-otp` with a wrong code increments `attempts` and returns `400`; after 5 wrong attempts, the correct code no longer works and a resend is required
- [ ] `POST /auth/signup/verify-otp` with an expired code (past `expires_at`) returns `400`
- [ ] `POST /auth/signup/resend-otp` issues a new working code and invalidates the old one; calling it twice within 30 seconds returns `429`
- [ ] Verifying an OTP for phone number X on account A, then signing up a second account B with the same phone X and trying to verify it, fails at the DB constraint (partial unique index)
- [ ] `GET /auth/me` with no cookie, or an invalid/expired one, returns `401`
- [ ] In `apps/web`, submitting the signup form (including phone number) moves to the OTP-entry screen; entering the correct code lands on `/dashboard`; a wrong code shows an inline error and stays on the OTP screen; "Resend code" works and respects the cooldown
- [ ] In `apps/web`, submitting the signup form with an already-registered email shows an inline error and stays on `/login`
