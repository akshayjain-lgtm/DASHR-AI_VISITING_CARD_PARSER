# Spec: Bulk Delete Cards

## Overview
This step extends single-card delete (step 08) with a bulk counterpart. A seller triaging a large batch of scanned cards on the dashboard often needs to remove several at once (duplicates, mis-scans, cards from the wrong exhibition) rather than deleting one at a time. This adds a `POST /cards/bulk-delete` endpoint and a "Delete Selected" action next to the dashboard's existing "Score Selected"/"Export CSV" buttons, reusing the same bulk-select checkbox UI from step 09.

## Depends on
- **04 — Visiting card bulk upload**: cards must exist (`visiting_cards`).
- **08 — Delete card**: reuses `CardHasMergedChildrenError`/`CardStateChangedError` and the same cascade-confirmation contract, storage cleanup, and FK-safe delete ordering (children before parents) established there.
- **09 — Bulk select/parse/enrich**: reuses the dashboard's existing `useCardSelection` checkbox UI exactly as "Score Selected"/"Export CSV" do — no new selection mechanism.

Steps 05–07 (parsing/enrichment) and 10–11 (scoring/export) are unrelated and not required.

## API endpoints (apps/api)

- `POST /cards/bulk-delete` — permanently delete a seller-selected set of cards — org-authenticated (owner or org-admin visibility via `scope_to_visible_users`) — request `CardBulkDeleteRequest {card_ids: list[UUID], min_length=1, max_length=200, confirm_cascade: bool = false}` (same cap as `CardEnrichRequest`/`CardScoreRequest`/`CardExportRequest`). Response `CardBulkDeleteResponse {deleted_count: int, skipped_count: int}`. `card_ids` not visible to the caller are silently skipped and counted in `skipped_count` (mirrors `POST /cards/enrich-companies`/`POST /cards/score`'s best-effort batch semantics) rather than failing the whole request. If any selected card has merged/duplicate children (`merged_into_card_id` pointing at it) that are *not themselves part of the selection*, and `confirm_cascade` is `false`, returns `409` with `{message, child_count}` (the aggregate extra-child count across the whole batch) and deletes nothing; the caller resends the same request with `confirm_cascade=true` to proceed. A `409` can also occur if a concurrent request changes card state between the lookup and the commit (`CardStateChangedError`) — retryable.

No other endpoints change.

## Frontend surface (apps/web)

- **Modified: `apps/web/app/dashboard/page.tsx`** — add a "Delete Selected" button next to "Score Selected"/"Export CSV", enabled whenever `selectedCardIds.size > 0` (no status restriction — any selected card can be deleted). Clicking opens a confirmation dialog (`ConfirmDialog`, reused from single-card delete); confirming calls `bulkDeleteCards([...selectedCardIds])`. On success, clears the selection and refreshes the card list (unlike export, delete does mutate state).
- **Modified: `apps/web/lib/api.ts`** — add `bulkDeleteCards(cardIds: string[], confirmCascade = false): Promise<{deleted_count, skipped_count}>`. Not routed through the generic `request()` helper — mirrors `deleteCard`'s existing dedicated-`fetch` pattern for handling the overloaded `409` response (a `{child_count}` body means "cascade confirmation needed" and throws `CardHasMergedChildrenError`; any other non-2xx throws a generic `ApiError`).
- **Modified: `apps/web/lib/use-delete-card-confirm.ts`** — add `useBulkDeleteCardsConfirm`/`bulkDeleteConfirmCopy`, the bulk counterpart to the existing `useDeleteCardConfirm`/`deleteConfirmCopy`: same two-step confirm state machine (generic confirm, then a second cascade-specific confirm only on a `409` with `child_count`), but driving a whole `cardIds[]` selection instead of one `cardId`.

No new pages or components — `ConfirmDialog` is reused as-is.

## Database changes
No database changes. Bulk delete only removes existing `VisitingCard` rows (and cascades to `card_emails`/`card_phones` via existing `ON DELETE CASCADE`) and deletes existing storage objects — nothing new is stored.

## Background jobs
No background job changes. Bulk delete is synchronous in the request handler, same as single-card delete — deletion (including any merged-children cascade) is bounded by the 200-id request cap and does only DB writes plus best-effort storage cleanup, no external I/O per row beyond the storage delete calls.

## Files to change
- `apps/api/app/schemas/cards.py` — add `CardBulkDeleteRequest`, `CardBulkDeleteResponse`
- `apps/api/app/routers/cards.py` — add `POST /cards/bulk-delete`, mirroring `DELETE /cards/{card_id}`'s exception-to-HTTP-status mapping
- `apps/api/app/services/card_service.py` — add `bulk_delete_cards(db, current_user, card_ids, confirm_cascade) -> tuple[int, int]`
- `apps/web/lib/api.ts` — add `bulkDeleteCards`
- `apps/web/lib/use-delete-card-confirm.ts` — add `useBulkDeleteCardsConfirm`/`bulkDeleteConfirmCopy`
- `apps/web/app/dashboard/page.tsx` — add "Delete Selected" button wired to the existing selection state

## Files to create
None — this step only extends existing files.

## New dependencies
No new dependencies.

## Rules for implementation
- Every query against `visiting_cards` filters through the existing `scope_to_visible_users` helper against `VisitingCard.user_id`
- No raw SQL string interpolation — SQLAlchemy query builder or bound params only
- Merged-children lookup for the cascade check is scoped by `merged_into_card_id` alone, **not** by the deleting user's own visibility — same reasoning as single-card `delete_card`: a child's authorization derives from having merged into an already-authorized card, not from sharing the deleter's `user_id`, since a duplicate/back-of-card match can span owners within the same org
- Children (rows with `merged_into_card_id` set) are deleted and flushed before parents/standalones — `merged_into_card_id` is a self-referencing FK with no `ON DELETE` rule, so a parent can't be removed first while a child still points at it
- Business logic (visibility scoping, cascade aggregation, delete ordering, storage cleanup) lives in `card_service.bulk_delete_cards`, not in the router
- `card_ids` not visible to the caller are silently skipped (counted in `skipped_count`), never raise — this is a best-effort batch over a client-picked selection, same contract as `enqueue_enrichment`/`enqueue_scoring`/`export_cards`
- The 200-id cap on `CardBulkDeleteRequest.card_ids` matches every other bulk endpoint's cap and bounds the synchronous, in-request delete work

## Definition of done
- [ ] `POST /cards/bulk-delete` with a selection of visible card ids with no merged children deletes all of them, deletes their storage objects, and returns `{deleted_count, skipped_count: 0}`
- [ ] A `card_ids` list containing an id not visible to the current user is silently skipped — `skipped_count` reflects it, the response is still `200`, and the request is not failed outright
- [ ] If any selected card has a merged/duplicate child outside the selection and `confirm_cascade` is omitted/`false`, the response is `409` with the total extra-child count, and no card is deleted
- [ ] Resending the same request with `confirm_cascade=true` deletes both the originally selected cards and their extra merged children
- [ ] A child already included in the selection needs no cascade confirmation
- [ ] `apps/web/app/dashboard/page.tsx`'s "Delete Selected" button is disabled with no selection, shows a confirmation dialog before deleting, and clears the selection + refreshes the card list on success
- [ ] No query against `visiting_cards` in the new code paths omits the `scope_to_visible_users` scoping (except the intentionally-unscoped merged-children lookup, matching `delete_card`'s existing precedent)
