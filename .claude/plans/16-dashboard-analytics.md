# Implementation Plan: Dashboard Analytics (spec 16-dashboard-analytics.md)

## Context
The DASHR AI Dashboard page (`apps/web/app/dashboard/page.tsx`) today only shows a scored lead table with 3 stat tiles. CLAUDE.md's Dashboard section already calls for "a scored/filterable lead table plus an analytics layer above it (charts/graphs on lead volume, industry mix, score distribution, exhibition performance, etc.)" — that analytics layer has never been built. Spec `.claude/specs/16-dashboard-analytics.md` (already written, on branch `feature/dashboard-analytics`) scopes exactly that gap: a new read-only backend aggregation endpoint plus four Recharts charts rendered above the existing table, with zero changes to the existing table/tiles/search/drawer.

Mid-session, the user additionally asked that "Analytics dashboard" be promoted to its own numbered step (step 8) in CLAUDE.md's "Core workflows" list in the Project overview — today it's only implied inside step 5 ("Review & export ... on an analytics dashboard"). This plan folds that CLAUDE.md documentation update in alongside the feature build, since CLAUDE.md's own rule is to "keep this file in sync with what's actually built."

Testing scope: this plan covers **dev-level verification only** — writing and running `pytest`/`vitest` tests and using the `dashr-test-runner` subagent to triage results (per CLAUDE.md's subagent policy). The user will separately trigger the `/test-feature` QA pipeline themselves; this plan does not invoke it.

## Step 0 — Save this plan in-repo
No `.claude/plans/` (or similar) directory existed anywhere in this repo before this feature — specs (`.claude/specs/`) were the only planning artifact ever committed. This file establishes that convention.

## Step 1 — CLAUDE.md roadmap update
In the "Core workflows" numbered list:
- Trim item 5 to remove the now-redundant analytics-dashboard mention: `5. **Review & export** — sales reps triage a scored lead list per exhibition and push qualified leads to CRM`
- Add a new item 8: `8. **Analytics dashboard** — a visual summary layer (lead volume, industry mix, score distribution, exhibition performance) surfaced above the scored lead table so sellers get at-a-glance triage across exhibitions, not just a row-by-row list`

## Step 2 — Backend: new `/analytics/dashboard` endpoint
New files:
- `apps/api/app/schemas/analytics.py` — `LeadVolumePoint {date, count}`, `IndustryMixPoint {industry, count}`, `ScoreDistributionOut {high, medium, low, unscored}`, `ExhibitionPerformancePoint {exhibition_id, exhibition_name, lead_count, avg_score}`, `DashboardAnalyticsOut` wrapping all four.
- `apps/api/app/services/analytics.py` — `get_dashboard_analytics(db, current_user, exhibition_id, start_date, end_date)` calling four private helpers, each scoped via `scope_to_visible_users(select(...), current_user, VisitingCard.user_id)` plus shared optional filters. Score-bucket cutoffs (`>=80` high, `60–79` medium, `<60` low, `NULL` unscored) live as named module-level constants, mirroring `scoring.py`'s constants style.
  - `lead_volume`: `GROUP BY cast(created_at AS date)`, ascending, gaps omitted.
  - `industry_mix`: LEFT JOIN `Company`, `CASE` mapping `NULL`/`''` industry to `"Unclassified"`, grouped, ordered by count descending. No top-N folding server-side.
  - `score_distribution`: single `CASE`-bucketed `GROUP BY`, backfilling absent buckets to `0`.
  - `exhibition_performance`: INNER JOIN `Exhibition`, `func.avg(lead_score)` (Postgres ignores NULLs, so zero-scored exhibitions yield `avg_score: None`).
- `apps/api/app/routers/analytics.py` — `router = APIRouter(prefix="/analytics", tags=["analytics"])`, single `GET /dashboard` handler.

Modified: `apps/api/app/main.py` (register the new router).

## Step 3 — Backend tests: `apps/api/tests/test_analytics.py`
Sync `TestClient`, real Postgres `dashr_test` DB, two independent `TestClient` instances for tenant-isolation checks, `db_session`-seeded fixture rows for deterministic score/date boundaries. Coverage: own-data-only scoping, score bucket boundaries (79/80, 59/60, null), industry `"Unclassified"` rollup, `avg_score` null-handling, `lead_volume` day-bucketing, query-param filters, and a `pytest.mark.skip` placeholder for admin-org-aggregation (matching the existing gap at `test_04_visiting_card_bulk_upload.py:770-781`).

## Step 4 — Frontend: types + API client
`apps/web/lib/api.ts` — append `LeadVolumePoint`, `IndustryMixPoint`, `ScoreDistribution`, `ExhibitionPerformance`, `DashboardAnalyticsOut` types and `getDashboardAnalytics(params?)`, matching `listCards()`'s exact pattern.

## Step 5 — Frontend: chart components (`apps/web/components/charts/`)
- `lead-volume-chart.tsx` — area chart, brand-orange gradient.
- `industry-mix-chart.tsx` — horizontal bar chart, top-7 + "Other" folding (presentational only), `"Unclassified"` always its own bar.
- `score-distribution-chart.tsx` — vertical bar chart, colors matching the page's existing `ScoreBadge`.
- `exhibition-performance-chart.tsx` — two small-multiple bar charts (lead count / avg score) sharing an x-axis, not a dual-axis combo chart.
- Each renders its own empty-state placeholder.

## Step 6 — Frontend: wire into `dashboard/page.tsx`
Add `analytics` state, fetch `getDashboardAnalytics()` alongside `listCards()` in the existing `useEffect`, insert the new chart grid as the first child of the `p-8 space-y-6` wrapper, above the existing stat tiles. No other line in the file changes.

## Step 7 — `recharts` dependency
`npm install recharts` inside `apps/web`.

## Step 8 — Frontend tests: `apps/web/__tests__/16-dashboard-analytics.test.tsx`
Mock `useRouter`, stub `global.fetch` with a new `/api/analytics/dashboard` branch, render the real Dashboard page, assert chart sections render and the zero-data fixture renders empty states without crashing; regression-check existing tiles/search/table.

## Sequencing
Backend (steps 2–3) → manual sanity check → Frontend (steps 4–8) → manual visual check → Definition of Done pass.

## Verification
- `cd apps/api && pytest tests/test_analytics.py -v`
- `cd apps/web && npx vitest run 16-dashboard-analytics.test.tsx`
- Manual: dev stack up, hit `/analytics/dashboard`, visually confirm the dashboard page for both a populated and a zero-card account.
- `dashr-test-runner` subagent invoked after both test runs (dev-level verification only — `/test-feature` QA pipeline is run separately by the user).

## Critical files
- `apps/api/app/services/analytics.py`, `apps/api/app/routers/analytics.py`, `apps/api/app/schemas/analytics.py`, `apps/api/app/main.py`
- `apps/web/lib/api.ts`, `apps/web/app/dashboard/page.tsx`, `apps/web/components/charts/*.tsx`
- `apps/api/tests/test_analytics.py`, `apps/web/__tests__/16-dashboard-analytics.test.tsx`
- `CLAUDE.md`, `.claude/plans/16-dashboard-analytics.md`

