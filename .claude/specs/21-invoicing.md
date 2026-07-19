# Spec: Invoicing

## Overview
Every time a user recharges their prepaid wallet (spec `14-wallet-recharge`), DASHR must issue a durable, immutable invoice for that recharge under a single service line item, "Cardex Recharge - For Visiting Card Parsing, Enrichment and Scoring". 18% GST (9% CGST + 9% SGST, SAC code 9983) applies on top of every recharge — the wallet is credited exactly the amount the user requested, while Razorpay collects that amount plus GST, and the invoice shows the full taxable-value/CGST/SGST/total breakdown. This step adds an `Invoice` record generated synchronously the moment a Razorpay `payment.captured` webhook credits a wallet, renders it as a PDF (bearing the DASHR logo, DASHR's own registered company details, and the paying user's billing details from their `SellerProfile`), stores it in object storage, and surfaces it to that user — and to every admin of that user's Organization — through a new **Orders** section under Settings. This sits at the review/export end of the roadmap: it doesn't touch card capture, extraction, enrichment, or scoring, but it does extend the billing/wallet layer that already exists — not just add alongside it.

## Depends on
- `14-wallet-recharge` — `Wallet`, `WalletTransaction`, `PricingRate`, Razorpay order creation, and the signature-verified webhook handler (`services/payments.py::handle_payment_captured`) that performs the credit this feature hooks into. **This spec modifies, not just depends on, that flow**: `create_recharge_order` must charge Razorpay 18% more than the requested recharge amount, and `handle_payment_captured` must credit the wallet only the pre-tax amount — see API endpoints and Files to change below
- `06-company-profile-backend` — `SellerProfile` (GST No., Billing Address, and `User.name`) supplies the bill-to party
- `17-admin-user-management` — `get_current_admin` and the admin/org-scoping pattern this feature reuses for admin-visible invoices

## API endpoints (apps/api)
- `POST /wallet/recharge` (**modified**, owned by `14-wallet-recharge`) — now creates the Razorpay Order for `amount_inr × 1.18` (18% GST) instead of `amount_inr` alone, and stores the requested pre-tax `amount_inr` in the order's `notes` (alongside the existing `notes.user_id`) so the webhook can credit the wallet the correct, un-taxed amount regardless of what Razorpay actually collected — org-authenticated — request: unchanged `WalletRechargeRequest { amount_inr: Decimal (min 100, max 500000) }` (bounds still apply to the pre-tax amount) — response: `WalletRechargeOut { razorpay_order_id: str, razorpay_key_id: str, net_amount_inr: Decimal, cgst_amount_inr: Decimal, sgst_amount_inr: Decimal, gross_amount_inr: Decimal, currency: "INR" }` — the frontend must pass `gross_amount_inr` (in paise) as the Razorpay checkout widget's `amount`, since that's what the created Order actually charges
- `GET /invoices` — paginated list of the current user's own invoices, newest first — org-authenticated — query: `limit: int = 50 (max 200)`, `offset: int = 0` — response: `list[InvoiceOut]`
- `GET /invoices/{invoice_id}` — single invoice's metadata — org-authenticated — 404 unless the invoice belongs to the caller, or the caller is an admin of the invoice's `org_id` (read-only visibility, per CLAUDE.md) — response: `InvoiceOut`
- `GET /invoices/{invoice_id}/pdf` — streams the invoice PDF (`Content-Type: application/pdf`, `Content-Disposition: attachment; filename="{invoice_number}.pdf"`) — org-authenticated — same visibility rule as above — 404 if not visible to the caller
- `GET /invoices/org` — admin-only, every invoice issued to any user in the admin's Organization, newest first — org-authenticated (`get_current_admin`) — query: `limit: int = 50 (max 200)`, `offset: int = 0` — response: `list[InvoiceOut]`

No `POST`/`PATCH`/`DELETE` endpoints — an Invoice is never created, edited, or deleted through the API directly; it is only ever produced as a side effect of a successful wallet recharge, and CLAUDE.md forbids editing or deleting an issued Invoice.

