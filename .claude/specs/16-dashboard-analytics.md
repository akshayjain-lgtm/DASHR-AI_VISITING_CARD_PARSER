


# Spec: Dashboard Analytics

## Overview
This feature makes the Dashboard page CLAUDE.md's dedicated **Analytics dashboard** roadmap step (step 8): a visual summary layer — lead volume, industry mix, score distribution, exhibition performance, role mix, region mix — giving sellers at-a-glance triage across exhibitions. The page pivots from "table + charts" to a pure analytics surface: the row-by-row lead table, its search box, and the `CardDetailDrawer` drill-down are removed from `/dashboard` (per-card review still happens on `/upload`, which already has its own row-level view/actions); the "Total Leads / High Fit / Low Fit" stat band moves to the top of the page, and a filter bar (exhibition + time range) sits above the charts and re-scopes every chart identically.

This revision also fixes the Industry Mix chart's biggest weakness — `Company.industry` had no writer anywhere in the codebase, so every card showed as "Unclassified" — by adding the first real classifier for it, and drops the Exhibition Performance chart's average-score sub-chart for now (until scoring itself is revisited), keeping only lead count per exhibition.

A follow-up correction pass (same branch) further refines the filter bar and chart layout: the exhibition filter became multi-select (was single-select), the time-range preset list changed to Last 30 days (new default) / Last 90 days / Last 1 year / All time / Custom range (with explicit start/end date pickers), the "High Fit"/"Low Fit" stat tiles were removed for the time being (Total Leads only, until scoring is revisited — same rationale as dropping `avg_score`), Industry/Region Mix's Y-axis width is now estimated from the labels actually rendered (fixing a fixed-width whitespace gap), Exhibition Performance's rotated axis labels are now drawn with a custom truncating tick renderer (fixing clipped long exhibition names), and every chart/filter control is responsive down to phone widths. Verifying phone-width responsiveness surfaced a pre-existing, page-independent bug: `Sidebar` (shared by every authenticated page — Dashboard, Upload, Company Profile, Wallet, Settings) was a permanently-visible fixed `w-52` column with no small-screen behavior, squeezing every page's content into a sliver at phone widths. `Sidebar` now collapses to a hamburger-triggered slide-in drawer below the `sm` breakpoint on all four pages that render it, not just Dashboard — this is a shared-component fix, not something scoped to charts specifically, but the dashboard chart grid was unusable without it.

## Depends on
- Step 05 (Parsing Visiting Card) — `VisitingCard` rows, `created_at`, `designation_level`, `address`, `products_offered`
- Step 07 (Data Enrichment) — `Company.industry`/`website`, and the `enrich_company_task` worker this spec adds an industry-classification step to
- Step 10 (Lead Scoring) — `lead_score` for the score-distribution chart
- Step 11 (Export Data) — established `/upload` as where per-card review/export happens; this spec extends that separation by removing the last per-card drill-down surface from `/dashboard`
- Supersedes the original cut of this spec (same branch): drops the lead table/search/drawer from `/dashboard`, drops `exhibition_performance.avg_score`, adds `role_mix`/`region_mix`

## Industry classification (new)
`Company.industry` is a plain nullable column that no code path has ever written (confirmed: only ever read, in `analytics.py`, `scoring.py`, `card_service.py`, `export_service.py`) — every enriched company was landing as "Unclassified". This adds the first writer:

- New `apps/api/app/services/industry_classification.py`:
  - `_INDUSTRY_KEYWORDS: dict[str, tuple[str, ...]]` — a fixed taxonomy of ~15 B2B/industrial categories relevant to DASHR's seller base (Automotive & Auto Components, Industrial Machinery & Equipment, Electrical & Electronics, Chemicals & Petrochemicals, Pumps/Valves & Fluid Control, Metals & Fabrication, Plastics/Rubber & Packaging, Textiles & Apparel, Construction & Building Materials, Pharmaceuticals & Healthcare, Food Processing & FMCG, IT & Technology Services, Logistics & Warehousing, Energy & Power, Agriculture & Agri-Equipment), each with a keyword list — same "module-level data, not inline" convention as `scoring.py`/`designation.py`
  - `classify_industry(*, products_offered, website_text, company_name) -> str | None` — pure, no I/O. Tries each input **in priority order**: (1) the card's own `products_offered` text if present — direct human-entered ground truth; (2) the company website's fetched text; (3) the company name as a last resort. For whichever source is tried, every category's keyword hits are counted and the **highest-scoring category wins** ("most prominent" — the category with the strongest textual evidence, not just the first match); a source that scores zero on every category falls through to the next source. Returns `None` (stays "Unclassified") only if no source yields any match.
  - `fetch_website_text(url: str) -> str | None` — a real `httpx.get` (short timeout, wrapped in try/except returning `None` on any failure — DNS, timeout, non-200, etc. — mirroring `enrichment_summary.py`'s existing real Wikipedia fetch pattern, i.e. this codebase already makes real outbound calls to public sources elsewhere, this isn't new precedent), then a lightweight HTML-tag strip to plain text (no new parsing dependency).
