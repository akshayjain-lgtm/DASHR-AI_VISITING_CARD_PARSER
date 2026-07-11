# Spec: Lead Scoring

## Overview
This step adds the "scoring" stage of the DASHR AI roadmap (capture → extraction → enrichment → **scoring** → review/export). Once a card has been parsed (step 05) and, optionally, its company enriched with firmographics/signals (step 07), a seller needs a single product-fit number to triage which leads are worth following up on first. This feature computes an explainable, rules-based `lead_score` (0–100) and a `score_breakdown` for each `VisitingCard`, using the seller's own target-customer calibration (`SellerProfile`, step 06) against the prospect's designation seniority, company size, industry/product fit, and buying-intent signals already captured on the card and its linked `Company`/`CompanySignals`. The dashboard's "Leads" table — currently rendering hardcoded mock scores — is wired to this real score so sellers can sort/filter on actual fit instead of a placeholder.

Scoring is deliberately **not** a new `Lead` entity. `VisitingCard` already carries unused `lead_score` (Numeric), `score_breakdown` (JSONB), and `scored_at` (TIMESTAMPTZ) columns from the original schema (`01-database-setup.md`) — this step is the first to populate them. No new table, and no `org_id` column is added anywhere in this step.

## Depends on
- **04 — Visiting card bulk upload**: cards must exist (`visiting_cards`, `upload_batch_id`).
- **05 — Parsing visiting card**: scoring requires a card to have reached `status == "extracted"`, and reuses `designation.classify()` output already stored on `VisitingCard.designation_level`.
- **06 — Company profile backend**: scoring reads `SellerProfile` (`industry`, `product_lines`, `target_customer_description`) as the fit-calibration input.
- **07 — Data enrichment**: scoring reads `CompanySignals` (`linkedin_employee_count`, `hiring_signal`, `gem_tender_count`, `import_export_activity`, `marketplace_verified_badge`, `udyam_category`, `product_lines_summary`) and `Company` (`name`, `enrichment_status`). Enrichment is **not required** before scoring — a card can be scored right after extraction with company-derived criteria simply scoring 0/low until enrichment completes, and re-scored later to pick up enrichment data.

Steps 08 (delete card) and 09 (bulk select/parse/enrich) are unrelated and not required.

## API endpoints (apps/api)

- `POST /cards/{card_id}/score` — score a single card now — org-authenticated (owner or org-admin visibility via `scope_to_visible_users`) — no request body; returns `CardOut` (200). Raises `404` if the card isn't visible to the current user, `409` (`CardNotEligibleForScoringError`) if `card.status != "extracted"`. Mirrors the existing `POST /cards/{card_id}/enrich-company` single-action CTA pattern exactly — an explicit "Score Card" action, never auto-triggered after parsing or enrichment. Re-scoring an already-scored card is allowed (no "already scored" guard) — running it again after enrichment completes is the expected way to pick up better company data.

- `POST /cards/score` — bulk score a seller-selected set of cards — org-authenticated — request `CardScoreRequest {card_ids: list[UUID], min_length=1}`, response `CardScoreResponse {enqueued_count: int, skipped_count: int}` (200). Cards that aren't visible to the current user or aren't `status == "extracted"` are silently skipped and counted, not raised — mirrors `POST /cards/enrich-companies`'s best-effort batch semantics exactly.

Both endpoints enqueue a Celery task per card rather than compute synchronously in the handler, per CLAUDE.md's "bulk/long-running work is a Celery task, not inline in a request handler" and to keep the single- and bulk-card code paths identical (one task, `.delay()`'d once or many times) — see "Background jobs" below.

## Frontend surface (apps/web)

- **Modified: `apps/web/app/dashboard/page.tsx`** — replace the hardcoded `LEADS` mock array with a real `listCards()` fetch. Extend the existing 5-column table (Name / Company / Designation / Score / Exhibition) to render real `lead_score`/`scored_at` through the page's existing `ScoreBadge` component (already buckets `>= 80` HIGH / `>= 60` MED / else LOW — reused as-is, not redesigned). Cards with `lead_score == null` show an "Unscored" badge state instead of a numeric bucket. Add a "Score Selected" bulk action button next to the existing selection controls (from step 09's bulk-select UI), calling `scoreCards(cardIds)`. Stats tiles ("Total Leads", "High Fit", "Low Fit") now derive from real data instead of the mock array.

- **Modified: `apps/web/components/card-detail-drawer.tsx`** — add a score section below the existing company enrichment badge row (employee count, revenue band, GSTIN✓, Udyam✓, hiring signal, Google rating), reusing that row's exact badge style. Shows `lead_score`/`scored_at` when present, a breakdown of the five `score_breakdown` components, and a "Score Card" / "Re-score Card" button calling `scoreCard(cardId)` (disabled unless `status === "extracted"`, matching the backend eligibility rule).

