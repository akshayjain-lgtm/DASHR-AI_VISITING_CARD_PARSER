# Spec: User Login-Logout

## Overview
Adds sign-in and sign-out to the auth scaffold `02-user-registration` already built (FastAPI app, `core/`, `routers/auth.py`, `services/auth_service.py`, `schemas/auth.py`, `deps.py`, `GET /auth/me`) — plus real route protection, so `/dashboard` and `/profile` require a session. A returning user can now leave and come back, not just sign up once and stay logged in for the session.

## Depends on
`02-user-registration` — needs the FastAPI app, `core/security.py` (hashing + JWT), `deps.py` (`get_current_user`), `routers/auth.py`, `services/auth_service.py`, `schemas/auth.py`, and `GET /auth/me` to already exist. This step **extends** those files; it does not create a parallel auth stack.

## API endpoints (apps/api)
- `POST /auth/login` — public — body `{ email, password }` → verifies credentials, sets the session cookie, returns `{ user_id, name, email }`. Wrong email or wrong password both return the same `401` with a generic message (don't leak which one was wrong)
- `POST /auth/logout` — org-authenticated (any logged-in user) — clears the session cookie, returns `204`

`GET /auth/me` already exists from `02-user-registration` and is unchanged here — this step just adds more ways to end up authenticated (login) and unauthenticated (logout).

No password-reset, email-verification, or org invite/join flow in this step — those remain separate future specs. No signup/registration here — that's `02-user-registration`.

## Frontend surface (apps/web)
- **New**: `lib/auth.ts` — `getCurrentUser()` helper (calls `GET /auth/me`) used by protected pages/middleware to check session state
- **New**: `middleware.ts` at the app root — redirects unauthenticated requests away from `/dashboard`, `/profile`, and `/upload` back to `/login`; also redirects an *authenticated* request landing on `/` (the public marketing homepage) to `/dashboard` — an authenticated user should never see the logged-out marketing page just by navigating back to `/`
- **Modified**: `app/login/page.tsx` — `mode === "signup"` submit was already wired in `02-user-registration`; this step wires the remaining `mode === "login"` submit to call `POST /auth/login` via the existing `lib/api.ts`; on success `router.push("/dashboard")`; on failure show the inline error (same pattern already used for signup)
- **Modified**: `components/sidebar.tsx` — "Sign Out" button currently just does `router.push("/")`. Wire it to call `POST /auth/logout` first, then redirect home. The sidebar's logo (top-left) is part of the authenticated app shell, so its click target is `/dashboard`, not `/` — clicking it must never route through the public marketing page or make the user appear logged out

## Database changes
No database changes.

## Background jobs
No background job changes.

## Files to change
- `apps/api/app/routers/auth.py` — add `login`/`logout` handlers alongside the existing `signup`
- `apps/api/app/services/auth_service.py` — add credential-verification logic alongside the existing signup logic
- `apps/api/app/schemas/auth.py` — add `LoginRequest`
- `apps/web/app/login/page.tsx` — wire the login-mode submit only (signup mode already wired)
- `apps/web/components/sidebar.tsx` — wire real logout call

## Files to create
- `apps/web/lib/auth.ts`
- `apps/web/middleware.ts`

## New dependencies
No new dependencies — reuses everything `02-user-registration` already added (`fastapi`, `uvicorn`, `passlib[bcrypt]`, `pyjwt`, `pydantic-settings`).

## Rules for implementation
- Reuse `get_current_user()` from `deps.py` as-is for the `POST /auth/logout` auth requirement — do not write a second auth dependency
- `POST /auth/login` returns the same generic `401 Invalid email or password` whether the email doesn't exist or the password is wrong — no user enumeration
- Login issues the same cookie shape as signup (httpOnly, `Secure`, `SameSite=Lax` JWT) — one cookie contract for the whole app, not a variant per entry point
- Logout clears the cookie server-side (`Set-Cookie` with an expired/empty value) — don't rely on the frontend just "forgetting" the token
- `middleware.ts` checks for the session cookie's presence for routing UX only; it does not verify the JWT signature (that stays server-side in `apps/api`'s `get_current_user()`) — a forged/expired cookie still gets a `401` from any real API call, middleware just avoids flashing a protected page before that happens
- `/` is public (no cookie) and authenticated (cookie present) reachable, but never both at once from the user's perspective: an authenticated request to `/` is redirected to `/dashboard` by `middleware.ts` before the marketing page ever renders, so no page in the authenticated app shell (sidebar logo, nav items) should ever leave the user looking logged-out just by linking back to `/`
- Sign-out must redirect via a hard navigation (`window.location.href`), not `router.push`: Next's client Router Cache can hold a stale `/` → `/dashboard` redirect cached from earlier in the same authenticated session (e.g. from clicking the logo), and a soft client-side transition after logout can serve that stale cached redirect instead of re-checking the now-cleared cookie — bouncing a freshly-logged-out user right back into the dashboard. A hard navigation always re-hits middleware fresh
- No raw SQL string interpolation — SQLAlchemy query builder / bound params only
- Router handlers stay thin, same as `02-user-registration`: parse request → call `services/auth_service.py` → return response

## Definition of done
- [ ] `POST /auth/login` with correct credentials returns `200` and sets the session cookie
- [ ] `POST /auth/login` with a wrong password, and with a non-existent email, both return the same `401` body
- [ ] `POST /auth/logout` clears the cookie; a subsequent `GET /auth/me` returns `401`
- [ ] In `apps/web`, submitting the login form with valid credentials navigates to `/dashboard`; with invalid credentials shows an inline error and stays on `/login`
- [ ] Visiting `/dashboard` or `/profile` directly while logged out redirects to `/login`
- [ ] Clicking "Sign Out" in the sidebar ends the session (`GET /auth/me` returns `401` afterward) and returns to `/`
- [ ] While logged in, visiting `/` directly (or clicking the sidebar logo) redirects to `/dashboard` instead of showing the logged-out marketing page
