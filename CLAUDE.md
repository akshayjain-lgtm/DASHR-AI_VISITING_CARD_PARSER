# CLAUDE.md

## Project overview

DASHR AI is a modern B2B SaaS platform for industrial and manufacturing sellers. It scans visiting cards collected in bulk at trade exhibitions, extracts structured contact data via AI/OCR, enriches each contact with public company data (firmographics, industry classification, size, revenue signals), and scores each resulting lead for product-fit against the seller's target customer profile.

Core workflows:
1. **Bulk capture** — upload/photograph dozens–hundreds of cards per exhibition in one batch
2. **Extraction** — AI vision parses each card image into structured fields (name, title, company, email, phone, address)
3. **Enrichment** — cross-reference extracted company names against public data sources to attach firmographics (industry/NAICS code, employee count, revenue band, location); if the prospect company turns out to itself be a registered DASHR org, link the two and prefer that org's own declared product lines/target-customer profile over generic third-party data, since we then know exactly what they're in the market for
4. **Scoring** — rank each lead against a configurable industrial/manufacturing product-fit model
5. **Review & export** — sales reps triage a scored lead list per exhibition, correct any AI-mis-extracted field inline (name, role/title, phone, email, address, company name, products, IndiaMART URL), and push qualified leads to CRM
6. **Billing** — sellers recharge their prepaid INR wallet via Razorpay, and each recharge generates an invoice (service name "Visiting Card Recharge and Scoring") viewable in their account
7. **Wallet usage** — every parse/enrich/score action debits the acting user's own wallet, after each user's first 20 free actions per action type (parse/enrich/score) are used up; once free allowance is exhausted, no parse/enrich/score action is allowed to run at a 0 wallet balance; once free allowance is exhausted, no parse/enrich/score action is allowed to run at a 0 wallet balance
8. **Analytics dashboard** — a filterable visual summary layer (lead volume, industry mix, score distribution, exhibition performance, role mix, region mix) so sellers get at-a-glance triage across exhibitions; row-by-row lead review happens on the Upload page, not here
9. **Feedback & support** — a Feedback page, reached via its own nav item in the logged-in app's sidebar (positioned just below Settings and FAQ), where a visitor can tell us what's working and what isn't; submissions are stored for later internal product review, never surfaced back to any user. The same page has a "raise a query" section for support questions, which assigns a ticket id and emails the full query to info@dashrtech.com

This is a greenfield repo — no application code exists yet. This file define
s the target architecture new work should scaffold toward, not a description of code that already exists. Treat structural claims below as the plan, not as verified fact, until the corresponding code lands.

---

## Architecture

Two-service architecture: a Next.js frontend/BFF for the SaaS UI and auth, and a Python backend that owns all AI/enrichment/scoring logic and the database. Splitting this way keeps the OCR/enrichment/scoring pipeline (Python's ML/data ecosystem) decoupled from the dashboard UI (React/Next's strength), and lets bulk-processing jobs scale independently of the web tier.

```
dashr-ai/
├── apps/
│   ├── web/                      # Next.js 14 (App Router, TypeScript) — SaaS frontend + BFF
│   │   ├── app/                  # Routes: dashboard (leads+analytics), upload, exhibitions, wallet, account, settings, feedback, (marketing)/privacy-policy, (marketing)/terms-of-use, (marketing)/faq
│   │   ├── components/           # UI components (shadcn/ui + Tailwind), charts (Recharts)
│   │   ├── lib/                  # API client for backend, auth helpers
│   │   └── package.json
│   │
│   ├── api/                      # FastAPI (Python) — business logic, owns Postgres
│   │   ├── app/
│   │   │   ├── routers/          # REST endpoints: cards, leads, exhibitions, orgs, wallet, invoices, payments/webhooks, feedback
│   │   │   ├── services/
│   │   │   │   ├── ocr.py            # Card image → structured fields (vision LLM)
│   │   │   │   ├── enrichment.py     # Company name → firmographics lookup
│   │   │   │   ├── scoring.py        # Firmographics + role → product-fit score
│   │   │   │   ├── billing.py        # Wallet debits/credits, pricing lookup, ledger writes
│   │   │   │   ├── invoicing.py      # Invoice generation on wallet recharge, service name "Visiting Card Recharge and Scoring"
│   │   │   │   ├── payments.py       # Razorpay order creation + webhook verification
│   │   │   │   └── notifications.py  # Outbound transactional email (e.g. support-query ticket notifications to info@dashrtech.com)
│   │   │   ├── models/           # SQLAlchemy models (multi-tenant, org-scoped)
│   │   │   ├── workers/          # Celery tasks for async batch processing
│   │   │   └── db/               # Session management, Alembic migrations
│   │   └── requirements.txt
│   │
│   └── mobile/                   # Future: React Native (Expo) app, same API — not yet scoped
│
├── packages/
│   └── shared-types/             # OpenAPI-generated TS types shared by web ↔ api (↔ mobile later)
│
├── assets/
│   └── brand/                    # Canonical DASHR logo (vector) — single source for both the web frontend and invoice PDF header
│
├── infra/
│   ├── docker-compose.yml        # Local dev: web, api, worker, postgres, redis
│   └── migrations/
│
└── README.md
```

