# Spec: Database Setup

## Overview
Establishes the core relational schema for DASHR AI: organizations, users (optionally org-scoped), each user's own seller/product profile (used to calibrate lead scoring), prospect companies (with enrichment), visiting cards parsed from scanned contacts — exhibition-linked or general/ad-hoc — their multi-valued phone/email child tables, exhibitions, and a company-enrichment audit trail. This is the data-layer foundation every later step (extraction, enrichment, scoring, review/export) writes into and reads from.

## Depends on
Nothing — this is the first step.

## API endpoints (apps/api)
No new endpoints.

## Frontend surface (apps/web)
No frontend changes.

## Database changes

Nine tables, Postgres, `gen_random_uuid()` (pgcrypto) PKs.

> **Deviation from CLAUDE.md flagged, resolved**: CLAUDE.md's target data model scopes every table to an `Organization` tenant via a mandatory `org_id`. This step adds `organizations`, but membership is **optional by design** — a user can sign up and parse visiting cards without ever belonging to an org (solo/freelance sellers, or before they've been invited to a team). So `users.org_id` is nullable, and `visiting_cards` / `exhibitions` / `seller_profiles` stay scoped to `user_id`, not `org_id`. The visibility question this raised is now resolved — see "Org visibility model" below: **exactly one admin per org**, admin sees every member's cards, members see only their own. This is enforced via `users.role` + a DB constraint, not by adding `org_id` to `visiting_cards`.

### 1. `organizations`
| Column | Type | Notes |
|---|---|---|
| org_id | UUID | PK, `gen_random_uuid()` |
| name | TEXT | not null |
| created_at | TIMESTAMPTZ | default `now()` |

### 2. `users` *(pre-existing shape, extended with optional org membership + role)*
| Column | Type | Notes |
|---|---|---|
| user_id | UUID | PK, `gen_random_uuid()` |
| org_id | FK → organizations | **nullable** — a user may not belong to an org |
| role | TEXT | `admin` / `member`, nullable (only meaningful when `org_id` is set) |
| created_at | TIMESTAMPTZ | default `now()` |
| name | TEXT | |
| email | TEXT | UNIQUE, not null |
| phone_no | TEXT | |
| password_hash | TEXT | |

`org_id` FK is `ON DELETE SET NULL` — deleting an organization must never delete its users or their cards.

Constraints on `role`:
- `CHECK (role IS NULL OR role IN ('admin', 'member'))`
- `CHECK (role <> 'admin' OR org_id IS NOT NULL)` — can't be an org admin without an org
- **Partial unique index** on `org_id` where `role = 'admin'` — at most one admin per organization