## Frontend surface (apps/web)
- **Modified: `apps/web/app/wallet/page.tsx`** — `handleRecharge` must use `order.gross_amount_inr` (not the old `order.amount_inr`, which no longer exists on `WalletRechargeOut`) for the Razorpay widget's `amount` field. Show a live "+ 18% GST" estimate under the Amount input as the user types (client-side `amount × 1.18`, purely a display estimate — the authoritative figures are whatever `createWalletRecharge` returns), so the user sees the total they'll actually be charged before clicking "Add Money", not just the amount that lands in their wallet.
- **Modified: `apps/web/app/settings/page.tsx`** — add a third entry to `TABS` (`{ id: "orders", label: "Orders", icon: Receipt }`), a `TabId` union member `"orders"`, and render an `OrdersTab` component when `tab === "orders"`. `OrdersTab` fetches `listInvoices()` on mount and renders a table (date issued, invoice number, amount, a "Download PDF" button per row) with the same loading/error conventions as `RolesAccessTab`/`CompanyProfileTab`. No new route — this lives entirely inside the existing Settings page's tab switcher, per CLAUDE.md ("Settings gains a new Orders section").
- **New: `apps/web/components/orders-tab.tsx`** (or inline in `settings/page.tsx`, matching how `CompanyProfileTab`/`RolesAccessTab` are currently inlined there rather than extracted — follow whichever the file does when this is implemented) — the Orders table described above.

## Database changes
New table, `invoices` — org-scoped per CLAUDE.md's Organization bullet ("every other table carries an org_id... even where, as with Wallet/Invoice, it is not the billing scope"); billing/visibility scope is still the individual `user_id`, `org_id` exists only so the admin-visibility query (`GET /invoices/org`) can filter by tenant without joining through `users`.

```
invoices
  invoice_id            UUID PK, server_default gen_random_uuid()
  user_id                UUID NOT NULL, FK users.user_id           -- billing/visibility owner
  org_id                 UUID NULL, FK organizations.org_id        -- denormalized from user.org_id at issue time; NULL if the user had no org
  wallet_transaction_id  UUID NOT NULL, FK wallet_transactions.wallet_transaction_id, UNIQUE  -- one invoice per recharge, enforced at the DB level
  invoice_number          TEXT NOT NULL, UNIQUE                     -- e.g. "DASHR-INV-000123", from a dedicated Postgres sequence (see below)
  sac_code                 TEXT NOT NULL DEFAULT '9983'
  taxable_value_inr        NUMERIC NOT NULL                          -- pre-tax recharge amount; equals the WalletTransaction amount credited to the wallet
  cgst_rate_percent        NUMERIC NOT NULL DEFAULT 9.00
  sgst_rate_percent        NUMERIC NOT NULL DEFAULT 9.00
  cgst_amount_inr          NUMERIC NOT NULL
  sgst_amount_inr          NUMERIC NOT NULL
  total_inr                NUMERIC NOT NULL                          -- taxable_value_inr + cgst_amount_inr + sgst_amount_inr; what Razorpay actually collected
  currency                TEXT NOT NULL DEFAULT 'INR'
  service_description     TEXT NOT NULL DEFAULT 'Cardex Recharge - For Visiting Card Parsing,Enrichment and Scoring'
  bill_to_name             TEXT NOT NULL                            -- snapshot of SellerProfile.company_name (if gst_no is set) or User.name (if not), at issue time
  bill_to_gst_no           TEXT NULL                                -- snapshot of SellerProfile.gst_no at issue time (optional, per CLAUDE.md)
  bill_to_billing_address  TEXT NULL                                -- snapshot of SellerProfile.billing_address at issue time (optional)
  issuer_name              TEXT NOT NULL                            -- snapshot of DASHR's registered name at issue time
  issuer_gst_no            TEXT NOT NULL
  issuer_address           TEXT NOT NULL
  terms_and_conditions     TEXT NOT NULL                            -- snapshot of the T&C text at issue time (see invoicing.py below)
  pdf_storage_key          TEXT NOT NULL                            -- S3 key, e.g. invoices/{user_id}/{invoice_number}.pdf
  issued_at                TIMESTAMPTZ NOT NULL, server_default now()
```

