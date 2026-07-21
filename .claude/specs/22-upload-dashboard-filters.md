# Spec: Upload Dashboard Filters

## Overview
`/dashboard` and `/upload` currently offer different, non-overlapping filters even though both are triage surfaces over the same underlying `VisitingCard` rows in the capture → extraction → enrichment → scoring → review pipeline: `/dashboard` has an exhibition + time-range filter bar scoping its charts, while `/upload` already has an admin-only "uploaded by" filter narrowing its card list but no date filter at all. This feature closes that gap symmetrically — `/upload` gains the same date-range filter `/dashboard` already has, and `/dashboard` gains the same admin-only uploaded-by filter `/upload` already has — so a seller (or an org admin reviewing teammates' work) gets a consistent filtering experience whichever page they're on, and the two pages' numbers stay comparable when scoped the same way.

## Depends on
- **16-dashboard-analytics** — `DashboardFilterBar`/`rangeToDates`/`TimeRangePreset` (the date-range preset UI this spec reuses on `/upload` rather than inventing a second date picker), and `GET /analytics/dashboard`'s existing `exhibition_ids`/`start_date`/`end_date` params + `AnalyticsFilters`/`_apply_shared_filters` (the pattern the new `user_id` param follows).
- **17-admin-user-management** — `role == "admin"` gating, `GET /orgs/members`, and `scope_to_visible_users` (admin sees every org member's rows, a member sees only their own) — this spec's `user_id` filters only ever narrow within what that helper already permits, never widen it.
- **The existing (undocumented-in-spec) "uploaded by" filter already live on `/upload`** (`showUserFilter`/`userFilter` in `apps/web/app/upload/page.tsx`, `user_id` param on `GET /cards`/`card_service.list_cards`) — this spec formalizes that pattern and mirrors it onto `/dashboard`, it does not build it from scratch.

## API endpoints (apps/api)
- `GET /cards` — **modified**, org-authenticated — gains `start_date: date | None = None`, `end_date: date | None = None` query params, filtering on `VisitingCard.created_at` using the same inclusive-start/exclusive-end-plus-one-day pattern already used in `analytics.py::_apply_shared_filters` (`created_at >= start_date`, `created_at < end_date + timedelta(days=1)`), so a `TIMESTAMPTZ` created later in the end day isn't silently excluded. Response shape (`CardOut`) is unchanged.
- `GET /analytics/dashboard` — **modified**, org-authenticated — gains `user_id: uuid.UUID | None = None`, threaded into `AnalyticsFilters` and applied in `_apply_shared_filters` (`VisitingCard.user_id == filters.user_id` when set), narrowing on top of `scope_to_visible_users` exactly like `card_service.list_cards`'s existing `user_id` handling — safe to accept from any caller since a non-admin's query is already self-scoped and can only narrow to "self or nothing." Response shape (`DashboardAnalyticsOut`) is unchanged.

## Frontend surface (apps/web)
- **Modified: `apps/web/components/dashboard-filter-bar.tsx`**
  - `DashboardFilters` gains `userId?: string` (default/absent = "all users").
  - New exported `UploadedByFilter` control (a `<select>`, same visual style as `/upload`'s existing one) — takes `orgMembers: OrgMemberOut[]`, `currentUserId`, `value`, `onChange`; renders `"All users"` + one option per org member (current user labeled "(You)"). Extracted so both pages render the identical control instead of `/dashboard` reimplementing it.
  - `DashboardFilterBar` gains an optional `showUserFilter` + `orgMembers` + `currentUserId` prop set; renders `UploadedByFilter` alongside the existing exhibition/time-range controls only when `showUserFilter` is true.
- **Modified: `apps/web/app/dashboard/page.tsx`**
  - New state: `orgMembers: OrgMemberOut[]`, fetched via `listOrgMembers()` gated on `user?.role === "admin"` (mirrors `/upload`'s existing effect exactly, including swallowing the 403 a non-admin would get).
  - `showUserFilter = user?.role === "admin" && orgMembers.length > 1` (mirrors `/upload`'s `showUserFilter` gate exactly — no single-option control).
  - `filters.userId` threaded into `getDashboardAnalytics({ exhibitionIds, startDate, endDate, userId })`.
- **Modified: `apps/web/app/upload/page.tsx`**
  - New state: `dateFilter: TimeRangePreset` (imported from `dashboard-filter-bar.tsx`), **default `"all"`** — not `"30d"` like `/dashboard` — since `/upload` today shows every un-actioned card with no date scoping and defaulting to a 30-day window would silently hide older cards a seller still needs to parse/enrich/score. Also `customStart`/`customEnd` state for the `"custom"` preset.
  - New UI: the existing time-range `<select>` (+ conditional custom date inputs) from `dashboard-filter-bar.tsx`, rendered next to the exhibition picker near the top of the page, reusing `RANGE_OPTIONS`/`rangeToDates` (exported for reuse) rather than a new component.
  - `refreshCards()` gains `...rangeToDates({ range: dateFilter, customStart, customEnd })` spread into the `listCards(...)` call, and the page-reset effect (`setPage(1)`) and the card-refresh effect both add `dateFilter`/`customStart`/`customEnd` to their dependency arrays, matching how `selectedExhibitionId`/`userFilter` are already handled.
- **Modified: `apps/web/lib/api.ts`**
  - `listCards(params)` gains `start_date?: string; end_date?: string`, appended to the query string when present.
  - `getDashboardAnalytics(params)` gains `userId?: string`, serialized as `user_id` when present.

## Database changes
No database changes. `VisitingCard.created_at` and `VisitingCard.user_id` already exist and are already the columns `scope_to_visible_users`/`analytics.py` filter on — no new columns, tables, or indexes required.

## Background jobs
No background job changes.

## Files to change
- `apps/api/app/routers/cards.py`
- `apps/api/app/services/card_service.py`
- `apps/api/app/routers/analytics.py`
- `apps/api/app/services/analytics.py`
- `apps/web/components/dashboard-filter-bar.tsx`
- `apps/web/app/dashboard/page.tsx`
- `apps/web/app/upload/page.tsx`
- `apps/web/lib/api.ts`

## Files to create
No new files.

## New dependencies
No new dependencies.

## Rules for implementation
- Every query on an org-scoped table filters by `org_id` (already satisfied transitively via `scope_to_visible_users`; do not add a second, redundant `org_id` filter on top of it).
- No raw SQL string interpolation — SQLAlchemy query builder or bound params only.
- Business logic (the `start_date`/`end_date`/`user_id` filtering itself) lives in `card_service.py`/`analytics.py` — the routers only translate query params to service calls, matching every other router in this codebase.
- API contracts are Pydantic models / query params — no new response schemas needed, since this feature only adds request-side filters, not new response fields.
- `GET /analytics/dashboard`'s new `user_id` param must be applied inside `_apply_shared_filters`, narrowing on top of `scope_to_visible_users` — never bypass or replace that visibility scope, and never let it be applied before the visibility `.where()` in a way that could widen results.
- `GET /cards`'s new `start_date`/`end_date` must use the same inclusive-start/exclusive-end-plus-one-day comparison `analytics.py` already uses for `VisitingCard.created_at` — a naive `created_at <= end_date` truncates the end day at midnight and must not be reintroduced.
- The uploaded-by control must only render when `isAdmin && orgMembers.length > 1`, identically on both pages — never show a single-option "uploaded by" control, and never render it at all for a `member`/org-less user.
- The uploaded-by dropdown is populated exclusively from `GET /orgs/members` (already org-scoped, admin-only) on both pages — never a new endpoint, never a global user search.
- `/upload`'s date-range filter defaults to `"all"`; `/dashboard`'s existing default (`"30d"`) is unchanged — do not unify the defaults, since they serve different purposes (dashboard = recent-trend summary, upload = complete actionable queue).

## Definition of done
- As a `member`/org-less user, neither `/dashboard` nor `/upload` renders an "Uploaded by" control.
- As an admin whose org has only themself, neither page renders an "Uploaded by" control (single-option case).
- As an admin whose org has ≥2 members, both `/dashboard` and `/upload` render an "Uploaded by" select populated with the same org members (including "(You)" on the current user's row); selecting a member narrows `/dashboard`'s charts and `/upload`'s card list to only that member's cards, both via `GET .../?user_id=`.
- `/upload` shows a new date-range control with the same five presets as `/dashboard` (Last 30 days / Last 90 days / Last 1 year / All time / Custom range), defaulting to **All time**; switching presets or picking a custom range re-fetches `GET /cards` with matching `start_date`/`end_date` and the visible card list/pagination narrows accordingly.
- `/dashboard`'s existing date-range behavior (including its `"30d"` default) is unchanged.
- Combining both filters on either page narrows to their intersection — a card outside either bound does not appear.
- Manually passing a crafted `user_id` query param to `GET /cards` or `GET /analytics/dashboard` as a non-admin, or as an admin for a user outside their org, returns only the caller's own rows (or empty), never another org member's or another org's data.
- Existing tests for `card_service.list_cards`, `analytics.py`, `/dashboard`, and `/upload` still pass; new tests cover the added `start_date`/`end_date` cards filter and the added `user_id` analytics filter, including the cross-tenant/non-admin narrowing case above.