- Wired into `apps/api/app/workers/enrichment_processing.py`'s `enrich_company_task`, right after `run_all_signal_lookups` returns: **only if `company.industry is None`** (never re-classify an already-classified company, same caching principle as the rest of enrichment) — loads the triggering card's `products_offered` (the card is already loaded there for `gst_number`), calls `classify_industry(...)`, and sets `company.industry` if a match was found. Never blocks or fails the enrichment task — classification errors are caught the same way `_run_lookup` isolates provider failures.

## Region classification (new)
No `hq_city`/`hq_country` writer exists either (also always NULL). Rather than add a new writer + migration for a field only this chart needs, region is derived **at query time, not persisted** — a deliberate judgment call to avoid a schema change for a first cut:
- New `apps/api/app/services/region_classification.py`: `classify_region(address: str | None) -> str` — keyword match against major Indian states/metro regions (case-insensitive substring match on the card's free-text `address`), returning `"Unclassified"` if nothing matches.
- `analytics.py`'s new `_region_mix` aggregator fetches `(card_id, address)` for the caller's scoped, filtered cards, classifies each in Python, and aggregates counts — the one aggregation in this feature that isn't a pure SQL `GROUP BY`, since the classification itself is Python-side pattern matching over free text.

## API endpoints (apps/api)
- `GET /analytics/dashboard` — same visibility rule as `GET /cards`
  - Query params (all optional): `exhibition_ids: UUID` (repeatable — `?exhibition_ids=a&exhibition_ids=b` — multi-select, `FastAPI`'s `Query(default=None)`; a plain `list[...] | None = None` default silently drops the param from query binding, confirmed via the generated OpenAPI schema), `start_date: date`, `end_date: date` — filter all six aggregations identically. An empty/absent `exhibition_ids` means "all exhibitions"; any non-empty list is applied as `exhibition_id IN (...)`
  - Response (`DashboardAnalyticsOut`):
    ```json
    {
      "lead_volume": [{"date": "2026-07-01", "count": 12}],
      "industry_mix": [{"industry": "Automotive & Auto Components", "count": 34}],
      "score_distribution": {"high": 10, "medium": 20, "low": 5, "unscored": 3},
      "exhibition_performance": [{"exhibition_id": "...", "exhibition_name": "...", "lead_count": 40}],
      "role_mix": [{"role": "c_level", "count": 8}, {"role": "Unclassified", "count": 2}],
      "region_mix": [{"region": "Maharashtra", "count": 12}, {"region": "Unclassified", "count": 5}]
    }
    ```
  - `exhibition_performance` **no longer includes `avg_score`** — lead count only, for the time being, until scoring is revisited
  - `role_mix` groups by `VisitingCard.designation_level`, NULL folded into an explicit `"Unclassified"` key (raw enum values returned; the frontend maps them to display labels, there is no existing label map to reuse)
  - `region_mix` per the Region classification section above
  - All aggregations reuse `scope_to_visible_users` against `VisitingCard.user_id`

## Frontend surface (apps/web)
- **Removed from `apps/web/app/dashboard/page.tsx`**: the lead table, the name/company search box, the non-functional "Filter" button, and all `CardDetailDrawer` wiring (`selectedCardId` state, `listCards()`/`refreshCards()` calls). Per-card review continues to live on `/upload`.
- **New page structure** (top to bottom): stat band (**Total Leads only** — High Fit/Low Fit tiles removed for the time being, until scoring is revisited, same rationale as dropping `avg_score`) → filter bar → chart grid (`grid-cols-1 sm:grid-cols-2 lg:grid-cols-3`, responsive down to phone width).
- **Filter bar** (`dashboard-filter-bar.tsx`) — two controls, both driving a single `filters` state passed to `getDashboardAnalytics(filters)`; changing either re-fetches and re-renders every chart from the same slice, so all numbers always agree with each other, per the dataviz skill's filter-composition rule:
  - **Exhibition filter — multi-select.** A custom checkbox-dropdown (`ExhibitionMultiSelect`, not a native `<select multiple>` — a fixed-height listbox is a poor fit on touch screens), populated via `listExhibitions()`. Empty selection means "all exhibitions"; the trigger button's label summarizes the selection ("All exhibitions" / the one selected name / "N exhibitions"). Selected ids are sent as repeated `exhibition_ids` query params.
  - **Time range preset — `<select>`.** Options: **Last 30 days (default)** / Last 90 days / Last 1 year / All time / **Custom range**. Selecting "Custom range" reveals two `<input type="date">` fields (start/end); `rangeToDates(filters)` resolves whichever mode is active into `{startDate, endDate}`.
- **New/changed chart components** under `apps/web/components/charts/` (all presentational, no fetching inside):
  - `lead-volume-chart.tsx` — unchanged (area chart, brand orange)
  - `industry-mix-chart.tsx` — recolored: each bar now a distinct hue from the shared categorical palette (was single-hue orange) — up to 8 bars (top-7 + "Other"/"Unclassified" folding, unchanged logic). Y-axis width is computed per-render from the actual rendered labels via `estimateCategoryAxisWidth` (was a hardcoded `150`, which left a dead whitespace strip on the left whenever the visible labels were shorter than the longest-ever label)
  - `score-distribution-chart.tsx` — recolored: High stays brand orange (matches the app's existing "hot lead" identity), Medium becomes amber, Low a cool neutral, Unscored unchanged dashed-outline
  - `exhibition-performance-chart.tsx` — simplified to a single categorical-colored bar chart of `lead_count` (the avg-score sub-chart is removed along with the field). X-axis exhibition-name ticks are drawn via a custom `AngledExhibitionTick` renderer that truncates long names to a fixed character cap (full name still shown in the tooltip, since the truncation is display-only) — a fixed-rotation built-in tick still clipped long names against the chart's bottom edge on narrow containers
  - `role-mix-chart.tsx` (new) — donut chart (≤6 categories, part-to-whole at a glance is the dataviz skill's one sanctioned donut use case), categorical palette, with a display-label map (`c_level` → "C-Level", etc.)
  - `region-mix-chart.tsx` (new) — horizontal bar chart, categorical palette, same top-N + "Other" folding pattern as industry mix, same dynamic Y-axis width as industry mix
  - `palette.ts` (new) — the validated 8-hue categorical palette (shared constant, fixed order, not re-cycled) used by all multi-category charts above
  - `chart-utils.ts` (new) — `estimateCategoryAxisWidth(labels, opts?)`, a shared helper estimating a category YAxis's pixel width from the longest label actually being rendered (clamped to a sane min/max), used by `industry-mix-chart.tsx` and `region-mix-chart.tsx`
- **Modified: `apps/web/lib/api.ts`** — `ExhibitionPerformance` type drops `avg_score`; new `RoleMixPoint`/`RegionMixPoint` types; `DashboardAnalyticsOut` gains `role_mix`/`region_mix`; `getDashboardAnalytics({exhibitionIds, startDate, endDate})` now accepts `exhibitionIds: string[]` (was a single `exhibitionId: string`), serialized as repeated `exhibition_ids` query params

## Database changes
No new tables or columns. `Company.industry` already exists (`company.py:30`) and simply gets its first writer. Region is intentionally not persisted (see Region classification above).

## Background jobs
No new Celery tasks. Industry classification is a small addition inside the existing `enrich_company_task`, not a new job — it runs exactly once per company (guarded by `company.industry is None`), same lifecycle as the rest of enrichment.

## Files to change
- `apps/api/app/workers/enrichment_processing.py` — call the new classifier after `run_all_signal_lookups`
- `apps/api/app/services/analytics.py` — add `_role_mix`/`_region_mix`, drop `avg_score` from `_exhibition_performance`, `exhibition_id` → `exhibition_ids: list[UUID] | None` (`.in_()` filter) throughout
- `apps/api/app/schemas/analytics.py` — add `RoleMixPoint`/`RegionMixPoint`, drop `avg_score`
- `apps/api/app/routers/analytics.py` — `exhibition_ids: list[uuid.UUID] | None = Query(default=None)` (a bare `list[...] | None = None` default is silently dropped from query-param binding — confirmed via the generated OpenAPI schema, not documented anywhere else in this codebase since no other route takes a list query param)
- `apps/web/app/dashboard/page.tsx` — remove table/search/drawer, relocate stat band (Total Leads only), add filter bar + 6-chart grid, responsive grid classes
- `apps/web/lib/api.ts` — type updates described above, `getDashboardAnalytics` takes `exhibitionIds: string[]`
- `apps/web/components/charts/industry-mix-chart.tsx`, `score-distribution-chart.tsx`, `exhibition-performance-chart.tsx`, `region-mix-chart.tsx` — recolor/simplify/dynamic-width/truncating-tick fixes described above
- `apps/web/__tests__/10-lead-scoring.test.tsx`, `apps/web/__tests__/11-export-data.test.tsx` — remove/adjust assertions that referenced the now-removed Dashboard table
- `apps/web/__tests__/16-dashboard-analytics.test.tsx` — updated for the multi-select filter UI, the removed High/Low Fit tiles, and the new default/custom time-range behavior
- `apps/api/tests/test_analytics.py` — `exhibition_id` → `exhibition_ids` filter tests, plus a new multi-value-filter test
- `apps/web/components/sidebar.tsx` — collapses to a hamburger/slide-in drawer below `sm`, unchanged fixed column at `sm`+
- `apps/web/app/dashboard/page.tsx`, `apps/web/app/upload/page.tsx`, `apps/web/app/profile/page.tsx`, `apps/web/app/wallet/page.tsx` — root layout `flex` → `flex flex-col sm:flex-row` so the mobile Sidebar topbar stacks above page content instead of sitting beside it in a row
- `CLAUDE.md` — "Dashboard & marketing pages" section updated: the Dashboard is now a pure analytics surface, not "table + charts on one page"

## Files to create
- `apps/api/app/services/industry_classification.py`
- `apps/api/app/services/region_classification.py`
- `apps/api/tests/test_industry_classification.py`
- `apps/web/components/charts/role-mix-chart.tsx`
- `apps/web/components/charts/region-mix-chart.tsx`
- `apps/web/components/charts/palette.ts`
- `apps/web/components/charts/chart-utils.ts`
- `apps/web/components/dashboard-filter-bar.tsx`

## New dependencies
None — `httpx` is already a backend dependency (used by `enrichment_summary.py`); no new npm packages beyond the already-added `recharts`.

## Rules for implementation
- Every query on `VisitingCard`/`Company`/`Exhibition` filters through `scope_to_visible_users`/`user_id`
- No raw SQL string interpolation — SQLAlchemy query builder only; `region_mix`'s Python-side classification is the one deliberate exception to "aggregate in SQL," documented above, not a precedent for other fields
- Industry/region taxonomies are fixed module-level data (keyword tuples/dicts), never inline in a route handler or aggregator
- `fetch_website_text`/industry classification must never raise into `enrich_company_task` — wrap and log, exactly like `_run_lookup`'s existing isolation
- Only classify industry when `company.industry is None` — never re-fetch/re-classify an already-classified company
- Chart components stay presentational; the filter bar is the only place that owns fetch-triggering state
- Categorical hues are assigned in the palette's fixed order, never cycled per-render or re-shuffled when the filtered category count changes

## Definition of done
- [ ] `GET /analytics/dashboard` returns `lead_volume`, `industry_mix`, `score_distribution`, `exhibition_performance` (lead_count only, no `avg_score`), `role_mix`, `region_mix`, all scoped to the caller's visible cards
- [ ] A newly enriched company with a `products_offered`-bearing source card gets a real, non-null `Company.industry` (not "Unclassified") from the products-offered text, without any network call
- [ ] A company with no `products_offered` signal but a reachable website gets classified from fetched website text; a failed fetch (timeout/DNS/4xx/5xx) never fails or blocks `enrich_company_task`
- [ ] `role_mix` groups correctly by `designation_level`, nulls folded into `"Unclassified"`
- [ ] `region_mix` classifies `card.address` into Indian state/metro buckets, unmatched addresses folded into `"Unclassified"`
- [ ] `exhibition_ids`/`start_date`/`end_date` filters apply identically across all six aggregations; multiple `exhibition_ids` values are unioned (`IN`), not intersected
- [ ] `/dashboard` renders: stat band (Total Leads only) at the top, filter bar (multi-select exhibition + time range) above the chart grid, six charts below — no lead table, search box, `CardDetailDrawer`, or High Fit/Low Fit tile anywhere on the page
- [ ] Changing any filter control (exhibition multi-select, time-range preset, or custom start/end date) re-fetches and re-renders every chart consistently (no stale chart left showing the prior slice)
- [ ] Time-range preset defaults to "Last 30 days" on first load; selecting "Custom range" reveals start/end date pickers that drive the fetch once both are set
- [ ] Each multi-category chart (industry/role/region/exhibition) uses the shared categorical palette in fixed hue order, not a single monochrome hue
- [ ] Industry Mix and Region Mix charts size their Y-axis to the labels actually rendered, not a fixed width wider than needed
- [ ] Exhibition Performance's exhibition-name axis labels are never visually clipped, on any chart container width
- [ ] Dashboard renders a sensible empty state for a zero-card account across all six charts
- [ ] The filter bar and all six charts remain usable (no horizontal overflow, no clipped controls/labels) at phone, tablet, and laptop widths
- [ ] `apps/api/tests/test_analytics.py` updated for the new/changed aggregations, including multi-value `exhibition_ids`; `apps/api/tests/test_industry_classification.py` covers the priority order and most-prominent-match selection with no real network calls (website fetch mocked)