Indexes: `ix_invoices_user_id_issued_at (user_id, issued_at)` for `GET /invoices`; `ix_invoices_org_id_issued_at (org_id, issued_at)` for `GET /invoices/org`.

`bill_to_name` is `SellerProfile.company_name` when `gst_no` is set (a GST-registered buyer is billed under their registered company name, since the name must match the GSTIN shown alongside it on the invoice), falling back to `User.name` if `gst_no` is set but `company_name` was never filled in (never blank); with no `gst_no` on file, `bill_to_name` is always `User.name`, regardless of whether `company_name` happens to be set.

All snapshot groups (bill-to, issuer, tax breakdown, `service_description`, `terms_and_conditions`) are captured at generation time and never re-derived from live `SellerProfile`/constant lookups afterward — this is what makes "immutable once issued" (CLAUDE.md) true even if a user edits their GST No. next month, DASHR's own registered address changes, or the GST rate or standard T&C wording is updated later, mirroring how `PricingRate` is versioned so historical invoices stay correct after a rate change.

`taxable_value_inr` must equal the `amount_inr` on the `WalletTransaction` this invoice covers (the pre-tax amount actually credited to the wallet) — never the gross amount Razorpay collected. `cgst_amount_inr`/`sgst_amount_inr`/`total_inr` are computed from it via `billing.compute_gst` (see Files to change), the same function `create_recharge_order` used to decide how much to actually charge via Razorpay — using one shared function for both is what guarantees `total_inr` on the invoice always exactly equals what Razorpay collected, with no independent-rounding drift between the two call sites.

A dedicated Postgres sequence, `invoice_number_seq` (created via `op.execute("CREATE SEQUENCE ...")` in the Alembic migration, not app-level code), backs `invoice_number` generation — `services/invoicing.py` calls `db.scalar(func.nextval("invoice_number_seq"))` and formats it, so numbering stays gap-free and safe under concurrent recharges without a row lock on `invoices` itself.

## Background jobs
No new background job changes. Invoice generation (a DB insert, a template-filled PDF render, and one S3 `put_object`) happens synchronously inside `services/payments.py::handle_payment_captured`, immediately after `billing.credit_wallet` succeeds — not a Celery task. Unlike OCR/enrichment/scoring, this touches no external AI/enrichment API and is fast and deterministic, so CLAUDE.md's "never block a request on OCR/enrichment" rule doesn't apply; the closest precedent is `cards/export`, which is also synchronous. If the webhook is a redelivery for an already-invoiced transaction, generation is a no-op (see Rules below).

## Files to change
- `apps/api/app/services/billing.py` — add `GST_RATE = Decimal("0.18")` (9% CGST + 9% SGST), `SAC_CODE = "9983"`, and `compute_gst(net_amount_inr: Decimal) -> tuple[Decimal, Decimal, Decimal]` returning `(cgst_amount_inr, sgst_amount_inr, gross_amount_inr)` — configurable-data placement mirrors `PricingRate`/`ACTION_TYPES` already living here per CLAUDE.md's rule that pricing is data, not hardcoded branches. This is the **single** source both `payments.create_recharge_order` and `invoicing.generate_invoice_for_transaction` call, so the amount Razorpay actually charges and the amount the invoice displays can never independently round to different totals.
- `apps/api/app/services/payments.py` — `create_recharge_order` now creates the Razorpay Order for `billing.compute_gst(amount_inr)`'s `gross_amount_inr` (not `amount_inr`), and adds `net_amount_inr=str(amount_inr)` to the order's `notes` alongside the existing `user_id`. `handle_payment_captured` now reads `notes.net_amount_inr` (raising `MalformedWebhookPayloadError` if missing/unparseable, same treatment as a missing `notes.user_id`) and credits the wallet that amount — **never** `payment.amount / 100`, which is the gross, tax-inclusive figure Razorpay actually collected. Also calls `invoicing.generate_invoice_for_transaction(db, transaction)` right after `billing.credit_wallet(...)` succeeds, wrapped so a generation failure never rolls back or blocks the already-verified wallet credit
- `apps/api/app/schemas/wallet.py` — `WalletRechargeOut` gains `net_amount_inr`, `cgst_amount_inr`, `sgst_amount_inr`, `gross_amount_inr`, replacing the old single `amount_inr` field
- `apps/api/app/models/__init__.py` — register the new `Invoice` model
- `apps/api/app/core/config.py` — add any invoicing-related settings if the PDF template needs configurable values beyond the constants below (expected: none)
- `apps/api/requirements.txt` — add the new PDF library
- `apps/web/app/wallet/page.tsx` — see Frontend surface
- `apps/web/app/settings/page.tsx` — add the Orders tab (see Frontend surface)
- `apps/web/lib/api.ts` — update `WalletRechargeOut` to match the new schema; add `InvoiceOut` type, `listInvoices()`, `listOrgInvoices()`, and a blob-download `downloadInvoicePdf(invoiceId)` mirroring `exportCards`'s pattern

