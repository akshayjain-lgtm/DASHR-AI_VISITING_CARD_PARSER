# Spec: Company Profile Backend

## Overview
Gives the signed-up seller a real backend for their own company/product profile — the `seller_profiles` row used later to calibrate lead scoring against extracted/enriched prospect data (capture → extraction → enrichment → **scoring**). The `seller_profiles` table and model already exist (`01-database-setup`), and `apps/web/app/profile/page.tsx` already exists as a fully-built UI, but it currently holds hardcoded mock state (`"Thermax Limited"` etc.) with a fake "Save" button that never calls an API. This step adds the missing `GET`/`PUT` backend for that page — get-or-null on first load, upsert on save — and wires the existing page to it. It does not touch scoring itself; `seller_profiles` stays an input scoring will read in a later step.

## Depends on
- `01-database-setup` — `seller_profiles` table (`profile_id`, `user_id` unique, `company_name`, `industry`, `product_lines`, `last_year_revenue`, `revenue_currency`, `target_customer_description`, `target_regions`, `created_at`, `updated_at`) already exists at migration head (`0007`); no new migration needed.
- `02-user-registration` / `03-user-login-logout` — reuses `get_current_user`/session-cookie auth from `app/deps.py`; a profile is always looked up/created for `current_user.user_id`, never a `profile_id` passed by the client.

## API endpoints (apps/api)
- `GET /profile` — org-authenticated (any logged-in user, org membership not required — profiles are `user_id`-scoped, matching `seller_profiles`' existing shape) — returns the caller's `SellerProfileOut`, or a response with every field `null`/`profile_id: null` if the caller has never saved one (first-time visit to the page, no 404 — an unsaved profile is a normal, expected state, not an error).
- `PUT /profile` — org-authenticated — body `SellerProfileUpdate` (all fields optional). Upserts: creates the caller's `seller_profiles` row on first save, updates it on every subsequent save (one row per `user_id`, enforced by the existing unique constraint). Returns the resulting `SellerProfileOut`. A field omitted from the request body leaves that column unchanged; there is no partial-vs-full distinction beyond that — the page always submits the full form, so this only matters for future callers.

No `DELETE` — clearing a profile is out of scope; a seller who wants to reset fields does so by blanking them and saving.

## Frontend surface (apps/web)
- **Modified**: `app/profile/page.tsx` — replace the hardcoded `form` initial state with `useEffect` fetching `GET /profile` on mount (empty-string fields while `null`/loading, matching the existing controlled-input shape). `businessType` and `avgDealSize` fields are removed from `FIELDS` — neither has a backing column in `seller_profiles` (the schema has `last_year_revenue`/`revenue_currency` and `target_customer_description`/`target_regions`, not those two), and this step doesn't add new columns for them (see Database changes). "Save Profile" now calls `updateProfile(form)` (`PUT /profile`) before showing the "Saved!" state; on failure, show an inline error instead of the checkmark and leave the form as-is so nothing entered is lost.
- **Modified**: `lib/api.ts` — add `getProfile()`, `updateProfile(data)`, and a `SellerProfileOut` type matching the Pydantic schema below.

## Database changes
No database changes — `seller_profiles` already has every column this step's endpoints read/write, at migration head `0007`.

## Background jobs
No background job changes — profile read/write is a plain synchronous request; there's no bulk or long-running work here.

## Files to change
- `apps/api/app/main.py` — register the new profile router (`app.include_router(profile_router)`)
- `apps/web/app/profile/page.tsx` — fetch on mount, real save call, drop `businessType`/`avgDealSize` fields, inline error state
- `apps/web/lib/api.ts` — add `getProfile`, `updateProfile`, `SellerProfileOut` type

## Files to create
- `apps/api/app/routers/profile.py` — `GET /profile`, `PUT /profile`
- `apps/api/app/services/profile_service.py` — `get_or_empty_profile(db, current_user)` (returns the row or a not-yet-created sentinel the router maps to null fields) and `upsert_profile(db, current_user, data)` (get-or-create by `user_id`, then apply only the fields present in the request)
- `apps/api/app/schemas/profile.py` — `SellerProfileOut`, `SellerProfileUpdate`

## New dependencies
No new dependencies.

## Rules for implementation
- Every query on `seller_profiles` filters by `user_id = current_user.user_id` — a caller can only ever read or write their own profile; `profile_id` is never accepted as a request parameter.
- No raw SQL string interpolation — SQLAlchemy query builder or bound params only.
- All upsert/lookup logic lives in `profile_service.py`, not in `routers/profile.py` — the router stays a thin call-and-map-to-schema layer, same as `exhibitions.py`.
- `PUT /profile` is a synchronous request-response upsert, not a Celery task — this is a small single-row write, not bulk/long-running work.
- API contract is the Pydantic `SellerProfileOut`/`SellerProfileUpdate` pair in `schemas/profile.py` — the `SellerProfileOut` TS type in `lib/api.ts` is hand-aligned to match, never assumed.
- `revenue_currency` defaults to `'INR'` at the DB level already — `SellerProfileUpdate` treats it as optional and `upsert_profile` never overwrites it with `null` if omitted.

## Definition of done
- [ ] `GET /profile` for a user who has never saved a profile returns `200` with `profile_id: null` and every other field `null` (not a `404`)
- [ ] `PUT /profile` with a full form body for a first-time caller creates exactly one `seller_profiles` row for that `user_id` and returns it with a non-null `profile_id`
- [ ] Calling `PUT /profile` a second time for the same user updates the existing row (`seller_profiles` still has exactly one row for that `user_id`, not two) and `updated_at` advances
- [ ] `GET /profile` after a save returns the previously saved values, including `last_year_revenue`/`revenue_currency`/`target_customer_description`/`target_regions`
- [ ] A `PUT /profile` body that omits `target_regions` leaves the existing stored `target_regions` value unchanged rather than nulling it
- [ ] Calling `GET /profile` or `PUT /profile` without a valid session cookie returns `401`
- [ ] User A's `PUT /profile` never creates or modifies a row for User B — verified by checking `seller_profiles.user_id` on the written row matches the authenticated caller, not any client-supplied id
- [ ] `apps/web/app/profile/page.tsx` loads real saved values into the form on mount (verified against a seeded profile), and clicking "Save Profile" persists edits that are still present after a full page reload
- [ ] The `businessType` and `avgDealSize` fields no longer render on the profile page and are not sent in the `PUT /profile` request body