- **Modified: `apps/web/lib/api.ts`** — add `lead_score: number | null`, `score_breakdown: Record<string, number | string> | null`, `scored_at: string | null` to the `CardOut` and `CardDetailOut` TS types (hand-aligned to the Pydantic schema, never assumed). Add `scoreCard(cardId: string): Promise<CardOut>` and `scoreCards(cardIds: string[]): Promise<{enqueued_count: number; skipped_count: number}>` functions, mirroring `enrichCompany`/`enrichCompanies`.

## Database changes
No database changes. `visiting_cards.lead_score` (Numeric), `visiting_cards.score_breakdown` (JSONB), and `visiting_cards.scored_at` (TIMESTAMPTZ) already exist from migration `0001_initial_schema.py` and are already declared on the `VisitingCard` model — this step is the first to write to them. No new table, no new column, no `org_id` addition (scoring reads `Company`/`CompanySignals`, which remain org-agnostic shared cache tables by existing design from step 07; tenant scoping is enforced only on the `visiting_cards` side via the existing `scope_to_visible_users` helper).

`score_breakdown` JSONB shape written by this step (versioned, per the original draft in `01-database-setup.md`):
```json
{
  "des/
}
```
Note: the original draft in `01-database-setup.md` named the fourth field `engagement_score`; this spec renames it `momentum_signal_score` for clarity (it scores company growth/momentum signals, not card-level engagement) — JSONB has no schema to migrate, so this is a naming decision, not a breaking change.

## Background jobs
- **New task: `app.workers.scoring_processing.score_card(self, card_id: str)`** — added to `celery_app.py`'s `include` list. Loads the `VisitingCard` plus its linked `Company`/`CompanySignals` (if any) and the scoring user's `SellerProfile`, calls `scoring.calculate_score(...)`, and writes `lead_score`, `score_breakdown`, `scored_at` onto the card. Follows the exact conventions already established by `enrich_company_task`/`process_card`: `bind=True`, `max_retries=3`, manual `self.retry(countdown=2**self.request.retries)`, `db = SessionLocal(); try/finally: db.close()`. Unlike enrichment, there is no in-flight status to guard against (`card.status` does not change during scoring — no external I/O, so no "scoring" intermediate state is needed); the retry-safety guard instead re-checks `card.status == "extracted"` on every attempt (fresh delivery or retry) and skips as stale if the card's status changed underneath it (e.g. deleted, merged) mid-retry.
- **Trigger**: enqueued only from `POST /cards/{card_id}/score` (single) and `POST /cards/score` (bulk) — never auto-chained after `process_card` or `enrich_company_task` complete, consistent with every existing pipeline stage being a distinct, seller-initiated action.

## Files to change
- `apps/api/app/workers/celery_app.py` — add `app.workers.scoring_processing` to `include`
- `apps/api/app/routers/cards.py` — add `POST /cards/{card_id}/score` and `POST /cards/score` endpoints
- `apps/api/app/schemas/cards.py` — add `lead_score`, `score_breakdown`, `scored_at` to `CardOut` and `CardDetailOut`; add `CardScoreRequest`/`CardScoreResponse`
- `apps/api/app/services/card_service.py` — add `score_card_now()` and `enqueue_scoring()` (mirroring `enrich_company_now()`/`enqueue_enrichment()`); include `lead_score`/`score_breakdown`/`scored_at` in `to_card_out()` and `get_card_detail()`
- `apps/api/app/services/exceptions.py` — add `CardNotEligibleForScoringError`
- `apps/web/lib/api.ts` — extend `CardOut`/`CardDetailOut` types; add `scoreCard`/`scoreCards`
- `apps/web/app/dashboard/page.tsx` — replace mock `LEADS` with real `listCards()`; wire `ScoreBadge` to real scores; add "Score Selected" bulk action
- `apps/web/components/card-detail-drawer.tsx` — add score section + "Score Card" CTA

