# Spec: Export Data

## Overview
This step adds the "review & export" stage that closes out the DASHR AI roadmap (capture → extraction → enrichment → scoring → **review/export**). Once a seller has scored their leads (step 10) and triaged their batch on the `/upload` page's card list, they need a way to get a qualified subset out of DASHR AI and into whatever CRM or spreadsheet workflow they already use — DASHR AI has no CRM integration yet, so "push to CRM" for v1 means a CSV download a rep can import anywhere. This feature adds a `POST /cards/export` endpoint that turns a seller-picked selection of cards (reusing the existing bulk-select UI from step 09) into a CSV file with one row per lead — contact fields, company firmographics, and lead score together — and an "Export" action on the `/upload` page next to the existing Parse/Enrich/Score/Delete bulk actions. The export CTA lives on `/upload`, not the `/dashboard` Leads page — a seller reviewing a just-processed batch triages and exports it right there, without navigating away.

## Depends on
- **04 — Visiting card bulk upload**: cards must exist (`visiting_cards`).
- **05 — Parsing visiting card**: export reads `full_name`, `job_title`, `website`, `address`, `products_offered`, `gst_number`, `designation_level`, `special_remark`, and linked `CardEmail`/`CardPhone` rows populated by extraction.
- **07 — Data enrichment**: export reads `Company` (`name`, `industry`) and `CompanySignals` (`linkedin_employee_count`, `estimated_revenue_band`) when present; enrichment is **not required** — a card with no linked company, or an unenriched company, still exports with those columns blank.
- **09 — Bulk select/parse/enrich**: export reuses the `/upload` page's existing `useCardSelection` checkbox UI exactly as its Parse/Enrich/Score/Delete buttons do — no new selection mechanism.
- **10 — Lead scoring**: export includes `lead_score`; a card with `lead_score == null` (never scored) still exports with that column blank.

Step 08 (delete card) is unrelated and not required.

## API endpoints (apps/api)

- `POST /cards/export` — export a seller-selected set of cards as CSV — org-authenticated (owner or org-admin visibility via `scope_to_visible_users`) — request `CardExportRequest {card_ids: list[UUID], min_length=1, max_length=200}` (same cap as `CardEnrichRequest`/`CardScoreRequest`, for the same reason: a caller-picked selection can never legitimately exceed the largest batch that could have been uploaded). Response: `200`, `Content-Type: text/csv`, `Content-Disposition: attachment; filename="dashr-leads-<YYYY-MM-DD>.csv"`, body is the CSV content. Card ids that aren't visible to the current user are silently omitted from the output (mirrors `POST /cards/enrich-companies`/`POST /cards/score`'s best-effort batch semantics) rather than raising — a partial export of what the caller is allowed to see is more useful than failing the whole request over one stale/foreign id. If every requested id is invisible or the resulting row set is empty, still returns `200` with a header-only CSV (not a 404) — an empty result isn't an error condition for a batch endpoint.
- **Status exclusion**: a visible card whose `status` is `failed`, `duplicate`, `processing`, or `merged` is also silently skipped, the same way an invisible id is — it never gets a row in the output CSV, and never counts toward or blocks the response (a selection made up entirely of excluded-status cards still returns `200` with a header-only CSV, exactly like an all-invisible selection). Rationale: a `failed` card has no usable extracted data; a `duplicate` or `merged` card's data was already folded into whichever card it matched, so its own row would just be confusing, near-empty noise; a `processing` card hasn't finished extraction yet, so there's nothing meaningful to export. Only `new` and `extracted` cards ever get a row — `new` because a seller may want a "cards captured so far" export before running Parse, `extracted` being the normal post-parse state.

No other endpoints change.

## Frontend surface (apps/web)