**Where things belong:**
- UI, routing, session/auth flows → `apps/web`
- Anything touching the database, OCR, enrichment, scoring, wallet, or invoicing → `apps/api`, never in Next.js API routes beyond thin passthroughs
- Long-running or bulk work (batch card processing, enrichment lookups) → Celery tasks in `apps/api/app/workers`, never inline in a request handler
- New scoring criteria/weights → `apps/api/app/services/scoring.py`, kept as configurable data, not hardcoded branches
- Per-action pricing (parse/enrich/score rates) → `apps/api/app/services/billing.py`, kept as configurable data, not hardcoded branches — same principle as scoring weights, since prices will change and may eventually vary per-org
- Razorpay order creation, signature verification, and webhook handling → `apps/api/app/services/payments.py` + `apps/api/app/routers/payments` only; never verify payment status from a client-side callback alone
- Matching a scanned company against registered DASHR orgs (`Company.linked_org_id`) and the per-field-type staleness check on `CompanySignals` → `apps/api/app/services/enrichment.py`, run before falling back to third-party firmographics providers
- The Company Details write-up shown in Card Details (`Company.summary`) → `apps/api/app/services/enrichment_summary.py`, sourced from a documented, extensible list of public data inputs — Google Search, Claude, Google News, and the company's IndiaMART profile today — never a separate generator per source
- Marketing/static pages (privacy policy, terms of use, FAQ) → `apps/web/app/(marketing)/`, publicly accessible, no auth required
- The Feedback page → `apps/web/app/feedback/`, a sidebar nav item alongside Settings/FAQ, not under `(marketing)/`
- Feedback storage and the "raise a query" ticket id + email dispatch → `apps/api` (`feedback` router + `notifications.py`), never a client-side/mailto-only submission — unlike the general FAQ contact link (`mailto:info@dashrtech.com`), a raised query must be durably stored and provably emailed
- The DASHR logo (vector) → `assets/brand/`, the single canonical copy; both `apps/web` (site chrome) and `apps/api` (invoice PDF header) read from it rather than keeping separate copies

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Frontend | Next.js 14 (App Router) + TypeScript + Tailwind CSS + shadcn/ui | Fast to build a polished SaaS dashboard; server components suit data-heavy lead tables |
| Backend API | FastAPI (Python 3.11+) | Async I/O for enrichment API calls; same language as the AI/OCR pipeline |
| Card OCR/extraction | Vision-capable LLM (Claude with vision) via API, with a deterministic field validator/normalizer pass | Handwriting, varied layouts, and multiple languages on cards need a model, not template OCR; validator catches hallucinated fields before they hit the DB |
| Company enrichment | Third-party firmographics API (e.g. Clearbit/Crunchbase-style provider) + NAICS/SIC industry classification, cached in Postgres, shared cross-org | Public company data changes slowly — cache once and reuse for every org that meets that company, rather than re-fetching (and re-billing) the same public facts per org |
| Lead scoring | Rules-based weighted scoring service in `apps/api`, tuned for industrial/manufacturing fit (industry code, company size, revenue band, buyer title/seniority) | Sellers need to see *why* a lead scored the way it did — an explainable rules engine beats an opaque ML model here |
| Database | PostgreSQL via SQLAlchemy + Alembic | Relational integrity for org/lead/company relationships; strong multi-tenant row-level scoping |
| Object storage | S3-compatible bucket (card images) | Card images are large binary blobs — never store in Postgres |
| Async jobs/queue | Celery + Redis | Bulk uploads (hundreds of cards) must process in the background, not block the request |
| Auth | Org-based multi-tenant auth (Auth.js/NextAuth or Clerk) with an admin/sub-user role per User, JWT passed to FastAPI | B2B SaaS — every request is scoped to an organization for data visibility, but billing is scoped to the individual user, not the org |
| Payments | Razorpay (Orders API for wallet recharge; webhooks for confirmation; e-mandates for the later UPI AutoPay phase) | Native support for Indian Netbanking/UPI/Debit/Credit Card and UPI AutoPay in one provider; standard choice for Indian B2B SaaS |
| Wallet/billing ledger | Append-only ledger table in Postgres (SQLAlchemy), org-scoped | Money-bearing balances must be auditable and reconstructable from history, not just a mutable balance column |
| Invoicing | Server-generated PDF invoice per wallet recharge, single line item "Visiting Card Recharge and Scoring", stored in object storage, linked from Postgres, surfaced to the user via a new Orders section under Settings | Sellers need a durable, re-viewable record per recharge, not per individual/bulk parse action; never regenerate an invoice's contents after issue |
| Analytics/charts | Recharts (pairs with shadcn/ui) on the dashboard page | Lead-type/industry/score breakdowns need charts, not just tables; keep it in the same design system as the rest of the UI |
| Transactional email | Provider TBD (e.g. SES/SendGrid) — to be finalized in the Feedback & Support spec | A raised support query must reliably reach info@dashrtech.com from the backend itself, not rely on a client-side mailto link |
| Infra | Docker Compose (local), containers on AWS/GCP (prod) | Standard, portable, no vendor lock-in for a small early-stage service |
| CI/CD | GitHub Actions | Matches the existing GitHub-hosted repo and installed GitHub plugin |