## Files to create
- `apps/api/app/services/scoring.py` — `calculate_score(card, company, signals, seller_profile) -> dict` returning the `score_breakdown` shape above; all weights, bands, and keyword lists defined as module-level constants (mirrors `enrichment_service.py`'s `_HIRING_SIGNAL_EXPANDING_THRESHOLD` / `_PAID_UP_CAPITAL_BAND_THRESHOLDS` pattern), never inline in the router or worker. Reuses `designation.classify()` for seniority rather than reimplementing it. v1 criteria (max 100):
  - `designation_score` (max 30): from `VisitingCard.designation_level` — c_level=30, director=22, manager=14, individual_contributor=6, none=0
  - `company_size_score` (max 25): from `CompanySignals.linkedin_employee_count` banded (500+=25, 100–499=18, 20–99=10, 1–19=4, none=0); falls back to `CompanySignals.udyam_category` (medium=15, small=8) when employee count is unavailable
  - `industry_fit_score` (max 25): keyword-overlap match between `SellerProfile.industry`/`product_lines` and `Company.name` + `CompanySignals.product_lines_summary` + `VisitingCard.products_offered`, bucketed by overlap strength (0/8/15/25). Documented limitation: no formal NAICS/SIC classification exists yet anywhere in the codebase (confirmed absent from both `Company` and `CompanySignals`), so this is a text-similarity proxy, not a coded industry match — a future step can replace this criterion's implementation without changing its weight or the `score_breakdown` shape.
  - `momentum_signal_score` (max 10): `CompanySignals.hiring_signal == "expanding"` (+4), `gem_tender_count > 0` (+2), `import_export_activity` (+2), `marketplace_verified_badge` (+2)
  - `remark_signal_score` (max 10): keyword scan of `VisitingCard.special_remark` against a static positive-intent keyword list (e.g. "follow up", "urgent", "interested", "budget") — match=10, non-empty remark with no match=3, empty/null=0
- `apps/api/app/workers/scoring_processing.py` — `score_card` Celery task, per "Background jobs" above

## New dependencies
No new dependencies.

## Rules for implementation
- Every query touching `visiting_cards` filters through the existing `scope_to_visible_users` helper against `VisitingCard.user_id` — do not add a new `org_id` column to `visiting_cards`, `companies`, or `company_signals`; follow the org-scoping deviation already established in step 01 (`users.org_id`/`role`, not a literal column on every table)
- No raw SQL string interpolation — SQLAlchemy query builder or bound params only
- All scoring weights, bands, and keyword lists live in `scoring.py` as module-level data, never as inline branches in `routers/cards.py` or `workers/scoring_processing.py`
- Scoring computation itself (`calculate_score`) is a pure function — no DB writes, no Celery/session imports inside `scoring.py`; only the Celery task and service layer touch the database
- Bulk scoring is always a Celery task per card, never synchronous in the request handler, even though the computation itself is cheap — this keeps the single-card and bulk-card code paths identical and leaves room for slower scoring signals (e.g. an LLM-based qualitative fit pass) to be added later without changing the API contract
- `score_card` never changes `VisitingCard.status` — `scored_at` being non-null is the only signal that a card has been scored, exactly as `Company.enrichment_status` (not `VisitingCard.status`) is the signal for enrichment completion
- Scoring is never auto-triggered after parsing or enrichment completes — it is always a seller-initiated action via the `/score` endpoints
- API contracts are the Pydantic models (`CardScoreRequest`/`CardScoreResponse`, extended `CardOut`/`CardDetailOut`) — the TS types in `apps/web/lib/api.ts` are hand-aligned to match, never assumed

## Definition of done
- [ ] `POST /cards/{card_id}/score` on an `extracted` card returns 200 with a populated `lead_score` (0–100), `score_breakdown` (all 5 components + `total` + `version: "v1"`), and `scored_at`, and the same values persist on `GET /cards/{card_id}`
- [ ] `POST /cards/{card_id}/score` on a card with `status != "extracted"` (e.g. `"new"` or `"processing"`) returns 409
- [ ] `POST /cards/{card_id}/score` on a card not visible to the current user (wrong owner, different org) returns 404
- [ ] `POST /cards/score` with a mix of eligible and ineligible card IDs returns correct `enqueued_count`/`skipped_count`, and only eligible cards get a `scoring_processing.score_card` task enqueued (verify via Celery task call assertions in tests)
- [ ] Re-scoring an already-scored card (e.g. after running `POST /cards/{card_id}/enrich-company` and waiting for enrichment to finish) updates `lead_score`/`score_breakdown`/`scored_at` to reflect the newly available company signals, and `company_size_score`/`industry_fit_score`/`momentum_signal_score` visibly change between the pre- and post-enrichment scores
- [ ] A card scored with no linked company (`company_id is None`) still scores successfully, with `company_size_score`, `industry_fit_score`, and `momentum_signal_score` all `0`
- [ ] `apps/web/app/dashboard/page.tsx` renders real scores from `listCards()` (no `LEADS` mock array remaining), `ScoreBadge` buckets match real `lead_score` values, and "Score Selected" enqueues scoring for the current selection
- [ ] `apps/web/components/card-detail-drawer.tsx` shows the score breakdown and a working "Score Card"/"Re-score Card" button that's disabled unless the card's `status === "extracted"`
- [ ] `docker-compose` Celery worker logs show `scoring_processing.score_card` tasks executing (task registered in `celery_app.py`'s `include`)
- [ ] No query against `visiting_cards` in the new code paths omits the `scope_to_visible_users` scoping