- **Modified: `apps/web/app/upload/page.tsx`** — add an "Export" button to the bulk-action row (alongside Parse/Enrich/Score/Delete), enabled whenever `selectedCardIds.size > 0` (the button itself imposes no client-side status/score eligibility filter — unlike Parse/Enrich/Score, any selected card can be included in the request; the `failed`/`duplicate`/`processing`/`merged` exclusion described above is enforced server-side in `export_cards`, not by disabling/filtering the selection client-side). Calls `exportCards([...selectedCardIds])`, which triggers a browser file download directly (no page state changes afterward — selection is left as-is, unlike the Parse/Enrich/Score handlers, since exporting doesn't mutate any card or its status). Grouped in its own bordered section after Delete, with its own `isExporting`/`exportError` state and error banner, following the same pattern as the page's other bulk actions.
- **Not modified: `apps/web/app/dashboard/page.tsx`** — the Leads page keeps only its existing "Score" action; it does not get an export CTA. (An earlier pass of this spec built export here; it has since been relocated to `/upload`.)
- **Modified: `apps/web/lib/api.ts`** — add `exportCards(cardIds: string[]): Promise<void>`. Not routed through the generic `request()` helper (that assumes a JSON response) — mirrors `deleteCard`'s existing pattern of a dedicated raw `fetch` call for a response shape the generic helper doesn't cover. Reads the response as a `Blob`, creates an object URL, and triggers a download via a temporary anchor element's `.click()`, then revokes the object URL. On a non-2xx response, parses the JSON error body and throws `ApiError`, same as every other client function.

No new pages or components.

## Database changes
No database changes. Export only reads existing columns on `VisitingCard`, `Company`, `CompanySignals`, `CardEmail`, `CardPhone`, and `Exhibition` — nothing new is stored.

## Background jobs
No background job changes. Export is synchronous in the request handler — deliberately not a Celery task, unlike bulk card processing/enrichment/scoring: the request is capped at 200 card ids (same cap as the other bulk endpoints) and does only read queries plus in-memory CSV formatting, no external I/O, no per-row network calls, so it stays well within a normal request's latency budget. This is a scoped exception to CLAUDE.md's "bulk/long-running work is a Celery task" rule — the rule targets slow, I/O-bound, or unbounded work, and a capped, read-only, synchronous formatting step is neither.

## Files to change
- `apps/api/app/routers/cards.py` — add `POST /cards/export`
- `apps/api/app/schemas/cards.py` — add `CardExportRequest`
- `apps/api/app/services/card_service.py` — add `export_cards(db, current_user, card_ids) -> list[dict]`, querying visible cards (LEFT JOIN `Company`, `CompanySignals`, `Exhibition`; separate queries for each card's `CardEmail`/`CardPhone` rows, mirroring `get_card_detail`'s existing pattern) and assembling one row dict per card, silently skipping ids that aren't visible and cards whose `status` is `failed`, `duplicate`, `processing`, or `merged`
- `apps/web/lib/api.ts` — add `exportCards(cardIds: string[]): Promise<void>`
- `apps/web/app/upload/page.tsx` — add "Export" button wired to the existing selection state

## Files to create
- `apps/api/app/services/export_service.py` — `build_csv(rows: list[dict]) -> str`, a pure function (no DB, no session — mirrors `scoring.py`'s separation of pure computation from the DB-touching service/router layer) that takes the row dicts assembled by `card_service.export_cards` and writes them via the stdlib `csv` module into a `io.StringIO`, returning the resulting text. Column order and headers defined as a module-level constant list, not inline, so a future column addition is a one-line change. v1 columns: `Full Name, Job Title, Company, Industry, Employee Count, Revenue Band, Primary Email, All Emails, Primary Phone, All Phones, Website, Address, GST Number, Products Offered, Designation Level, Lead Score, Special Remark, Exhibition, Status, Scanned On`. `All Emails`/`All Phones` are `; `-joined (a card can have more than one, per `CardEmail`/`CardPhone`); `Primary Email`/`Primary Phone` are the `is_primary` row, or the first row if none is flagged primary, or blank if none exist.

## New dependencies
No new dependencies — CSV formatting uses Python's stdlib `csv` module.

## Rules for implementation
- Every query on `visiting_cards` filters through the existing `scope_to_visible_users` helper against `VisitingCard.user_id` — do not add a new `org_id` column anywhere
- No raw SQL string interpolation — SQLAlchemy query builder or bound params only
- `export_service.build_csv` is a pure function — no DB writes, no Celery/session imports; only `card_service.export_cards` and the router touch the database, mirroring `scoring.py`'s existing pure-computation-vs-DB-layer split
- Business logic (row assembly, CSV formatting) lives in `services/`, not in `routers/cards.py` — the router only calls `card_service.export_cards`, passes the result to `export_service.build_csv`, and wraps it in a `Response`
- `POST /cards/export` never mutates any row — it is a read-only endpoint; no `status`, `lead_score`, or any other card field changes as a side effect of exporting
- API contracts are the Pydantic models (`CardExportRequest`) — the `exportCards` TS function in `apps/web/lib/api.ts` is hand-aligned to match, never assumed
- The 200-id cap on `CardExportRequest.card_ids` is not arbitrary — it bounds the synchronous, in-request work described in "Background jobs" above; don't remove or silently raise it without re-evaluating whether export should become a Celery task instead

## Definition of done
- [ ] `POST /cards/export` with a selection of visible card ids returns `200`, `Content-Type: text/csv`, and a `Content-Disposition: attachment` header with a `dashr-leads-<date>.csv` filename
- [ ] The returned CSV has one header row plus one row per requested card, in the column order specified above, with values matching each card's current `full_name`/`job_title`/`lead_score`/etc. and its linked `Company`/`CompanySignals`/`Exhibition` data
- [ ] A card with no linked company exports with `Company`/`Industry`/`Employee Count`/`Revenue Band` blank, not an error
- [ ] A card with `lead_score == null` (never scored) exports with `Lead Score` blank, not an error or a `0`
- [ ] A card with two emails (one `is_primary`) exports the primary one under `Primary Email` and both under `All Emails`, `; `-joined
- [ ] A `card_ids` list containing an id not visible to the current user (wrong owner, different org) is silently omitted from the CSV — the response is still `200`, not `404`/`403`, and contains rows only for the visible ids
- [ ] A `card_ids` list where none of the ids are visible to the current user returns `200` with a header-only CSV (no rows), not an error
- [ ] A visible card with `status == "failed"` is silently omitted from the CSV — no row, no error, `200`
- [ ] A visible card with `status == "duplicate"` is silently omitted from the CSV — no row, no error, `200`
- [ ] A visible card with `status == "processing"` is silently omitted from the CSV — no row, no error, `200`
- [ ] A visible card with `status == "merged"` is silently omitted from the CSV — no row, no error, `200`
- [ ] A `card_ids` selection made up entirely of excluded-status (`failed`/`duplicate`/`processing`/`merged`) cards returns `200` with a header-only CSV, same as an all-invisible selection
- [ ] A visible card with status `new` or `extracted` still exports normally — the exclusion is scoped to `failed`/`duplicate`/`processing`/`merged` only
- [ ] `apps/web/app/upload/page.tsx`'s "Export" button is disabled with no selection, enabled with any selection regardless of card status/score, and triggers a real file download in the browser when clicked
- [ ] No query against `visiting_cards` in the new code paths omits the `scope_to_visible_users` scoping
