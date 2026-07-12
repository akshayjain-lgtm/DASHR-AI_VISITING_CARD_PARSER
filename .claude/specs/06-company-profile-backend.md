# Spec: Company Profile Backend

## Overview
Gives the signed-up seller a real backend for their own company/product profile — the `seller_profiles` row used later to calibrate lead scoring against extracted/enriched prospect data (capture → extraction → enrichment → **scoring**). The `seller_profiles` table and model already exist (`01-database-setup`), and `apps/web/app/profile/page.tsx` already exists as a fully-built UI, but it currently holds hardcoded mock state (`"Thermax Limited"` etc.) with a fake "Save" button that never calls an API. This step adds the missing `GET`/`PUT` backend for that page — get-or-null on first load, upsert on save — and wires the existing page to it. It does not touch scoring itself; `seller_profiles` stays an input scoring will read in a later step.

**Update (post-launch addendum):** `seller_profiles` also carries the caller's `gst_no` and `billing_address` — used later so a future Invoicing spec can bill the recharging user correctly. These live on `seller_profiles` rather than `users` even though CLAUDE.md's original data-model sketch put them on `User`: `seller_profiles` is already a strict 1:1 extension of `User` (unique `user_id`), it's already the backend for the one existing profile page, and adding two nullable columns here avoids a second table/endpoint pair for what is, in practice, the same "fill in your profile" moment. Both fields are **optional, full stop** — a seller can save/update their profile with either or both blank, and neither is ever a precondition for anything, including future Invoice generation. A future Invoicing spec bills whatever value (including blank) is on file at issue time.

## Depends on
- `01-database-setup` — `seller_profiles` table (`profile_id`, `user_id` unique, `company_name`, `industry`, `product_lines`, `last_year_revenue`, `revenue_currency`, `target_customer_description`, `target_regions`, `created_at`, `updated_at`) already exists at migration head (`0007`); no new migration needed.
- `02-user-registration` / `03-user-login-logout` — reuses `get_current_user`/session-cookie auth from `app/deps.py`; a profile is always looked up/created for `current_user.user_id`, never a `profile_id` passed by the client.

