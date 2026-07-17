
# Spec: Bulk Select, Parse, and Enrich

## Overview

Today enrichment can only be triggered one card at a time, from inside `CardDetailDrawer` — a seller has to open each card's drawer just to click "Enrich Company". Meanwhile "Parse Cards" already works as a one-click bulk action, but it always acts on *every* `status="new"` card in the current exhibition scope — there's no way to pick specific cards, and no selection UI exists anywhere in the app.

This feature brings enrichment up to parity with parsing as a first-class list action — a per-row "Enrich" icon next to the existing per-row delete icon — and adds explicit multi-select (a header "select all" checkbox plus a per-row checkbox) so a seller can scope *either* bulk action (Parse Selected / Enrich Selected) to a chosen subset of cards instead of only "everything eligible in scope". It's a UI/API convenience layer on top of the existing extraction (05) and enrichment (07) pipelines — it introduces no new pipeline stage, no new Celery task, and no new eligibility rule: a card is still only enrichable once its linked `Company.enrichment_status == "pending"`, and enrichment is still never auto-triggered by parsing.

## Depends on

- `05-parsing-visiting-card` — defines `VisitingCard.status`, the `process_card` task, and `POST /cards/process` this step extends with an optional `card_ids` filter
- `07-data-enrichment` — defines `Company.enrichment_status`, `POST /cards/{card_id}/enrich-company`, and the `enrich_company_task` this step's new bulk endpoint reuses unchanged
- `08-delete-card` — the row-level action-icon area (Trash2) this step adds an Enrich icon next to

## API endpoints (apps/api)

- `POST /cards/process` (existing, extended) — `CardProcessRequest` gains `card_ids: list[uuid.UUID] | None = None`. When provided, `enqueue_processing` narrows its existing org-visibility + `status == "new"` query with `VisitingCard.card_id.in_(card_ids)`, ignoring any id in the list that isn't visible or isn't `"new"` (no error — just excluded from the count). When omitted, behavior is unchanged (all `"new"` cards in scope, optionally narrowed by `exhibition_id`). Response unchanged: `{enqueued_count: int}`.
- `POST /cards/enrich-companies` (new) — org-authenticated, body `{card_ids: list[uuid.UUID]}` (`min_length=1`, else `422`). For each id: resolves via the existing org-scoped `get_visible_card`; skips (does not raise) ids that are not visible, have no linked company, whose company's `enrichment_status != "pending"`, or whose company was already enqueued earlier in the same request (two selected cards can share one still-pending `Company` row). Enqueues `enrich_company_task.delay(company_id, card_id)` for every remaining, deduplicated id. Response: `{enqueued_count: int, skipped_count: int}`.
- `GET /cards` (existing, extended) — `CardOut` gains `company_id: uuid.UUID | None` and `company_enrichment_status: str | None` (mirrors `Company.enrichment_status`, null when the card has no linked company yet) — the minimum needed for the list/table view to decide whether to show the row-level Enrich icon, without pulling in the rest of `CardCompanyOut`.

## Frontend surface (apps/web)

- **Modified: `apps/web/app/upload/page.tsx`**:
  - New header checkbox (selects/deselects every row currently in the table) and a per-row checkbox, backed by a single `selectedCardIds: Set<string>` state.
  - "Parse Cards" is replaced by two always-visible buttons, each independently disabled: **"Parse Selected (N)"** (N = selected rows with `status === "new"`) and **"Enrich Selected (N)"** (N = selected rows with `company_enrichment_status === "pending"`) — both call their respective endpoint with exactly the eligible subset of `selectedCardIds`, then clear the selection and refresh the list.
  - New per-row **Enrich** icon button (`Sparkles`, lucide-react) next to the existing Trash2 delete icon, shown only when that row's `company_enrichment_status === "pending"`, calling the existing `enrichCompany(cardId)` — the same call the drawer's "Enrich Company" button already makes. A per-row `rowEnrichingIds` set disables just that row's icon in flight; a shared error banner (matching the existing `deleteError`/`parseError` banner style) surfaces failures.
  - Row checkbox and the new Enrich icon both call `e.stopPropagation()` so neither opens `CardDetailDrawer`, matching the existing Trash2 button's behavior.
  - Selection is cleared after a successful bulk action, and pruned whenever the card list refreshes so stale ids from deleted/merged cards never linger.
- **No changes to `apps/web/components/card-detail-drawer.tsx`** — its existing single-card "Enrich Company" button is untouched; this step only adds equivalent list-level entry points.

## Database changes

None — no new tables, columns, or constraints. `Company.enrichment_status` and `VisitingCard.company_id` already exist and are simply surfaced further out (via `CardOut`) than before.