## Files to create
- `apps/api/app/models/invoice.py` — the `Invoice` SQLAlchemy model
- `apps/api/app/schemas/invoice.py` — `InvoiceOut` Pydantic model
- `apps/api/app/services/invoicing.py` — `generate_invoice_for_transaction(db, transaction) -> Invoice` (idempotent on `wallet_transaction_id`), the PDF template renderer, and the DASHR issuer constants:
  ```python
  DASHR_ISSUER_NAME = "DASHR Material Handling Solutions (OPC) Private Limited"
  DASHR_ISSUER_GST_NO = "06AAMCD5859M1ZX"
  DASHR_ISSUER_ADDRESS = "1185P, Near Arora Properties, Sector 46, Gurugram, Haryana 122001, India"

  DEFAULT_TERMS_AND_CONDITIONS = (
      "1. Recharged balance is prepaid and non-refundable/non-withdrawable "
      "from the website; it may only be spent on visiting card parsing, "
      "enrichment, and scoring actions on the DASHR AI platform.\n"
      "2. This invoice is issued on receipt of payment in full — no "
      "further amount is due against it.\n"
      "3. For a refund or withdrawal request, contact DASHR customer "
      "support; there is no self-serve refund flow.\n"
      "4. Subject to Gurugram jurisdiction only."
  )
  ```
  These are placeholders for review, not final legal copy — confirm the exact `DEFAULT_TERMS_AND_CONDITIONS` wording with DASHR before this ships.

  **PDF layout** (adapted from DASHR's existing Zoho-style tax invoice template, `IMG_6385.png` — content is trimmed as detailed below, but the *visual style* (outer border box, boxed/shaded section headers, ruled dividers, gridded item table) intentionally matches the reference exactly):
  - **Outer border**: a 1pt box drawn around the full page content area
  - **Header block**: boxed (own border), containing logo | issuer text | "INVOICE" title
  - **Meta row**: boxed, with a vertical divider between the two fields
  - **Bill To block**: boxed, with a shaded header bar (light grey) reading "Bill To"
  - **Subject line**: boxed
  - **Item table**: full grid border, shaded header row, numeric columns (Qty/Rate/Amount) right-aligned
  - **Totals block**: unboxed (plain, right-aligned), matching the reference's own unboxed totals — only the item table above it is boxed
  - **Header, left**: the DASHR logo, read from `assets/brand/` (see CLAUDE.md)
  - **Header, right of logo**: `issuer_name` (bold), `issuer_address`, `GSTIN {issuer_gst_no}`
  - **Header, top-right**: "INVOICE" as the document title — not "TAX INVOICE", since no tax breakdown is shown (see above)
  - **Meta row**: `Invoice # {invoice_number}` and `Invoice Date {issued_at, DD/MM/YYYY}` — no Due Date/Terms/Place of Supply/Ship To (all inapplicable to a fully-prepaid, non-physical service)
  - **Bill To block**: `bill_to_name`, `bill_to_billing_address` (omitted if blank), `GSTIN {bill_to_gst_no}` (omitted if blank)
  - **Subject line**: `service_description` (the same "Cardex Recharge - For Visiting Card Parsing, Enrichment and Scoring" text used in the table row below)
  - **Item table**: single row — `# | Description | SAC | Qty | Rate | Amount`, `1 | {service_description} | {sac_code} | 1 | {taxable_value_inr} | {taxable_value_inr}`
  - **Totals block**: `Sub Total` (`taxable_value_inr`), `CGST @ {cgst_rate_percent}%` (`cgst_amount_inr`), `SGST @ {sgst_rate_percent}%` (`sgst_amount_inr`), `Total Paid` (`total_inr`) — no "Balance Due" line, since the full `total_inr` is captured via Razorpay before this invoice is generated, and no IGST line, since every recharge is billed CGST+SGST
  - **No Bank Details section** — deliberately omitted: this is a receipt for a prepaid, already-collected amount, not a bill requesting payment into an account, so there's nothing for a payer to remit against
  - **Terms & Conditions**: `terms_and_conditions`, rendered as a numbered list
  - **Footer**: "This is a computer generated invoice and does not require a signature." — replaces the reference template's signature block entirely; no "Authorized Signature" caption, blank underline, or scanned signature/stamp image
  - **No footer branding** — the reference template's "Powered by Zoho" line has no DASHR equivalent and is dropped entirely
- `apps/api/app/routers/invoices.py` — the four endpoints above
- `apps/api/migrations/versions/0022_invoices.py` — the `invoices` table, its indexes, and the `invoice_number_seq` sequence
- `apps/web/components/orders-tab.tsx` — if extracted rather than inlined (see Frontend surface note)

## New dependencies
- **reportlab** (Python, `apps/api/requirements.txt`) — pure-Python PDF generation with no system-level dependencies (unlike WeasyPrint's Pango/Cairo requirement), used to render the invoice template server-side

## Rules for implementation
- Every query on `invoices` filters by `org_id` for the admin-visibility path (`GET /invoices/org`), and by `user_id` for the self-visibility paths — never rely on `wallet_transaction_id` alone to scope a lookup
- No raw SQL string interpolation — SQLAlchemy query builder or bound params only; the `invoice_number_seq` sequence is schema DDL in the Alembic migration, not a runtime string-interpolated query
- Business logic (PDF rendering, numbering, idempotency) lives in `services/invoicing.py`, never in `routers/invoices.py` or inlined in `payments.py`
- API contracts are Pydantic models (`InvoiceOut`) — no hand-duplicated TS type; regenerate `packages/shared-types`
- `generate_invoice_for_transaction` must be idempotent on `wallet_transaction_id`: check for an existing `Invoice` row first (the DB's `UNIQUE` constraint on `wallet_transaction_id` is the backstop, not the primary guard) so a redelivered Razorpay webhook for an already-invoiced recharge is a silent no-op, matching `handle_payment_captured`'s own idempotency on `razorpay_order_id`
- Invoice generation failure (PDF render error, S3 unreachable) must **never** roll back or fail the wallet credit — the payment was already captured by Razorpay and the wallet already credited by the time invoicing runs; log the failure and leave the transaction creditable/re-visitable for a manual/retried invoice generation later, rather than losing or reversing real money over a PDF bug
- Never generate an Invoice for anything other than a `recharge_credit` `WalletTransaction` — never per card parsed, per enrichment, per scoring action, or per bulk batch (CLAUDE.md)
- Every field snapshotted onto the `Invoice` row (`bill_to_*`, `issuer_*`, the tax breakdown, `service_description`) is read once at generation time and never re-joined from `SellerProfile`/`User`/constants afterward — an invoice's PDF and its DB row must always agree, forever, even after the source profile changes
- No endpoint may update or delete an `Invoice` row after creation — there is no `PATCH`/`DELETE /invoices/*` route, full stop
- Admin visibility (`GET /invoices/org`) is strictly read-only — it must never expose, or be reachable from, any action that credits, debits, or refunds another user's wallet
- GST No./Billing Address remain optional on `SellerProfile` — `generate_invoice_for_transaction` must succeed and snapshot `bill_to_gst_no`/`bill_to_billing_address` as `NULL`/blank when either is unset, never block invoice generation on their absence
- `Wallet.balance_inr`/`WalletTransaction.amount_inr` for a recharge must always be the pre-tax amount the user requested — GST is collected via Razorpay and shown on the invoice, but never enters the wallet's own INR accounting (parse/enrichment/scoring `PricingRate`s are untouched by this spec and stay tax-free debits against that same pre-tax balance)
- `billing.compute_gst` is the only place GST math happens — never recompute CGST/SGST inline in `payments.py`, `invoicing.py`, or a router
- This spec bills every recharge as intra-state (CGST+SGST), matching DASHR's Haryana-registered GSTIN and the rate given for this spec, regardless of the paying user's own billing-address state — flagged here as a simplification, not re-derived from Place-of-Supply rules; revisit if DASHR later needs IGST for out-of-state customers

## Definition of done
- [ ] Alembic migration `0022_invoices` applies cleanly (`alembic upgrade head`) and creates `invoices`, its two indexes, and `invoice_number_seq`
- [ ] `POST /wallet/recharge` for `amount_inr=1000` creates a Razorpay Order for ₹1,180.00 (`gross_amount_inr`) and returns `net_amount_inr=1000`, `cgst_amount_inr=90`, `sgst_amount_inr=90`
- [ ] Recharging a wallet end-to-end (test webhook payload for `payment.captured` on that order) credits the wallet exactly ₹1,000.00 (not ₹1,180.00), and creates exactly one `Invoice` row with a unique `invoice_number`, `taxable_value_inr=1000`, `cgst_amount_inr=90`, `sgst_amount_inr=90`, `total_inr=1180`, correct `bill_to_*`/`issuer_*` snapshot values, and an uploaded PDF at `pdf_storage_key`
- [ ] Redelivering the same `payment.captured` webhook (same `razorpay_order_id`) does not create a second `Invoice` and does not double-credit the wallet
- [ ] `GET /invoices` returns only the caller's own invoices, paginated, newest first
- [ ] `GET /invoices/{invoice_id}` and `GET /invoices/{invoice_id}/pdf` 404 for a user who is neither the invoice's owner nor an admin of its `org_id`
- [ ] `GET /invoices/{invoice_id}/pdf` returns a valid PDF containing the DASHR logo, DASHR's issuer name/GST/address, invoice #/date, the bill-to party, the Subject line, the line item "Cardex Recharge - For Visiting Card Parsing, Enrichment and Scoring" with SAC 9983, the CGST/SGST/Total breakdown, the Terms & Conditions, and the "This is a computer generated invoice and does not require a signature." footer — and does **not** contain a Bank Details section, Ship To, Place of Supply, an IGST line, a Balance Due line, an "Authorized Signature" line or signature image, or any Zoho branding
- [ ] `GET /invoices/org` (admin) returns invoices for every user in the admin's org, including sub-users', and a non-admin caller gets 403
- [ ] Settings → Orders tab lists the current user's invoices and each row's "Download PDF" button downloads a real PDF file in the browser
- [ ] Wallet page shows the GST-inclusive total before the user clicks "Add Money", and the amount actually charged in the (test-mode) Razorpay checkout matches `gross_amount_inr`
- [ ] A `SellerProfile` with blank GST No./Billing Address still produces a valid invoice (blank bill-to fields), not an error
- [ ] Editing `SellerProfile`'s GST No./Billing Address after an invoice was issued does not change that invoice's already-snapshotted values
- [ ] `pytest apps/api/tests` passes, including new tests for `billing.compute_gst`'s rounding, `services/invoicing.py`'s idempotency/snapshot behavior, and `handle_payment_captured` crediting the net (not gross) amount