## API endpoints (apps/api)
- `GET /profile` — org-authenticated (any logged-in user, org membership not required — profiles are `user_id`-scoped, matching `seller_profiles`' existing shape) — returns the caller's `SellerProfileOut`, or a response with every field `null`/`profile_id: null` if the caller has never saved one (first-time visit to the page, no 404 — an unsaved profile is a normal, expected state, not an error). `SellerProfileOut` includes `gst_no: str | None` and `billing_address: str | None`.
- `PUT /profile` — org-authenticated — body `SellerProfileUpdate` (all fields optional, including `gst_no` and `billing_address`). Upserts: creates the caller's `seller_profiles` row on first save, updates it on every subsequent save (one row per `user_id`, enforced by the existing unique constraint). Returns the resulting `SellerProfileOut`. A field omitted from the request body leaves that column unchanged; there is no partial-vs-full distinction beyond that — the page always submits the full form, so this only matters for future callers. Submitting `gst_no`/`billing_address` as an empty string is a valid way to clear a previously-saved value (same as any other text field here) — omitting the key entirely is what leaves it unchanged.

No `DELETE` — clearing a profile is out of scope; a seller who wants to reset fields does so by blanking them and saving.

## Frontend surface (apps/web)
- **Modified**: `app/profile/page.tsx` — replace the hardcoded `form` initial state with `useEffect` fetching `GET /profile` on mount (empty-string fields while `null`/loading, matching the existing controlled-input shape). `businessType` and `avgDealSize` fields are removed from `FIELDS` — neither has a backing column in `seller_profiles` (the schema has `last_year_revenue`/`revenue_currency` and `target_customer_description`/`target_regions`, not those two), and this step doesn't add new columns for them (see Database changes). "Save Profile" now calls `updateProfile(form)` (`PUT /profile`) before showing the "Saved!" state; on failure, show an inline error instead of the checkmark and leave the form as-is so nothing entered is lost. Add two more entries to `FIELDS` — "GST No." (`gstNo` → `gst_no`) and "Billing Address" (`billingAddress` → `billing_address`, `multi: true`) — placed after the existing fields, each with placeholder copy that makes clear they're optional (e.g. `"GSTIN (optional)"`, `"Billing address for invoices (optional)"`). No client-side required-field validation on either — both submit fine blank, same as every other field on this form today.
- **Modified**: `lib/api.ts` — add `getProfile()`, `updateProfile(data)`, and a `SellerProfileOut` type matching the Pydantic schema below, including `gst_no`/`billing_address`.

## Database changes
New Alembic migration `0011_seller_profile_gst_billing.py` (revises `0010`), adding two nullable columns to `seller_profiles`:
- `gst_no` (String, nullable, no length constraint beyond the Pydantic schema's `max_length`) — no format/checksum validation; GSTIN format varies and isn't required by this feature, and not every seller is GST-registered.
- `billing_address` (String, nullable) — free-form multi-line address. Uses `String` rather than `Text` to match every other nullable free-text column already on `seller_profiles` (`company_name`, `industry`, `target_regions`, etc., all `sa.String()` since migration `0002`) — Postgres treats unbounded `String`/`Text` identically, so this is a convention choice, not a functional one.

Both are plain `ALTER TABLE ... ADD COLUMN ... NULL` — no backfill needed, no default, no `NOT NULL` constraint (they're optional by design).

## Background jobs
No background job changes — profile read/write is a plain synchronous request; there's no bulk or long-running work here.

## Files to change
- `apps/api/app/main.py` — register the new profile router (`app.include_router(profile_router)`)
- `apps/api/app/models/seller_profile.py` — add `gst_no: Mapped[str | None]` and `billing_address: Mapped[str | None]`
- `apps/web/app/profile/page.tsx` — fetch on mount, real save call, drop `businessType`/`avgDealSize` fields, inline error state, add GST No./Billing Address fields
- `apps/web/lib/api.ts` — add `getProfile`, `updateProfile`, `SellerProfileOut` type (with `gst_no`/`billing_address`)

## Files to create
- `apps/api/app/routers/profile.py` — `GET /profile`, `PUT /profile`
- `apps/api/app/services/profile_service.py` — `get_or_empty_profile(db, current_user)` (returns the row or a not-yet-created sentinel the router maps to null fields) and `upsert_profile(db, current_user, data)` (get-or-create by `user_id`, then apply only the fields present in the request)
- `apps/api/app/schemas/profile.py` — `SellerProfileOut`, `SellerProfileUpdate`
- `apps/api/migrations/versions/0011_seller_profile_gst_billing.py` — adds `gst_no`/`billing_address` to `seller_profiles`

## New dependencies
No new dependencies.

## Rules for implementation
- Every query on `seller_profiles` filters by `user_id = current_user.user_id` — a caller can only ever read or write their own profile; `profile_id` is never accepted as a request parameter.
- No raw SQL string interpolation — SQLAlchemy query builder or bound params only.
- All upsert/lookup logic lives in `profile_service.py`, not in `routers/profile.py` — the router stays a thin call-and-map-to-schema layer, same as `exhibitions.py`.
- `PUT /profile` is a synchronous request-response upsert, not a Celery task — this is a small single-row write, not bulk/long-running work.
- API contract is the Pydantic `SellerProfileOut`/`SellerProfileUpdate` pair in `schemas/profile.py` — the `SellerProfileOut` TS type in `lib/api.ts` is hand-aligned to match, never assumed.
- `revenue_currency` defaults to `'INR'` at the DB level already — `SellerProfileUpdate` treats it as optional and `upsert_profile` never overwrites it with `null` if omitted.
- `gst_no` and `billing_address` are optional in both `SellerProfileUpdate` (no `...` / required marker) and at the DB level (nullable columns, no `CheckConstraint`) — do not add backend validation that makes either required. Follow the existing `Field(default=None, max_length=...)` pattern used for the other free-text fields in this schema (e.g. `max_length=20` for `gst_no`, `max_length=500` for `billing_address`, matching `target_regions`).

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
- [ ] `PUT /profile` with `gst_no`/`billing_address` omitted entirely (existing form-shaped body) still returns `200` — neither field is required to save a profile
- [ ] `PUT /profile` with `gst_no` and `billing_address` set persists both, and a subsequent `GET /profile` returns them
- [ ] `apps/web/app/profile/page.tsx` renders "GST No." and "Billing Address" fields that save and reload like the other fields, with no required-field error when left blank
