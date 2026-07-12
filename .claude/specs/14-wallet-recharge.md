# Spec: Wallet Recharge

## Overview
This feature introduces DASHR AI's billing primitive: a prepaid INR wallet per User, funded via Razorpay. It is the first piece of the "Billing" workflow stage (card capture → extraction → enrichment → scoring → review/export → **billing**). It lands the Wallet/WalletTransaction/PricingRate data model, a race-safe `billing.py` service with credit and debit primitives, Razorpay order creation + signature-verified webhook confirmation for recharges, read endpoints for balance and ledger history, and a Wallet page in the dashboard where a user can see their balance, recharge it, and review past transactions.

Wiring an actual balance check/debit into the existing card parse, enrichment, and scoring endpoints (`apps/api/app/routers/cards.py`) is **out of scope** for this spec — those actions remain free to trigger today. `billing.py`'s `debit_wallet` primitive is built race-safe and ready for that wiring, but connecting it to `card_service`/`enrichment_service`/`scoring_processing` is a separate future step, to avoid changing the behavior of three already-shipped endpoints in a spec whose scope is the recharge/funding flow.

## Depends on
- Step 02 (User Registration) and Step 03 (User Login/Logout) — wallets are scoped to an authenticated `User`, via `get_current_user`.
- No dependency on Steps 04-13 (card/enrichment/scoring pipeline) since this spec doesn't touch billable-action enforcement.

## API endpoints (apps/api)

- `GET /wallet` — returns the current user's cached balance plus the most recent transactions — org-authenticated (any role) — response: `WalletOut { balance: Decimal, currency: "INR", transactions: WalletTransactionOut[] (most recent 20) }`
- `GET /wallet/transactions` — paginated full ledger for the current user, newest first — org-authenticated — query params `limit` (default 50, max 200), `offset` (default 0) — response: `WalletTransactionOut[]`
- `POST /wallet/recharge` — creates a Razorpay Order for the requested INR amount against the current user's own wallet; does **not** credit anything yet — org-authenticated — request: `WalletRechargeRequest { amount_inr: Decimal (min 100, max 500000) }` — response: `WalletRechargeOut { razorpay_order_id: str, razorpay_key_id: str, amount_inr: Decimal, currency: "INR" }`
- `POST /payments/webhook/razorpay` — Razorpay webhook receiver; verifies the `X-Razorpay-Signature` header against the raw request body using the webhook secret, and only then credits the paying user's wallet for a `payment.captured` event tied to a known `razorpay_order_id` — **public** (no session cookie — Razorpay calls this server-to-server; auth is the signature, not a user session) — request: raw Razorpay webhook payload — response: `{status: "ok"}` (200) or 400 on bad signature/malformed payload

No other new endpoints. GST No./Billing Address on the profile and per-card/batch Invoice generation are explicitly out of scope — those belong to a later Invoicing spec, per CLAUDE.md's roadmap.

## Frontend surface (apps/web)

