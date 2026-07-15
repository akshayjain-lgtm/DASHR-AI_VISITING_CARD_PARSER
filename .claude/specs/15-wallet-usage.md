# Spec: Wallet Usage

## Overview
This feature wires the "Billing" primitives from Step 14 (Wallet Recharge) into the actual work they're meant to pay for. It is the "Wallet usage" workflow stage in CLAUDE.md's core workflows list — distinct from "Billing" (recharge + invoicing) and coming after it. Today, `POST /cards/process`, `/cards/enrich-companies`, `/cards/score`, and their single-card counterparts (`/cards/{id}/reprocess`, `/cards/{id}/enrich-company`, `/cards/{id}/score`) enqueue Celery work completely free of charge — `billing.debit_wallet` exists but nothing calls it. This spec adds a per-user, per-action-type free allowance (20 free parses, 20 free enrichments, 20 free scorings, tracked independently), and once that allowance for an action type is exhausted, gates every further action of that type on a race-safe wallet balance check — blocking the action outright (never running OCR/enrichment/scoring, never enqueuing the Celery task) at a zero or insufficient balance.

## Depends on
- Step 14 (Wallet Recharge) — this spec extends `billing.py`, `Wallet`, `WalletTransaction`, and `PricingRate`, all built there. It only adds new functions/columns; nothing from Step 14 is removed or renamed.
- Step 05 (Parsing Visiting Card), Step 07 (Data Enrichment), Step 10 (Lead Scoring) — this spec wires charging into the endpoints those steps built (`card_service.reprocess_card`/`enrich_company_now`/`score_card_now`/`enqueue_processing`/`enqueue_enrichment`/`enqueue_scoring`); it does not change their eligibility rules, only adds a charge step ahead of enqueue.

## API endpoints (apps/api)

No new endpoints. Existing endpoints change response shape and/or gain a new error status:

- `POST /cards/process` — response gains `wallet_blocked_count: int` alongside the existing `enqueued_count`. Cards matched but blocked by a zero/insufficient balance are no longer enqueued and are counted here instead.
- `POST /cards/enrich-companies` — response gains `wallet_blocked_count: int` alongside existing `enqueued_count`/`skipped_count` (a distinct bucket from `skipped_count`, which remains ineligibility-only: no linked company, company not pending, duplicate company in the same batch).
- `POST /cards/score` — same shape change as `/cards/enrich-companies`.
- `POST /cards/{card_id}/reprocess` — now returns `402 Payment Required` (`detail: str`) if the user's free parse allowance is exhausted and their wallet balance can't cover the parse rate. Card status is left unchanged (still `"failed"`) — a blocked reprocess must not flip it to `"new"`.
- `POST /cards/{card_id}/enrich-company` — now returns `402 Payment Required` under the same condition for the enrichment rate.
- `POST /cards/{card_id}/score` — now returns `402 Payment Required` under the same condition for the scoring rate.
- `GET /wallet` — response gains `free_actions_remaining: {parse: int, enrichment: int, scoring: int}`, each `max(free_limit - used_count, 0)` for that action type.

## Frontend surface (apps/web)

- **Modified**: `apps/web/lib/api.ts` — extend `WalletOut` with `free_actions_remaining: { parse: number; enrichment: number; scoring: number }`; extend the inline response types of `processCards`/`enrichCompanies`/`scoreCards` with `wallet_blocked_count: number`.
- **Modified**: `apps/web/app/upload/page.tsx` —
  - Fetch `getWallet()` alongside the existing card list refresh and show a small balance + free-actions-remaining indicator near the Parse/Enrich/Score button row (same header area as the existing button group, ~line 691-712).
  - When a bulk `processCards`/`enrichCompanies`/`scoreCards` response has `wallet_blocked_count > 0`, show a banner reusing the existing status-banner pattern (~line 615-668) — e.g. "N card(s) were not parsed — wallet balance too low. Recharge to continue." with a link to `/wallet`.
  - When a per-row action (`handleRowEnrich`, single reprocess/score) throws `ApiError` with `status === 402`, show the same "wallet balance too low" messaging inline instead of the generic error path.
- No new pages — the existing `/wallet` page (Step 14) already renders `TRANSACTION_TYPE_LABEL` entries for `parse_debit`/`enrichment_debit`/`scoring_debit`, so real debit rows from this feature render correctly with no further wallet-page changes needed. Optionally show `free_actions_remaining` there too, next to the balance.

## Database changes

One new Alembic migration `0011_wallet_usage.py` (revises `0010_wallet_billing`):

- **Add column** `pricing_rates.free_limit` (Integer, not null, `server_default="20"`) — the free-action cap for that action type, versioned alongside `rate_inr` on the same row (a rate change and a free-limit change both become a new `pricing_rates` row via the existing `effective_from` mechanism; no separate config table needed). Backfills existing 3 seeded rows (parse/enrichment/scoring) to `free_limit=20`.