### Org visibility model
- Every organization has **exactly one admin** (enforced by the partial unique index above) and any number of `member` users.
- The **admin sees every visiting card scanned by any user in their org** — derived via `visiting_cards.user_id → users.user_id → users.org_id`, no new column needed on `visiting_cards`.
- A **member sees only their own cards** (`visiting_cards.user_id = self`), never a teammate's, even within the same org.
- This is an **API-layer authorization rule**, not a DB constraint: the query a request runs depends on the caller's role.
  - Admin request: `SELECT * FROM visiting_cards WHERE user_id IN (SELECT user_id FROM users WHERE org_id = :caller_org_id)`
  - Member request: `SELECT * FROM visiting_cards WHERE user_id = :caller_user_id`
  - Org-less user: same as member, scoped to `user_id = :caller_user_id` (there's no org to escalate from)

### 3. `seller_profiles`
One per user who has completed onboarding. This is the seller's *own* company/product info DASHR AI scores incoming leads against — distinct from `companies`, which holds *prospect* firmographics.

| Column | Type | Notes |
|---|---|---|
| profile_id | UUID | PK |
| user_id | FK → users | UNIQUE, not null — one profile per user |
| company_name | TEXT | the seller's own company |
| industry | TEXT | seller's industry/sector |
| product_lines | TEXT | free text description of what they sell |
| last_year_revenue | NUMERIC | |
| revenue_currency | TEXT | default `'INR'` |
| target_customer_description | TEXT | ideal buyer description, free text |
| target_regions | TEXT | free text, e.g. "Pan India, Middle East" |
| created_at | TIMESTAMPTZ | default `now()` |
| updated_at | TIMESTAMPTZ | default `now()`, updated on write |

### 4. `companies`
Unique at company level. Dedup key: `domain` (preferred), or `normalized_name` (fallback). **Free/personal email domains (gmail.com, yahoo.com, outlook.com, hotmail.com, etc.) are never used as a dedup key** — cards with those domains skip straight to `normalized_name` matching, since the domain doesn't identify a company.

| Column | Type | Notes |
|---|---|---|
| company_id | UUID | PK |
| name | TEXT | as-entered display name |
| normalized_name | TEXT | lowercase, trimmed, suffixes stripped (Pvt Ltd, Inc, LLC) |
| domain | TEXT | UNIQUE, nullable |
| website | TEXT | |
| industry | TEXT | enriched |
| size_bucket | TEXT | enriched (e.g. 1-10, 11-50, 51-200) |
| hq_city | TEXT | enriched |
| hq_country | TEXT | enriched |
| linkedin_url | TEXT | enriched |
| enrichment_status | TEXT | `pending` / `done` / `failed`, default `pending` |
| enriched_at | TIMESTAMPTZ | |
| created_at | TIMESTAMPTZ | default `now()` |

Indexes: unique partial index on `domain` where not null; btree on `normalized_name`.

### 5. `exhibitions`
| Column | Type | Notes |
|---|---|---|
| exhibition_id | UUID | PK |
| name | TEXT | |
| location | TEXT | |
| start_date | DATE | |
| end_date | DATE | |
| user_id | FK → users | |

### 6. `visiting_cards`
Unique at card level. One scanned card = one row. **`exhibition_id` is nullable on purpose** — cards are frequently collected outside any trade show (a general meeting, a walk-in, a referral), and those must parse and score exactly like exhibition cards, just with no exhibition attribution.

| Column | Type | Notes |
|---|---|---|
| card_id | UUID | PK |
| user_id | FK → users | owner/scanner, not null |
| company_id | FK → companies | nullable until resolved |
| exhibition_id | FK → exhibitions | **nullable** — NULL means "general capture", not an error |
| full_name | TEXT | |
| job_title | TEXT | |
| designation_level | TEXT | derived: C-level / manager / staff |
| raw_ocr_text | TEXT | full raw parse output, for reprocessing |
| image_url | TEXT | stored card image path (object storage key, not the blob) |
| special_remark | TEXT | handwritten/marginal note captured off the card (e.g. "met at booth, follow up re: pricing"); nullable, used as a scoring signal |
| lead_score | NUMERIC | |
| score_breakdown | JSONB | see below |
| scored_at | TIMESTAMPTZ | |
| status | TEXT | `new` / `enriched` / `scored` / `exported`, default `new` |
| created_at | TIMESTAMPTZ | default `now()` |

`score_breakdown` JSONB shape (draft, versioned so historical rows survive scoring-logic changes):
```json
{
  "designation_score": 25,
  "company_size_score": 15,
  "industry_fit_score": 20,
  "engagement_score": 10,
  "remark_signal_score": 5,
  "total": 75,
  "version": "v1"
}
```
`remark_signal_score` added to the draft so `special_remark` has a place to land once scoring is built — the scoring service itself is a later step.

### 7. `card_phones`
| Column | Type | Notes |
|---|---|---|
| phone_id | UUID | PK |
| card_id | FK → visiting_cards | `ON DELETE CASCADE` |
| phone_e164 | TEXT | normalized `+91XXXXXXXXXX` |
| phone_raw | TEXT | as printed |
| phone_type | TEXT | `mobile` / `office` / `fax` |
| is_primary | BOOLEAN | default `false` |

Unique: `(card_id, phone_e164)`

### 8. `card_emails`
| Column | Type | Notes |
|---|---|---|
| email_id | UUID | PK |
| card_id | FK → visiting_cards | `ON DELETE CASCADE` |
| email | TEXT | lowercased |
| email_type | TEXT | `work` / `personal` |
| is_primary | BOOLEAN | default `false` |

Unique: `(card_id, email)`

### 9. `company_enrichment` (audit trail)
| Column | Type | Notes |
|---|---|---|
| enrichment_id | UUID | PK |
| company_id | FK → companies | not null |
| source | TEXT | e.g. `clearbit`, `linkedin`, `manual` |
| payload | JSONB | |
| fetched_at | TIMESTAMPTZ | default `now()` |

### Relationships
```
organizations (1) ──< users (M)                [org_id nullable — users can exist org-less]
users (1) ──< visiting_cards (M) >── (1) companies
users (1) ──1 seller_profiles (1)               [one profile per user]
visiting_cards (1) ──< card_phones (M)
visiting_cards (1) ──< card_emails (M)
visiting_cards (M) >── (0..1) exhibitions        [nullable — general captures have none]
companies (1) ──< company_enrichment (M)
```

### Company resolution flow
1. Parse email domain from `card_emails`.
2. If the domain is a known free/personal provider (gmail.com, yahoo.com, outlook.com, hotmail.com, icloud.com, ...), **skip domain matching** and go straight to step 3.
3. Otherwise, match `companies.domain` on that domain.
4. If no match, normalize company name → match `companies.normalized_name`.
5. If no match, create new `companies` row, `enrichment_status = pending`.
6. Async job (Celery, later step) enriches new/pending companies from public sources.
7. Re-run `visiting_cards` scoring once `company_id` is resolved and enriched, folding in `seller_profiles` (for fit) and `special_remark` (as a scoring signal) alongside firmographics.

## Background jobs
No background job changes — the resolution/enrichment flow above is documented for context but its Celery task lands in a later step. This step is schema only.

## Files to change
- `apps/api/app/models/__init__.py` — register `Organization`, `SellerProfile`
- `apps/api/app/models/user.py` — add `org_id`, `role`
- `apps/api/app/models/visiting_card.py` — add `special_remark`

## Files to create
- `apps/api/app/models/organization.py`
- `apps/api/app/models/seller_profile.py`
- `apps/api/migrations/versions/0002_org_seller_profile_remark.py`
- `apps/api/migrations/versions/0003_user_role_admin_constraint.py`

*(Everything from the prior revision of this spec — `requirements.txt`, `alembic.ini`, `app/db/`, the other model files, `migrations/env.py`, `migrations/versions/0001_initial_schema.py`, `infra/docker-compose.yml` — already exists from step 1 and is unchanged here.)*

## New dependencies
No new dependencies.

## Rules for implementation
- UUID primary keys via Postgres `gen_random_uuid()` (already enabled via `pgcrypto` in migration 0001)
- No raw SQL string interpolation — SQLAlchemy query builder / Alembic `op.*` only
- `users.org_id` is nullable and `ON DELETE SET NULL` — an org can be deleted without touching its users, their cards, or their profiles
- `seller_profiles.user_id` is unique — enforce one profile per user at the DB level, not just in application code
- `visiting_cards.exhibition_id` stays nullable — never make it required; general (non-exhibition) capture is a first-class case, not an edge case
- `companies` dedup logic (free-email-domain skip) is a rule for the resolution *service*, built in a later step — this step only needs `companies`/`card_emails` shaped so that service can be written; no schema change required to support it
- Models are declarative SQLAlchemy 2.0 style (`Mapped[...]`, `mapped_column`), not the legacy `Column(...)` style
- New migrations (`0002_...`, `0003_...`) build on `0001`/`0002` in order — do not edit an already-applied migration
- The one-admin-per-org rule is enforced with a **partial unique index**, not application code alone — a race between two "make me admin" requests must still fail at the DB level
- Card-visibility scoping (admin-sees-org, member-sees-self) is implemented in `apps/api/app/services` query helpers, never duplicated ad hoc per router

## Definition of done
- [ ] `alembic upgrade head` runs clean from a database already at revision `0001`
- [ ] `organizations` and `seller_profiles` tables exist with correct columns/constraints
- [ ] `users.org_id` exists, is nullable, and is `ON DELETE SET NULL`
- [ ] `users.role` exists, nullable, constrained to `admin`/`member`/`NULL`
- [ ] Inserting a `users` row with `org_id = NULL` succeeds
- [ ] Deleting an `organizations` row with users attached succeeds and leaves those users' `org_id` set to `NULL` (not deleted)
- [ ] `seller_profiles.user_id` unique constraint rejects a second profile for the same user
- [ ] Inserting a `visiting_cards` row with `exhibition_id = NULL` succeeds (general capture)
- [ ] `visiting_cards.special_remark` accepts free text and is nullable
- [ ] Setting a second user's `role = 'admin'` for an org that already has an admin fails (partial unique index)
- [ ] Setting `role = 'admin'` with `org_id = NULL` fails (check constraint)
- [ ] A query scoped to the admin's org returns cards from multiple member users; a query scoped to a member returns only that member's own cards
- [ ] `alembic downgrade -1` cleanly removes only the latest revision's changes, leaving prior revisions' tables/columns intact