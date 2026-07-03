# CLAUDE.md

## Project overview

DASHR AI is a modern B2B SaaS platform for industrial and manufacturing sellers. It scans visiting cards collected in bulk at trade exhibitions, extracts structured contact data via AI/OCR, enriches each contact with public company data (firmographics, industry classification, size, revenue signals), and scores each resulting lead for product-fit against the seller's target customer profile.

Core workflows:
1. **Bulk capture** — upload/photograph dozens–hundreds of cards per exhibition in one batch
2. **Extraction** — AI vision parses each card image into structured fields (name, title, company, email, phone, address)
3. **Enrichment** — cross-reference extracted company names against public data sources to attach firmographics (industry/NAICS code, employee count, revenue band, location)
4. **Scoring** — rank each lead against a configurable industrial/manufacturing product-fit model
5. **Review & export** — sales reps triage a scored lead list per exhibition and push qualified leads to CRM

This is a greenfield repo — no application code exists yet. This file defines the target architecture new work should scaffold toward, not a description of code that already exists. Treat structural claims below as the plan, not as verified fact, until the corresponding code lands.

---

## Architecture

Two-service architecture: a Next.js frontend/BFF for the SaaS UI and auth, and a Python backend that owns all AI/enrichment/scoring logic and the database. Splitting this way keeps the OCR/enrichment/scoring pipeline (Python's ML/data ecosystem) decoupled from the dashboard UI (React/Next's strength), and lets bulk-processing jobs scale independently of the web tier.

```
dashr-ai/
├── apps/
│   ├── web/                      # Next.js 14 (App Router, TypeScript) — SaaS frontend + BFF
│   │   ├── app/                  # Routes: dashboard, upload, leads, exhibitions, settings
│   │   ├── components/           # UI components (shadcn/ui + Tailwind)
│   │   ├── lib/                  # API client for backend, auth helpers
│   │   └── package.json
│   │
│   └── api/                      # FastAPI (Python) — business logic, owns Postgres
│       ├── app/
│       │   ├── routers/          # REST endpoints: cards, leads, exhibitions, orgs
│       │   ├── services/
│       │   │   ├── ocr.py            # Card image → structured fields (vision LLM)
│       │   │   ├── enrichment.py     # Company name → firmographics lookup
│       │   │   └── scoring.py        # Firmographics + role → product-fit score
│       │   ├── models/           # SQLAlchemy models (multi-tenant, org-scoped)
│       │   ├── workers/          # Celery tasks for async batch processing
│       │   └── db/               # Session management, Alembic migrations
│       └── requirements.txt
│
├── packages/
│   └── shared-types/             # OpenAPI-generated TS types shared by web ↔ api
│
├── infra/
│   ├── docker-compose.yml        # Local dev: web, api, worker, postgres, redis
│   └── migrations/
│
└── README.md
```

**Where things belong:**
- UI, routing, session/auth flows → `apps/web`
- Anything touching the database, OCR, enrichment, or scoring → `apps/api`, never in Next.js API routes beyond thin passthroughs
- Long-running or bulk work (batch card processing, enrichment lookups) → Celery tasks in `apps/api/app/workers`, never inline in a request handler
- New scoring criteria/weights → `apps/api/app/services/scoring.py`, kept as configurable data, not hardcoded branches

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Frontend | Next.js 14 (App Router) + TypeScript + Tailwind CSS + shadcn/ui | Fast to build a polished SaaS dashboard; server components suit data-heavy lead tables |
| Backend API | FastAPI (Python 3.11+) | Async I/O for enrichment API calls; same language as the AI/OCR pipeline |
| Card OCR/extraction | Vision-capable LLM (Claude with vision) via API, with a deterministic field validator/normalizer pass | Handwriting, varied layouts, and multiple languages on cards need a model, not template OCR; validator catches hallucinated fields before they hit the DB |
| Company enrichment | Third-party firmographics API (e.g. Clearbit/Crunchbase-style provider) + NAICS/SIC industry classification, cached in Postgres | Public company data changes slowly — cache aggressively, don't re-fetch per lookup |
| Lead scoring | Rules-based weighted scoring service in `apps/api`, tuned for industrial/manufacturing fit (industry code, company size, revenue band, buyer title/seniority) | Sellers need to see *why* a lead scored the way it did — an explainable rules engine beats an opaque ML model here |
| Database | PostgreSQL via SQLAlchemy + Alembic | Relational integrity for org/lead/company relationships; strong multi-tenant row-level scoping |
| Object storage | S3-compatible bucket (card images) | Card images are large binary blobs — never store in Postgres |
| Async jobs/queue | Celery + Redis | Bulk uploads (hundreds of cards) must process in the background, not block the request |
| Auth | Org-based multi-tenant auth (Auth.js/NextAuth or Clerk), JWT passed to FastAPI | B2B SaaS — every request is scoped to an organization, not just a user |
| Infra | Docker Compose (local), containers on AWS/GCP (prod) | Standard, portable, no vendor lock-in for a small early-stage service |
| CI/CD | GitHub Actions | Matches the existing GitHub-hosted repo and installed GitHub plugin |

---

## Code style

- TypeScript (web): strict mode on, no `any` without justification, functional components only
- Python (api): PEP 8, type hints required on all function signatures, snake_case
- API contracts: FastAPI Pydantic models are the source of truth; TS types in `packages/shared-types` are generated from them, never hand-duplicated
- DB access: always through SQLAlchemy models/queries in `apps/api/app/db`, never raw SQL strings in routers or services
- Every DB row that isn't a global/reference table must carry an `org_id` and every query must filter on it — this is a multi-tenant app, cross-tenant leaks are a security bug, not a style nit

---

## Data model essentials

- **Organization** — the tenant; every other table is scoped to one
- **Exhibition** — a trade show/batch upload event (name, date, location)
- **Card** — one scanned image + raw OCR output + extraction confidence
- **Contact** — normalized person fields parsed from a Card (name, title, email, phone)
- **Company** — enriched firmographics, keyed by normalized company name/domain, shared across orgs where public data allows (cache once, reuse)
- **Lead** — the join of Contact + Company + a computed product-fit Score, scoped to one Organization and Exhibition

---

## Subagent Policy
- Always use a builtin explore subagent for codebase exploration before implementing any new feature
- Always use a subagent to verify test results after any implementation
- When asked to plan, delegate codebase research to a subagent before presenting the plan
- Always use the builtin plan subagent in plan mode

---

## Warnings and things to avoid

- **Never skip the `org_id` filter** on a query — this is a multi-tenant SaaS; a missing tenant scope is a data leak, not a bug to fix "later"
- **Never store card images in Postgres** — object storage only, DB holds the URL/key
- **Never call enrichment providers per-request without checking the Company cache first** — these APIs are rate-limited and billed per lookup
- **Never hardcode scoring weights inline in a route handler** — scoring criteria must live in `scoring.py` as data so they can be tuned per-org later without a redeploy
- **Never block a request on OCR or enrichment for bulk uploads** — batch card processing is async via Celery; the upload endpoint only enqueues work
- **Never trust raw vision-LLM output as final** — always run extracted fields through the validator/normalizer before persisting (catches malformed emails, missing required fields, obvious hallucinations)
- This file describes the target architecture for a repo that currently has no application code — when code starts landing, keep this file in sync with what's actually built rather than what was planned
