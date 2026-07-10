# Spec: Delete Card

## Overview

Sellers who scan a batch of cards at an exhibition inevitably capture a few they don't want kept — a blank/blurry shot, a misfire, or a card that should never have been included. This feature lets a user permanently remove a single `VisitingCard` (and any raw scans that were folded into it as a merged back-side or duplicate) from both the database and object storage. It's a housekeeping action on top of the existing capture → extraction flow (steps 04–07), not a new pipeline stage — it only ever operates on a card the user can already see.

## Depends on

- `04-visiting-card-bulk-upload` — defines `VisitingCard`, `card_emails`, `card_phones`, and the storage key convention this feature deletes
- `05-parsing-visiting-card` — defines the `merged`/`duplicate` status and `merged_into_card_id` folding relationship this feature must cascade through
- `06-company-profile-backend` / `07-data-enrichment` — not touched; deleting a card never deletes the shared `Company` row it's linked to

## API endpoints (apps/api)

- `DELETE /cards/{card_id}?confirm_cascade={bool}` — permanently deletes a card — org-authenticated (same visibility rule as `GET /cards/{card_id}`: owner, or org admin for any member's card) — no request body; `confirm_cascade` defaults to `false`; `204 No Content` on success

If the target card is a "parent" that other cards were merged into (i.e. other cards have `merged_into_card_id == card_id`), the deletion would cascade to those child cards. The endpoint never does that silently:

- If children exist and `confirm_cascade` is **not** `true`, the delete is rejected with `409` and nothing is deleted — the caller must re-issue the request with `confirm_cascade=true` after the user has explicitly confirmed the cascade.
- If children exist and `confirm_cascade=true`, the parent and all its merged children are deleted together, atomically.
- If there are no children, `confirm_cascade` is ignored and the card is deleted immediately.

Errors:
- `404` — card doesn't exist or isn't visible to the caller (`CardNotFoundError`, same as other card endpoints)
- `409` — card has one or more merged/duplicate children and `confirm_cascade` was not passed as `true` (`CardHasMergedChildrenError`); response body is `{"detail": {"message": "...", "child_count": <int>}}` so the frontend can name the count in its confirmation prompt without a second round-trip

## Frontend surface (apps/web)

Both delete entry points share the same two-step confirmation flow:

1. **Generic confirm** — before calling the API at all: "Delete this card? This can't be undone."
2. **Cascade confirm** — only shown if the first `DELETE` call comes back `409`: a second, distinct pop-up naming the child count from the response body (e.g. "This card has 2 merged/duplicate scans folded into it. Deleting it will also delete those. Continue?"). Only if the user confirms this second prompt does the frontend re-issue the request with `confirm_cascade=true`. Declining either prompt aborts with no request sent (or no further request, for the second prompt).

- **Modified: `apps/web/components/card-detail-drawer.tsx`** — add a "Delete Card" button (styled as a destructive action, distinct from the existing `OBtn`/`GBtn` treatments used for Retry/Enrich Company) that drives the two-step confirm flow above. On final success, closes the drawer and calls `onChanged?.()` so the parent list refreshes.
- **Modified: `apps/web/app/upload/page.tsx`** — add a row-level delete icon button (trash icon, `lucide-react`) on each row of the card list, driving the same two-step confirm flow. Clicking it must call `e.stopPropagation()` so it doesn't also open the drawer. On final success, calls `refreshCards()`.
- **No changes to `apps/web/app/dashboard/page.tsx`** — it currently renders hardcoded mock `LEADS` data, not real cards from the API; wiring it to live data is out of scope for this spec.

## Database changes

No new tables, columns, or constraints.

Verified against `apps/api/app/models/visiting_card.py`, `card_email.py`, `card_phone.py`:
- `card_emails.card_id` and `card_phones.card_id` already have `ondelete="CASCADE"` — deleting a `visiting_cards` row automatically removes its emails/phones, no application-level cleanup needed.
- `visiting_cards.merged_into_card_id` is a self-referencing FK with no `ondelete` (defaults to `NO ACTION`). Deleting a parent card while child rows still reference it via `merged_into_card_id` would raise a FK violation — so the service must delete child rows (see below) *before* deleting the parent, in the same transaction, rather than relying on the DB to cascade.
- `visiting_cards.company_id` is left untouched — `Company` rows are shared/cached across orgs per `CLAUDE.md` and must never be deleted as a side effect of deleting one card that references them.

## Background jobs

No new or changed Celery tasks. Deletion is synchronous and fast (a couple of row deletes plus best-effort S3 cleanup), matching the existing pattern for other single-card actions (`reprocess`, `enrich-company`) which are also synchronous request handlers, not queued.

## Files to change

- `apps/api/app/routers/cards.py` — add `DELETE /{card_id}` route, accepting `confirm_cascade` as a query param and mapping `CardHasMergedChildrenError` to `409`
- `apps/api/app/services/card_service.py` — add `delete_card(db, current_user, card_id, confirm_cascade)`
- `apps/api/app/services/exceptions.py` — add `CardHasMergedChildrenError`, carrying a `child_count: int` attribute
- `apps/web/lib/api.ts` — add `deleteCard(cardId: string, confirmCascade?: boolean): Promise<void>` and a `CardHasMergedChildrenError` class (holding `childCount`) that it throws on a `409` response, so callers can distinguish "needs cascade confirmation" from a generic failure without parsing response bodies themselves
- `apps/web/components/card-detail-drawer.tsx` — add delete button + two-step confirm + wired handler
- `apps/web/app/upload/page.tsx` — add row-level delete action + two-step confirm + wired handler

## Files to create

None.

## New dependencies

No new dependencies.

## Rules for implementation

- Every query filters through the existing `get_visible_card` / `scope_to_visible_users` helpers in `card_service.py` — never fetch a card (or its merge-children) by raw `card_id` without the visibility scope, or an admin/member from one org could delete another org's card.
- Reuse the existing `CardNotFoundError` exception (already mapped to `404` in the router) for the not-found case; add `CardHasMergedChildrenError` for the cascade-confirmation case — don't overload one exception for both.
- Delete order within `delete_card`: resolve the parent via `get_visible_card` (raises `CardNotFoundError` if missing/not visible) → find child cards where `merged_into_card_id == card_id` (also visibility-scoped) → if children exist and `confirm_cascade` is not `True`, raise `CardHasMergedChildrenError(child_count=len(children))` *before* deleting anything → otherwise delete child rows, then the parent row, in the same DB transaction → commit → only after commit, best-effort delete each deleted card's S3 object via the existing `storage_service.delete_file` (already non-raising, so a storage hiccup never leaves the DB and S3 inconsistent in a way that surfaces as a 500 to the user).
- The `child_count` check must happen server-side against a fresh query every call — never trust a count the frontend remembers from an earlier response, since another request could change it in between.
- Do not touch `Company`/`CompanySignals` rows — they are shared reference data, never owned by a single card.
- Business logic (visibility check, cascade decision, storage cleanup) lives entirely in `card_service.py`; the router stays a thin try/except → HTTP status mapping (`CardNotFoundError` → 404, `CardHasMergedChildrenError` → 409 with `child_count` in the body), consistent with every other endpoint in `cards.py`.
- No raw SQL — use the SQLAlchemy query builder, consistent with the rest of `card_service.py`.
- Frontend delete actions always require the first generic confirm, and — only when the API responds `409` — a second, visually distinct confirm naming the child count, before retrying with `confirm_cascade=true`. Never skip straight to `confirm_cascade=true` on the first attempt; the count must come from the server's response, not be guessed or omitted client-side.
- The row-level delete button in `upload/page.tsx` must stop click propagation so it doesn't also trigger the row's `onClick` (which opens `CardDetailDrawer`).

## Definition of done

- `DELETE /cards/{card_id}` on a card owned by the caller, with no merged children, returns `204` and the row (plus its `card_emails`/`card_phones`) is gone from the database.
- `DELETE /cards/{card_id}` on a card that has a merged back-side/duplicate child, called *without* `confirm_cascade`, returns `409` with `child_count` matching the actual number of children, and neither the parent nor any child row is deleted.
- `DELETE /cards/{card_id}?confirm_cascade=true` on that same card returns `204`, and both the parent and every child card row are gone, with no FK violation.
- `DELETE /cards/{card_id}` on a card the caller cannot see (belongs to a different org, or a non-admin requesting another member's card) returns `404`, and the card is untouched.
- `DELETE /cards/{card_id}` for a nonexistent `card_id` returns `404`.
- After a successful delete, every deleted card's image is no longer retrievable from the configured S3/MinIO bucket at its stored key.
- In the browser: deleting a childless card (from the drawer or the row-level icon) shows exactly one confirm prompt, then the card disappears from the list without a manual refresh.
- In the browser: deleting a card with merged children shows the generic confirm, then — after the `409` — a second, distinct prompt nami/ng the child count; declining either prompt leaves the card and its children untouched; confirming both deletes all of them.

- The row-level trash icon on the upload page never opens the detail drawer when clicked.
- An org admin can delete a card belonging to another member of their org; a non-admin member cannot delete another user's card (verified via a 404, not a 403, matching the existing visibility convention).