## Background jobs

No new or changed Celery tasks. Both bulk endpoints only enqueue existing tasks (`process_card`, `enrich_company_task`) more than one at a time in a single request, exactly like `POST /cards/process` already does for parsing — no task body changes.

## Files to change

- `apps/api/app/schemas/cards.py` — `CardOut` gains `company_id`, `company_enrichment_status`; `CardProcessRequest` gains `card_ids`; new `CardEnrichRequest`/`CardEnrichResponse`
- `apps/api/app/services/card_service.py` — `list_cards` joins `Company` for `enrichment_status`; `enqueue_processing` gains `card_ids` param; new `enqueue_enrichment(db, current_user, card_ids) -> tuple[int, int]`
- `apps/api/app/routers/cards.py` — `process_cards` passes through `card_ids`; new `POST /enrich-companies` route
- `apps/web/lib/api.ts` — `CardOut` type extended; `processCards` takes `{exhibitionId?, cardIds?}`; new `enrichCompanies(cardIds)`
- `apps/web/app/upload/page.tsx` — selection state, header/row checkboxes, updated bulk buttons, new per-row Enrich icon and handler
- `apps/web/__tests__/08-delete-card.test.tsx` — `CardOut` fixtures gain the two new fields

## Files to create

None.

## New dependencies

None.

## Rules for implementation

- Every query in `enqueue_enrichment` and the extended `enqueue_processing` still filters through `get_visible_card`/`scope_to_visible_users` — a `card_ids` list from the client is a hint, never a trusted authorization source; org-scoping is re-derived server-side on every id.
- `enqueue_enrichment` never raises for an individual ineligible/invisible/duplicate-company id — it counts it in `skipped_count` and continues, since this is a best-effort batch over a user-picked selection, not the single-card guarded action `enrich_company_now` already is.
- `enqueue_enrichment` must never call `enrich_company_task.delay(...)` twice for the same `company_id` within one request — track seen company ids and skip (count as skipped) any subsequent card that maps to an already-enqueued company.
- Enrichment eligibility is unchanged from `07-data-enrichment`: only `Company.enrichment_status == "pending"` cards are ever enqueued by either the row icon or the bulk action — bulk enrich must never re-trigger parsing or enrich a company that's `"enriching"`/`"enriched"`/`"not_found"`/`"failed"`.
- Business logic (eligibility filtering, deduping, enqueue loop) lives entirely in `card_service.py`; both routers stay thin try/except-free pass-throughs (no new exception types are needed since neither bulk path raises).
- No raw SQL — SQLAlchemy query builder only, consistent with the rest of `card_service.py`.
- The row-level Enrich icon and the row/header checkboxes must call `e.stopPropagation()` so they never also open `CardDetailDrawer`, matching the existing Trash2 button's established pattern in this file.
- `CardOut`'s new fields are additive only — `CardDetailOut`/`CardCompanyOut` (used by the drawer) are unchanged by this step.

## Definition of done

- `POST /cards/process` with no `card_ids` behaves exactly as before (all `status='new'` cards in scope, optionally by `exhibition_id`).
- `POST /cards/process` with `card_ids` naming a mix of eligible and ineligible (wrong status, or another org's) ids only enqueues the eligible-and-visible subset, and `enqueued_count` matches that subset's size exactly.
- `POST /cards/enrich-companies` with a list containing one pending-company card, one already-enriched-company card, one card with no company, and one nonexistent card id returns `{enqueued_count: 1, skipped_count: 3}`, and exactly one `enrich_company_task.delay` call is made.
- `POST /cards/enrich-companies` with two card ids that share the same still-`"pending"` `company_id` enqueues that company exactly once (`enqueued_count: 1, skipped_count: 1`), never twice.
- `POST /cards/enrich-companies` for a card belonging to another org is skipped (not raised, not enqueued) exactly like a nonexistent id — no `403`/`500`.
- `POST /cards/enrich-companies` with an empty `card_ids` list returns `422`.
- `GET /cards` includes `company_id`/`company_enrichment_status` on every row, `null` for a card with no linked company.
- In the browser: the header checkbox selects/deselects every row; a row checkbox toggles independently; both bulk buttons are disabled at 0 eligible-selected and show the correct live count otherwise.
- In the browser: the per-row Enrich icon appears only for rows whose `company_enrichment_status === "pending"`, disables itself (not the whole row) while its own request is in flight, and never opens the detail drawer when clicked.
- In the browser: clicking "Parse Selected (N)" or "Enrich Selected (N)" only affects the selected-and-eligible rows, clears the selection, and refreshes the list on success.