---

## Dashboard & marketing pages

- What was the "leads page" is the **Dashboard** page: a pure analytics surface — a stat band (total/high-fit/low-fit leads) at the top, a filter bar (exhibition + time range + admin-only uploaded-by) scoping every chart identically, and charts on lead volume, industry mix, score distribution, exhibition performance, role mix, and region mix below. Row-by-row lead review/drill-down lives on `/upload`, not `/dashboard` — the two pages don't duplicate that surface
- **Both `/dashboard` and `/upload` share the same two filters — date range and, for admins, uploaded-by — so triage stays consistent whichever page a seller is on:**
  - **Date range** filters on `VisitingCard.created_at` (no new column). Same preset UI/semantics on both pages: Last 30 days (default) / Last 90 days / Last 1 year / All time / Custom range, via the existing `DashboardFilterBar`/`rangeToDates` convention from `16-dashboard-analytics` — `/upload` adopts this control rather than inventing a second date-picker pattern. `GET /cards` gains `start_date`/`end_date` query params (mirroring `GET /analytics/dashboard`'s existing ones) so `/upload`'s card list can be scoped the same way charts already are
  - **Uploaded-by** filters on `VisitingCard.user_id` (no new column) and is visible **only to org admins** — a `member`/org-less user never sees it, since they only ever see their own rows anyway. `/upload` already has this filter (`showUserFilter`/`userFilter`, populated via `GET /orgs/members`, applied through `GET /cards?user_id=`); `/dashboard` gains the matching control in `DashboardFilterBar`, and `GET /analytics/dashboard` gains a `user_id` query param threaded through `AnalyticsFilters`/`_apply_shared_filters` the same way `exhibition_ids`/`start_date`/`end_date` already are
  - Both filters are always applied **on top of** `scope_to_visible_users`, never in place of it — `user_id` (like `exhibition_id`/date bounds) can only ever narrow an already-visibility-scoped query, never widen it, so a non-admin or cross-org id passed to either endpoint yields an empty result, never a leak. The uploaded-by control is populated exclusively from `GET /orgs/members` (org-scoped, admin-only) — never a global user search or lookup by raw id/email
- The homepage carries public **Privacy Policy** and **Terms of Use** sections/pages — static content, no auth, served from `apps/web/app/(marketing)/`
- The **Feedback** page is a new item in the logged-in app's sidebar nav (`apps/web/components/sidebar.tsx`'s `NAV` list), positioned just below **Settings** and **FAQ** — not a CTA embedded in the FAQ page itself. It asks what's good about the tool and what went wrong (stored as `Feedback` rows for later internal product review, never shown back to any user), and has a separate "raise a query" section that stores a `SupportQuery`, assigns it a ticket id, and emails the full details to info@dashrtech.com — a spec for this page is coming separately
- The **profile page** collects each User's GST No. and Billing Address as optional fields on their `SellerProfile` row (in addition to the standard company/product fields) — captured per-User (via the 1:1 `SellerProfile`), not a single org-wide setting, since billing is per-user. Neither is mandatory, at profile-save time or for Invoice generation — an Invoice is issued whether or not either is populated, carrying whatever value (including blank) the `SellerProfile` row holds at issue time
- **Settings** gains a new **Orders** section listing every Invoice issued to the current user (one row per Wallet recharge), each with a PDF download — this is the only surface where a user views/downloads their invoices; a spec for this section is coming separately