- **New table `free_action_allowances`** — one row per `(user_id, action_type)`, User-scoped like `wallets` (no `org_id` — matches CLAUDE.md's explicit carve-out for Wallet/WalletTransaction):
  - `free_action_allowance_id` (UUID, PK, `gen_random_uuid()`)
  - `user_id` (UUID, FK → `users.user_id`, not null)
  - `action_type` (String, not null — `"parse"` | `"enrichment"` | `"scoring"`)
  - `used_count` (Integer, not null, `server_default="0"`) — lifetime count of actions of this type by this user, free or paid; only ever incremented, never reset or decremented
  - `created_at` (TIMESTAMPTZ, not null, `server_default=now()`)
  - `updated_at` (TIMESTAMPTZ, not null, `server_default=now()`, `onupdate=now()`)
  - Unique index on `(user_id, action_type)` — one counter per user per action type, created lazily on first use (mirrors `wallets`' one-row-per-user pattern).

New model file `apps/api/app/models/free_action_allowance.py` (`FreeActionAllowance`), registered in `apps/api/app/db/base.py`'s metadata import list. `PricingRate` model (`apps/api/app/models/pricing_rate.py`) gains the `free_limit: Mapped[int]` column.

## Background jobs

No new Celery tasks and no changes to the three existing ones (`process_card`, `enrich_company_task`, `score_card_task`) — the charge happens synchronously ahead of `.delay()`, at the same point CLAUDE.md requires the balance check to happen ("ahead of the OCR/enrichment/scoring call, not after"). A task that's already been enqueued has, by construction, already been charged (free or wallet-debited); this spec does not add any refund-on-task-failure logic — a `process_card`/`enrich_company_task`/`score_card_task` run that ends in `"failed"` is not refunded, consistent with CLAUDE.md's "no self-serve withdrawal/refund flow" and prepaid-spend-only model. (If refunding failed extractions becomes a requirement later, that's a separate future spec, not an implicit part of this one.)

## Files to change

- `apps/api/app/models/pricing_rate.py` — add `free_limit: Mapped[int]` column
- `apps/api/app/db/base.py` — import `FreeActionAllowance` so Alembic autogenerate/metadata sees it
- `apps/api/app/services/billing.py` — add `get_free_limit`, `get_free_actions_remaining`, `charge_for_action` (see below)
- `apps/api/app/services/card_service.py` — wire `billing.charge_for_action` into `reprocess_card`, `enrich_company_now`, `score_card_now`, `enqueue_processing`, `enqueue_enrichment`, `enqueue_scoring`; change `enqueue_processing`'s return type from `int` to `tuple[int, int]` (enqueued, wallet_blocked), and `enqueue_enrichment`/`enqueue_scoring`'s from `tuple[int, int]` to `tuple[int, int, int]` (enqueued, skipped, wallet_blocked)
- `apps/api/app/routers/cards.py` — import `InsufficientBalanceError`; catch it in `reprocess_card`/`enrich_company`/`score_card` → `HTTPException(402, ...)`; update `process_cards`/`enrich_companies`/`score_cards` to unpack the new tuple shapes and populate `wallet_blocked_count`
- `apps/api/app/schemas/cards.py` — add `wallet_blocked_count: int` to `CardProcessResponse`, `CardEnrichResponse`, `CardScoreResponse`
- `apps/api/app/schemas/wallet.py` — add `free_actions_remaining: dict[str, int]` to `WalletOut`
- `apps/api/app/routers/wallet.py` — populate `free_actions_remaining` in the `GET /wallet` handler via `billing.get_free_actions_remaining`
- `apps/web/lib/api.ts` — extend `WalletOut` and the `processCards`/`enrichCompanies`/`scoreCards` return types
- `apps/web/app/upload/page.tsx` — wallet balance/free-actions indicator, wallet-blocked banner, 402 handling on per-row actions

## Files to create

- `apps/api/migrations/versions/0011_wallet_usage.py`
- `apps/api/app/models/free_action_allowance.py`
- `apps/api/tests/test_15_wallet_usage.py`
- `apps/web/__tests__/15-wallet-usage.test.tsx`

## New dependencies

No new dependencies.

## Rules for implementation

- Every query on `free_action_allowances` filters by `user_id` (not `org_id` — User-scoped like `wallets`, per CLAUDE.md)
- No raw SQL string interpolation — SQLAlchemy query builder or bound params only
- Business logic for charging lives in `services/billing.py` (`charge_for_action`) and is called from `services/card_service.py`; `routers/cards.py` only maps `InsufficientBalanceError` to a 402 — no billing logic in the router
- `charge_for_action(db, user_id, action_type, reference_id=None) -> bool` (returns `True` if billed from the wallet, `False` if covered by the free allowance) is the **only** entry point card_service calls for this — it must, in one sequence sharing a single row lock per resource:
  1. Lock (or lazily create) the `(user_id, action_type)` `FreeActionAllowance` row (`SELECT ... FOR UPDATE`)
  2. Read the currently-effective `PricingRate` row for `action_type` (rate + `free_limit`), same lookup pattern as `get_current_rate`
  3. If `used_count < free_limit`: increment `used_count`, commit, return `False` — no wallet touched, no `WalletTransaction` row (free actions aren't ledger events, since no money moves)
  4. Else: lock the wallet row, and if `balance_inr < rate_inr`, raise `InsufficientBalanceError` **without incrementing `used_count`** (the action never happened — nothing to count); otherwise debit the wallet, write the `WalletTransaction` row (`transaction_type=f"{action_type}_debit"`, `reference_id=<card_id>`), increment `used_count`, and commit **all of it — allowance increment, balance decrement, and ledger insert — in a single transaction**. Do not call the existing `debit_wallet` as a separately-committed step from here: it commits internally, and if the allowance increment failed afterward, the wallet would be debited with no matching allowance update. Inline the equivalent of `debit_wallet`'s body instead, sharing `_lock_or_create_wallet`, so the whole charge is one atomic commit.
- `charge_for_action` must run, and either succeed or raise, **before** any Celery `.delay()` call for that action, and before any DB mutation to the card's own state that a subsequent failure couldn't cleanly undo (e.g. `reprocess_card` must call `charge_for_action` before flipping `card.status` back to `"new"`, not after — a blocked reprocess must leave the card exactly as it was, still `"failed"` and still eligible for a real retry once funded)
- In `enqueue_processing`/`enqueue_enrichment`/`enqueue_scoring`, `InsufficientBalanceError` from `charge_for_action` is caught per-card-id inside the existing loop and counted in `wallet_blocked_count`, not raised — these are best-effort batch endpoints over a client-picked selection (matching the existing `skipped_count` convention), so one blocked card must not abort the rest of the batch (other cards may still be free-tier-eligible or already covered)
- In the three single-card endpoints (`reprocess_card`, `enrich_company_now`, `score_card_now`), `InsufficientBalanceError` propagates up to the router uncaught by the service, and `routers/cards.py` maps it to `402 Payment Required`
- `PricingRate.free_limit`, like `rate_inr`, is read through `billing.py`, never hardcoded — same "configurable data, not a branch" rule CLAUDE.md applies to scoring weights and per-action prices
- `FreeActionAllowance.used_count` increments on every parse/enrich/score by that user, whether free or wallet-debited (CLAUDE.md) — never reset, never decremented, and never shared across action types (a `(user_id, action_type)` pair is its own independent counter)
- API contracts are Pydantic models in `apps/api/app/schemas/`; TS types in `apps/web/lib/api.ts` are hand-written to match (no codegen step in this repo yet, same as every existing type in `api.ts`)

## Definition of done

- [ ] `alembic upgrade head` adds `pricing_rates.free_limit` (backfilled to 20 on the three seeded rows) and creates `free_action_allowances`
- [ ] A user with a fresh (never-used) `FreeActionAllowance` for `"parse"` can call `POST /cards/{id}/reprocess` (or get enqueued via bulk `/cards/process`) with a `0` wallet balance and it succeeds, incrementing `used_count` to 1 and writing no `WalletTransaction`
- [ ] After 20 parses by the same user, the 21st parse at a `0` or insufficient wallet balance is blocked: `POST /cards/{id}/reprocess` returns `402` and leaves `card.status`/`wallets.balance_inr`/`free_action_allowances.used_count` all unchanged; the bulk `POST /cards/process` equivalent enqueues nothing for that card and reports it in `wallet_blocked_count`, not `enqueued_count`
- [ ] After 20 parses, the 21st parse with a wallet balance ≥ the current parse rate succeeds, debits the wallet by exactly the rate, writes one `WalletTransaction` (`transaction_type="parse_debit"`, `reference_id=<card_id>`, correct `balance_after_inr`), and increments `used_count` to 21 — all in a way that a mid-sequence failure can't leave the wallet debited without the allowance incremented (or vice versa)
- [ ] Exhausting the free parse allowance has no effect on the enrichment or scoring allowances for the same user (independent counters) — verified by exhausting one and confirming the other two still start free
- [ ] `GET /wallet` returns `free_actions_remaining` reflecting `used_count`/`free_limit` for all three action types correctly, including `0` (not negative) once exhausted
- [ ] Concurrent calls to `charge_for_action` for the same user/action_type (simulated with two threads/sessions against the same row) never allow the wallet to go negative or the allowance to double-count — the row lock serializes them
- [ ] `apps/web/app/upload/page.tsx` shows a wallet-blocked banner when a bulk parse/enrich/score response has `wallet_blocked_count > 0`, and shows the same messaging for a per-row action that receives a 402
- [ ] `pytest apps/api/tests/test_15_wallet_usage.py` passes
- [ ] Frontend vitest suite (`apps/web/__tests__/15-wallet-usage.test.tsx`) passes
- [ ] Full existing suite (`test_09_bulk_select_parse_enrich.py`, `test_10_lead_scoring.py`, `test_14_wallet_recharge.py`, etc.) still passes unchanged — this feature must not alter eligibility rules, only add a charge gate ahead of already-eligible actions