- **New page**: `apps/web/app/wallet/page.tsx` — shows current balance, a recharge form (amount input + "Add Money" button that opens Razorpay Checkout via the `razorpay-checkout` script), and a paginated transaction history table (type, amount, running context, timestamp). Loads via `getWallet()`/`listWalletTransactions()`.
- **New component**: `apps/web/components/razorpay-checkout-script.tsx` — loads Razorpay's `checkout.js` once (client component, `next/script`), used by the wallet page to open the Checkout modal after `POST /wallet/recharge` returns an order.
- **Modified**: `apps/web/components/sidebar.tsx` — add a `Wallet` nav entry (route `/wallet`, icon e.g. `lucide-react`'s `Wallet`) between "Company Profile" and "Settings".
- **Modified**: `apps/web/lib/api.ts` — add `WalletOut`, `WalletTransactionOut` types and `getWallet()`, `listWalletTransactions()`, `createWalletRecharge()` functions, following the existing `request()`/typed-function pattern.
- **Modified**: `apps/web/middleware.ts` — confirm `/wallet` is included in the authenticated-route matcher (mirrors `/dashboard`, `/upload`, `/profile`).

## Database changes

Three new tables, all created in a new Alembic migration `0010_wallet_billing.py` (revises `0009`):

- **`pricing_rates`** — global reference data, no `org_id`/`user_id` (prices apply platform-wide, not per-tenant, matching CLAUDE.md's "configurable data" rule for pricing):
  - `pricing_rate_id` (UUID, PK, `gen_random_uuid()`)
  - `action_type` (String, not null — `"parse"` | `"enrichment"` | `"scoring"`)
  - `rate_inr` (Numeric, not null)
  - `effective_from` (TIMESTAMPTZ, not null, `server_default=now()`)
  - `created_at` (TIMESTAMPTZ, not null, `server_default=now()`)
  - Index on `(action_type, effective_from)` to look up the currently-effective rate per action type.
  - Seeded via a data migration insert: parse=5, enrichment=3, scoring=2 (₹, matching CLAUDE.md's launch rates).

- **`wallets`** — one row per User; `Wallet.balance` is a cached/derived value, never written outside `billing.py`:
  - `wallet_id` (UUID, PK, `gen_random_uuid()`)
  - `user_id` (UUID, FK → `users.user_id`, not null, unique — one wallet per user)
  - `balance_inr` (Numeric, not null, `server_default=0`)
  - `created_at` (TIMESTAMPTZ, not null, `server_default=now()`)
  - `updated_at` (TIMESTAMPTZ, not null, `server_default=now()`, `onupdate=now()`)
  - No `org_id` — CLAUDE.md is explicit that Wallet is User-scoped, not Organization-scoped, even though every other table carries `org_id`. `user_id` is the sole tenancy/ownership key here.

- **`wallet_transactions`** — append-only ledger, never updated/deleted:
  - `wallet_transaction_id` (UUID, PK, `gen_random_uuid()`)
  - `user_id` (UUID, FK → `users.user_id`, not null) — denormalized alongside `wallet_id` for direct ledger queries without a join
  - `wallet_id` (UUID, FK → `wallets.wallet_id`, not null)
  - `transaction_type` (String, not null — `"recharge_credit"` | `"parse_debit"` | `"enrichment_debit"` | `"scoring_debit"` | `"adjustment"`)
  - `amount_inr` (Numeric, not null — positive for credits, negative for debits)
  - `balance_after_inr` (Numeric, not null — snapshot of `wallets.balance_inr` right after this entry, so the ledger is independently auditable/reconstructable per CLAUDE.md)
  - `razorpay_order_id` (String, nullable — set for `recharge_credit` rows)
  - `razorpay_payment_id` (String, nullable — set for `recharge_credit` rows once webhook-verified)
  - `reference_id` (UUID, nullable — e.g. `card_id` for debit rows once wired up in a future step)
  - `created_at` (TIMESTAMPTZ, not null, `server_default=now()`)
  - Index on `(user_id, created_at)` for the ledger-history query.
  - Unique index on `razorpay_order_id` where not null, to make webhook retries (Razorpay redelivers on non-2xx) idempotent — a retried webhook for an already-credited order must not double-credit.

All three models added under `apps/api/app/models/` (`pricing_rate.py`, `wallet.py`, `wallet_transaction.py`) and registered in `apps/api/app/db/base.py`'s metadata import list (matching the existing model registration pattern).

## Background jobs

No new Celery tasks. Razorpay webhook processing (signature verification + credit) is fast, synchronous DB work — same category as the existing `POST /cards/{id}/reprocess`-style single-row writes — and must respond quickly for Razorpay's webhook retry policy, so it stays in the request handler rather than being deferred to a worker.

## Files to change

- `apps/api/app/main.py` — register `wallet_router` and `payments_router`, add `PUT`→ no change needed but confirm `POST` already allowed (it is)
- `apps/api/app/db/base.py` — import new models so Alembic autogenerate/metadata sees them
- `apps/api/app/core/config.py` — add `razorpay_key_id`, `razorpay_key_secret`, `razorpay_webhook_secret` settings
- `apps/api/.env.example` — document the three new Razorpay env vars
- `apps/api/requirements.txt` — add `razorpay` SDK
- `apps/web/lib/api.ts` — add wallet types/functions
- `apps/web/components/sidebar.tsx` — add Wallet nav entry
- `apps/web/middleware.ts` — add `/wallet` to the protected-route matcher

**Implementation note (post-review):** `apps/web/.env.example` was intentionally *not* changed to add `NEXT_PUBLIC_RAZORPAY_KEY_ID`, despite this being called for above. `WalletRechargeOut` already returns `razorpay_key_id` per-order from `POST /wallet/recharge`, and the frontend consumes it directly from that response in `wallet/page.tsx` — Checkout still gets the public key ID client-side, just without a second, duplicated copy of it living in a frontend env var that could drift from the backend's actual configured key.

## Files to create

- `apps/api/migrations/versions/0010_wallet_billing.py`
- `apps/api/app/models/pricing_rate.py`
- `apps/api/app/models/wallet.py`
- `apps/api/app/models/wallet_transaction.py`
- `apps/api/app/schemas/wallet.py` (`WalletOut`, `WalletTransactionOut`, `WalletRechargeRequest`, `WalletRechargeOut`)
- `apps/api/app/services/billing.py` (`get_or_create_wallet`, `get_balance`, `credit_wallet`, `debit_wallet`, `get_current_rate`, `list_transactions` — all take a `db: Session` and operate on one `user_id` at a time; `debit_wallet` uses a `SELECT ... FOR UPDATE` row lock on the wallet row so concurrent debits can't overdraw)
- `apps/api/app/services/payments.py` (`create_recharge_order`, `verify_webhook_signature`, `handle_payment_captured`)
- `apps/api/app/routers/wallet.py` (`GET /wallet`, `GET /wallet/transactions`, `POST /wallet/recharge`)
- `apps/api/app/routers/payments.py` (`POST /payments/webhook/razorpay`)
- `apps/api/tests/test_14_wallet_recharge.py`
- `apps/web/app/wallet/page.tsx`
- `apps/web/components/razorpay-checkout-script.tsx`
- `apps/web/__tests__/14-wallet-recharge.test.tsx`

## New dependencies

- **pip**: `razorpay` (official Python SDK — order creation + webhook signature verification helpers)
- **npm**: none required as an installed package — Razorpay Checkout is loaded via its hosted `checkout.js` script tag through `next/script`, not an npm package

## Rules for implementation

- Every query on `wallets`/`wallet_transactions` filters by `user_id` (not `org_id` — these tables are explicitly User-scoped per CLAUDE.md, not tenant-scoped like most tables)
- No raw SQL string interpolation — SQLAlchemy query builder or bound params only
- Business logic lives in `services/billing.py` and `services/payments.py`, not in `routers/wallet.py` or `routers/payments.py`
- `Wallet.balance_inr` is never assigned directly outside `billing.py`; every change is preceded by a `WalletTransaction` insert within the same DB transaction, and `balance_after_inr` on that row must match the post-write `wallets.balance_inr`
- `debit_wallet` and `credit_wallet` take a row lock (`SELECT ... FOR UPDATE`) on the target wallet before reading its balance, so concurrent calls for the same user serialize instead of racing
- The Razorpay webhook handler is the only code path allowed to call `credit_wallet` for a recharge — never credit from `POST /wallet/recharge` or any client-facing response
- Webhook signature verification happens before any DB write; an invalid signature returns 400 and touches no rows
- Webhook handling is idempotent on `razorpay_order_id` (enforced by the partial unique index) — a redelivered webhook for an already-credited order is a no-op 200, not a duplicate credit or an error
- `PricingRate` values are read through `billing.get_current_rate(action_type)`, never hardcoded — mirrors the scoring-weights rule in CLAUDE.md
- API contracts are Pydantic models in `apps/api/app/schemas/wallet.py`; TS types in `apps/web/lib/api.ts` are hand-written to match them for now (this repo has no codegen step yet — same pattern as every existing type in `api.ts`)
- Nothing in this spec touches `cards.py`/`card_service.py`/`enrichment_service.py`/`scoring_processing.py` — those stay unbilled until a future step wires `debit_wallet` into them

## Definition of done

- [ ] `alembic upgrade head` creates `pricing_rates`, `wallets`, `wallet_transactions` and seeds three `pricing_rates` rows (parse=5, enrichment=3, scoring=2)
- [ ] A new user has no `wallets` row until their first `GET /wallet` or `POST /wallet/recharge` call, which lazily creates one with `balance_inr=0`
- [ ] `POST /wallet/recharge` with a valid `amount_inr` returns a real Razorpay order id and does not change `wallets.balance_inr`
- [ ] Posting a validly-signed `payment.captured` webhook for that order credits the wallet exactly once, inserts one `wallet_transactions` row with `transaction_type="recharge_credit"` and correct `balance_after_inr`, and `GET /wallet` reflects the new balance
- [ ] Re-posting the identical webhook payload a second time does not double-credit the wallet (idempotency check passes)
- [ ] Posting a webhook with an invalid/missing signature returns 400 and leaves `wallets`/`wallet_transactions` unchanged
- [ ] `GET /wallet/transactions` returns only the requesting user's own rows, newest first, respecting `limit`/`offset`
- [ ] A second user's wallet/transactions are never visible or mutable via any endpoint using the first user's session
- [ ] `apps/web/app/wallet/page.tsx` renders balance and transaction history, and clicking "Add Money" opens Razorpay Checkout with the order returned from `POST /wallet/recharge`
- [ ] `pytest apps/api/tests/test_14_wallet_recharge.py` passes
- [ ] Frontend vitest suite (`apps/web/__tests__/14-wallet-recharge.test.tsx`) passes