---

## Future roadmap (not yet scoped — do not build ahead of a spec)

- **UPI AutoPay**: recurring wallet top-up via UPI e-mandate through Razorpay, once manual recharge is live and stable
- **Mobile apps (Android/iOS)**: same feature set as the web app, planned as a React Native (Expo) app in `apps/mobile` sharing `packages/shared-types` and the same FastAPI backend — no native-only business logic, the API stays the single source of truth
- Treat both as future phases: mention them in specs only where a current decision would otherwise block or complicate them later (e.g. keep pricing/billing logic in the API, not the Next.js BFF, so mobile can reuse it without duplication)

---

## Code style

- TypeScript (web): strict mode on, no `any` without justification, functional components only
- Python (api): PEP 8, type hints required on all function signatures, snake_case
- API contracts: FastAPI Pydantic models are the source of truth; TS types in `packages/shared-types` are generated from them, never hand-duplicated
- DB access: always through SQLAlchemy models/queries in `apps/api/app/db`, never raw SQL strings in routers or services
- Every DB row that isn't a global/reference table must carry an `org_id` and every query must filter on it — this is a multi-tenant app, cross-tenant leaks are a security bug, not a style nit

---

## Data model essentials

- **Organization** — the tenant; every other table carries an `org_id` for tenant-isolation/data-visibility purposes, even where (as with Wallet/Invoice below) it is not the billing scope
- **User** — belongs to one Organization, with a role of `admin` or `sub_user`
- **SellerProfile** — the signed-up seller's own company/product profile (company name, industry, product lines, revenue, target customer/regions), one row per User (1:1, unique `user_id`), used to calibrate lead scoring; also carries that User's GST No. and Billing Address (both optional, never required), used as the billing party on that user's Invoices when present, and an optional Role/Designation (e.g. job title) for that User. The Company Profile UI also collects a "Name" field, but that writes through to `User.name` itself, not a separate SellerProfile column — one shared name, not two fields that could drift apart
- **Exhibition** — a trade show/batch upload event (name, date, location)
- **Card** — one scanned image + raw OCR output + extraction confidence
- **Contact** — normalized person fields parsed from a Card (name, title, email, phone)
- **Company** — enriched firmographics, keyed by normalized company name/domain, shared across orgs where public data allows (cache once, reuse — no `org_id`, see Warnings). Optionally carries `linked_org_id` (nullable FK → `Organization`), set when the prospect company is itself a registered DASHR org — matched by normalized name/domain against registered orgs' `SellerProfile.company_name`/domain. This is a tag on the shared row, not a scope: it never duplicates the row and never grants either org visibility into the other's leads/wallet/invoices, it only tells enrichment which `SellerProfile` to prefer (see `CompanySignals` below and Warnings). `summary` holds the **Company Details** write-up shown in Card Details — a fixed-section digest (What They Do, Background, an Operational Angle tailored to the business type, Recent Signals from news, and a sales Icebreaker) rather than a loose paragraph — generated from an extensible list of public data inputs: Google Search, Claude, Google News, and the company's IndiaMART profile today, with more sources expected to fold in later as additional inputs to that same generation step, never a parallel pipeline per source
- **CompanySignals** — a 1:1 extension of `Company` (same shared cache, no `org_id`) holding the granular detail scoring reads: MCA/GST/Udyam registry and compliance facts, revenue/product/plant signals, and marketplace/social profile data — IndiaMART today, with LinkedIn/Glassdoor/Facebook/YouTube expected to fold into this same table as columns when added, not a new table per source. Freshness is tracked as two independent clocks rather than one blanket `updated_at`: `factual_fetched_at` (6-month TTL) covers registry, compliance, revenue band, and website-derived product lines — facts that rarely change; `dynamic_fetched_at` (3-month TTL) covers LinkedIn firmographics, growth/momentum/news signals, Google rating, and the whole IndiaMART block — data that moves faster. A refresh only re-fetches whichever half has passed its own TTL, never the whole row on one schedule
- **Lead** — the join of Contact + Company + a computed product-fit Score, scoped to one Organization and Exhibition
- **FieldCorrection** — an append-only audit record of every user-made correction to an AI-extracted or enriched field (name, role/title, phone, email, address, company name, products, or a company's IndiaMART URL), scoped to the Organization and User who made it and linked to the Card the correction was made from. Stores both `original_value` and `corrected_value` for that one field/event — never overwritten, so repeated corrections to the same field each get their own row and extraction/enrichment accuracy can be measured field-by-field later. Correcting a card-level field (name, title, phone, email, address, products) updates that field directly on the `Card`/`Contact` row it lives on. Correcting **company name** re-runs company match/dedup against the corrected name rather than renaming the shared `Company` row in place — `Company` is a cross-org cache keyed by normalized name/domain, and an in-place rename from one org's correction would silently relabel that company for every other org sharing the cached row. Correcting the **IndiaMART URL** (`CompanySignals.catalog_url`) is the one field-level exception to that rule: since it's itself shared, non-org-scoped cache data (see `19-data-enrichment-indiamart`), the correction updates it in place on the shared row and re-triggers the Apify `supplierProfile` lookup against the corrected URL, refreshing all `indiamart_*` fields for every org sharing that Company
- **Wallet** — one INR prepaid balance **per User**, not per Organization; the balance itself is a derived/cached value, not the source of truth (the ledger is). A sub-user's wallet is entirely their own — there is no shared org-level balance and no admin spending authority over it
- **FreeActionAllowance** — a per-User, per-action-type usage counter (parse/enrich/score, each capped at 20 free actions at launch) tracked independently of the Wallet; increments on every parse/enrich/score by that user regardless of whether it was free or wallet-debited, and gates the point at which that action type starts debiting the wallet
- **WalletTransaction** — append-only ledger entry (recharge credit, parse/enrichment/scoring debit, or support-initiated adjustment), scoped to one User; never updated or deleted, only inserted. Carries a `quantity` (default 1): a single-card parse/enrich/score debit is quantity 1 with that card as `reference_id`; a bulk batch is billed as one row with `quantity` = however many cards in the batch were actually charged, `reference_id` NULL (no single card to point at)
- **Corrections and their knock-on effects are never separately billed.** Correcting the IndiaMART URL re-fetches that company's `indiamart_*` fields for free, regardless of free-allowance/wallet state — it's fixing a mistake in an already-paid-for enrichment, not a new billable action. Likewise, once a Card has been scored, correcting any field on it unlocks exactly one free rescore (`VisitingCard.lead_score`/`score_breakdown`/`scored_at` recomputed) — also never billed, never counted against the free allowance — until the next correction unlocks another one. A rescore is only offered while at least one `FieldCorrection` postdates the card's current `scored_at`; with no such correction, scoring stays one-shot exactly as before
- **Invoice** — generated per Wallet recharge (never per card parsed or per batch), scoped to the User who made the recharge, references the recharge WalletTransaction it covers, carries a single service line item titled "Cardex Recharge - For Visiting Card Parsing,Enrichment and Scoring"; immutable once issued. Rendered as a PDF (with the DASHR logo from `assets/brand/`) and listed in the Orders section under Settings. The bill-to party is sourced from that User's `SellerProfile`/`User.name` at issue time: the billed name is `SellerProfile.company_name` when `gst_no` is set (a GST-registered buyer is billed under their registered company name, to match the GSTIN shown alongside it), falling back to `User.name` if `gst_no` is set but `company_name` was never filled in; with no `gst_no` on file, the billed name is always `User.name`. The issuer/seller-of-record side is fixed platform-wide data, not per-user:
  - **Name:** DASHR Material Handling Solutions (OPC) Private Limited
  - **GST:** 06AAMCD5859M1ZX
  - **Address:** 1185P, Near Arora Properties, Sector 46, Gurugram, Haryana 122001, India
- **PricingRate** — configurable per-action rate (parse/enrichment/scoring, currently ₹5/₹3/₹2), versioned so historical invoices remain correct if rates change later
- **Feedback** — a free-text "what's good" / "what went wrong" submission from the Feedback page (its own sidebar nav item, just below Settings and FAQ, behind login like the rest of the app). Captures the submitter's `user_id`/`org_id`. Stored purely for internal product-improvement review — never surfaced back to any user or org
- **SupportQuery** — a "raise a query" submission from the same Feedback page. On creation the API assigns a human-readable ticket id and sends a server-side email to info@dashrtech.com with the full query details — never a client-side/mailto submission, so ticket issuance and email dispatch stay atomic and auditable. Captures `user_id`/`org_id` from the submitter

---

## Billing & wallet model

- **Wallet, recharge, and spend are scoped to the individual User (admin or sub-user), never to the Organization.** Every user — admin or sub-user — recharges and spends their own wallet independently; there is no shared/org-level wallet, and one user's balance can never fund another user's actions
- The Admin/Sub-User relationship governs **data visibility only** (who can see which leads/cards/exhibitions within an org), not billing. Never let an admin role imply spending authority over a sub-user's wallet, and never let it imply a sub-user can spend an admin's balance
- Wallet recharge is prepaid-only: a user adds INR via Razorpay to their own wallet (Netbanking/UPI/Debit/Credit Card at launch; UPI AutoPay e-mandate is a later phase, not in the first cut)
- Recharged balance is **not refundable/withdrawable from the website**. It can only be spent on parsing/enrichment/scoring actions by that same user. A user who wants cash back must raise a request with customer care — there is no self-serve withdrawal flow to build
- Wallet balance is only ever credited/debited through `billing.py`, and every credit/debit writes a `WalletTransaction` ledger row first — the cached `Wallet.balance` is derived from the ledger, never the other way around
- Per-action pricing at launch: ₹5 per card parsed, ₹3 per enrichment, ₹2 per scoring, debited from the wallet of the user who triggered the action. These rates are configurable data in `billing.py`/`PricingRate`, not hardcoded — same rule as scoring weights
- **Free tier**: each User gets their first 20 actions of each type free — 20 free parses, 20 free enrichments, 20 free scorings, tracked as independent per-action-type counters (a `FreeActionAllowance`), not a single combined pool. Wallet debiting for a given action type only begins once that type's own free count is exhausted; the other two types keep debiting/staying-free on their own independent counts. The free-tier cap (20) is configurable data alongside `PricingRate`, not hardcoded
- **Zero-balance hard stop**: once a user's free allowance for an action type is exhausted, that action type is billable — and if their wallet balance is 0 (or insufficient for the action's rate), the action must be blocked outright before it starts: never run OCR/enrichment/scoring, and never enqueue the Celery task, for a billable action the user can't pay for. This check happens in `billing.py` ahead of the OCR/enrichment/scoring call, not after
- A Razorpay payment is only considered successful, and a wallet only credited, after webhook signature verification server-side — never on the strength of a client-side redirect/callback alone
- An Invoice is generated per Wallet recharge — never per card parsed or per batch of cards parsed — under a single service line item titled **"Visiting Card Recharge and Scoring"**, billed to the recharging user and carrying that user's GST No./Billing Address from their `SellerProfile` row. Parse/enrichment/scoring debits still hit the ledger (for balance tracking), but they are not separately invoiced — the invoice is tied to the recharge transaction, not to each debit. A bulk parse/enrich/score batch writes one collective `WalletTransaction` for the whole batch (carrying a `quantity` of how many actions it covers) rather than one row per card, so transaction history stays readable for a large batch; a single-card action still writes its own one-row, quantity-1 transaction with that card's id as `reference_id`. **Invoices are visible to the user who generated them, and to every admin of that user's Organization** — admin visibility into invoices is read-only, and does not extend to spending from or crediting the sub-user's wallet. Invoices are immutable once issued (corrections are new adjustment entries, not edits)
- A parse/enrich/score action must never be allowed to proceed, or be enqueued as billable Celery work, without first confirming sufficient balance in the **acting user's own wallet** — check-then-debit must be race-safe (e.g. a DB-level constraint or row lock), since concurrent bulk uploads by the same user can hit the same wallet at once

---

## Subagent Policy
- Always use a builtin explore subagent for codebase exploration before implementing any new feature
- Always use a subagent to verify test results after any implementation
- When asked to plan, delegate codebase research to a subagent before presenting the plan
- Always use the builtin plan subagent in plan mode

---

## Warnings and things to avoid

- **Never skip the `org_id` filter** on a query — this is a multi-tenant SaaS; a missing tenant scope is a data leak, not a bug to fix "later"
- **Never let the uploaded-by filter (`GET /cards`/`GET /analytics/dashboard`'s `user_id` param) bypass or substitute for `scope_to_visible_users`** — it must only ever narrow an already-visibility-scoped query, and the dropdown populating it must only ever come from `GET /orgs/members` (org-scoped, admin-only), never a global user search or a raw id/email lookup. Never render the uploaded-by control for a non-admin — a `member`/org-less user already only sees their own rows, so the control has nothing to add and shouldn't imply otherwise
- **Never store card images in Postgres** — object storage only, DB holds the URL/key
- **Never call enrichment providers per-request without checking the Company cache first** — these APIs are rate-limited and billed per lookup
- **Never hardcode scoring weights inline in a route handler** — scoring criteria must live in `scoring.py` as data so they can be tuned per-org later without a redeploy
- **Never block a request on OCR or enrichment for bulk uploads** — batch card processing is async via Celery; the upload endpoint only enqueues work
- **Never trust raw vision-LLM output as final** — always run extracted fields through the validator/normalizer before persisting (catches malformed emails, missing required fields, obvious hallucinations)
- **Never mutate `Wallet.balance` directly** — every change goes through a `WalletTransaction` ledger insert first; the balance is a derived read, not the source of truth
- **Never scope Wallet/WalletTransaction spending to the Organization** — they are scoped to the individual User. An org can hold many users' wallets, but no wallet is shared, poolable, or spendable by another user, including an admin
- **Never let an admin role imply spend authority over a sub-user's wallet** — admin visibility into sub-user data (including Invoices) is read-only; it never grants the ability to spend, recharge, or credit another user's wallet
- **Never credit a wallet from a client-side payment callback** — only a signature-verified Razorpay webhook may confirm a recharge and trigger a credit to the paying user's own wallet
- **Never build a self-serve withdrawal/refund flow** — wallet funds are spend-only from the website by design; withdrawals are a manual customer-care process, not a feature to scaffold
- **Never hardcode per-action prices inline** — parse/enrichment/scoring rates live in `billing.py`/`PricingRate` as configurable data, mirroring the scoring-weights rule
- **Never let a billable action (parse/enrich/score) run without a race-safe balance check on the acting user's own wallet** — concurrent bulk uploads can overdraw a wallet if debit isn't atomic with the balance check
- **Never let a parse/enrich/score action proceed at 0 wallet balance once that action type's free-20 allowance is used up** — check the `FreeActionAllowance` counter first, then the wallet balance, before doing any OCR/enrichment/scoring work or enqueuing Celery work; a user with no free allowance left and no balance gets blocked, not overdrawn
- **Never share one free-allowance counter across parse/enrich/score** — each action type gets its own independent 20-free count per user; exhausting the free parses must not affect free enrichments or free scorings
- **Never edit or delete an issued Invoice** — corrections are new ledger/adjustment entries, not mutations of past invoices
- **Never generate an Invoice per card parsed or per batch** — invoicing is tied to the Wallet recharge event only, under the single service name "Visiting Card Recharge and Scoring"; parse/enrichment/scoring debits are ledger entries, not separate invoices
- **GST No./Billing Address are never a precondition for generating an Invoice** — both are optional on `SellerProfile`; an Invoice is issued on every Wallet recharge regardless of whether either is populated, using whatever value (including blank) is on file at issue time. Invoices are still billed to the individual user, so those fields are never inherited from the org — just never required
- **Never overwrite an extracted/enriched field in place without first writing a `FieldCorrection` row carrying both `original_value` and `corrected_value`** — accuracy reporting depends on having both sides of every correction, not just the latest value
- **Never add `org_id` to `Company`, `CompanySignals`, `ProductFitJudgment`, or `GeocodedAddresses`** — these are deliberate cross-org shared caches (the same public company/address/product-fit answer applies for every org that encounters it). Scoping them per-org was considered and rejected: it would re-fetch and re-bill the same public data once per org and let two orgs' copies of the same fact silently drift apart. The way to record that a prospect is *also* one of our own customers is `Company.linked_org_id` — a tag on the shared row, never per-org duplication
- **Never create a new table per enrichment source** — LinkedIn, Glassdoor, Facebook, YouTube, etc. each add columns to `CompanySignals` under its existing factual/dynamic grouping, the same pattern IndiaMART already follows, so scoring keeps reading one row per company instead of joining across many
- **Never add a new Company Details data source as a parallel summary generator** — fold each new source (Google Search, Claude, Google News, and the IndiaMART profile today; more expected later) into `enrichment_summary.py`'s existing generation step as an additional input, the same "no parallel pipeline per source" rule `CompanySignals` already follows for raw signals
- **Never let the Company Details write-up drop its fixed section structure** (What They Do, Background, Operational Angle, Recent Signals, Icebreaker) **or fabricate a Recent Signals line when no news was found** — that section is the one allowed to be omitted outright; the other four are never skipped or collapsed back into one undifferentiated paragraph
- **Never refresh all of `CompanySignals` on one schedule** — `factual_fetched_at` (6 months) and `dynamic_fetched_at` (3 months) are independent; a refresh job checks each TTL separately and only re-fetches the sources behind whichever one actually expired
- **Never treat a `Company.linked_org_id` match as license to skip normal per-org visibility scoping** — the linkage only unlocks reading the matched org's `SellerProfile` for enrichment purposes; it never grants either org visibility into the other's leads, cards, wallet, or invoices
- **Never rename the shared `Company` row in place from a company-name correction** — re-run match/dedup against the corrected name (reuse or create a `Company` row) instead, since `Company` is a cross-org cache and an in-place rename would silently relabel that company for every other org sharing it
- **Never leave stale supplier-profile data attached after an IndiaMART URL correction** — correcting `CompanySignals.catalog_url` must re-trigger the Apify `supplierProfile` lookup against the corrected URL and refresh that Company's `indiamart_*` fields, the same as the original enrichment path in `19-data-enrichment-indiamart`
- **Never charge for a correction-triggered re-fetch or rescore** — an IndiaMART URL correction's re-fetch and a post-correction rescore are both free by design (no `billing.charge_for_action` call at all), so never call `billing.refund_action` for either on a failure path either — refund_action always decrements `FreeActionAllowance.used_count`, which would be wrong for an action that never incremented it in the first place
- **Never let a rescore happen without a `FieldCorrection` that postdates the card's `scored_at`** — scoring stays one-shot by default; a correction is what unlocks exactly one free rescore, re-checked inside `score_card_task` itself (not just the router/service gate), so two racing enqueues can't both slip through
- **Never accept a field correction whose `corrected_value` equals the field's current value** — since no correction is billed, an identical resubmission is otherwise a free way to spam-unlock rescores or (for the IndiaMART URL) spam-trigger a paid Apify re-fetch for zero actual change; reject it before writing anything. The IndiaMART URL additionally carries a short per-user cooldown, since the no-op check alone doesn't stop cycling between two distinct real URLs — a cheap anti-abuse throttle, not a comprehensive rate limiter, now that billing no longer serves that role for this action
- **Never let the "raise a query" flow depend on a client-side mailto or an unaudited email call** — ticket id issuance and the email to info@dashrtech.com must happen server-side in `apps/api` so every query is durably stored and provably sent, unlike the plain mailto link already used elsewhere on the FAQ page
- This file describes the target architecture for a repo that currently has no application code — when code starts landing, keep this file in sync with what's actually built rather than what was planned
