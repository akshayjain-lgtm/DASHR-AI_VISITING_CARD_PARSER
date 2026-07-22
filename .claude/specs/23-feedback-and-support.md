# Spec: Feedback & Support

## Overview
A Feedback page inside the authenticated app shell, reached via its own
sidebar nav item positioned just below Settings and FAQ — not a CTA
embedded in the FAQ page. It has two independent sections: (1) open-ended
product feedback ("what's good about this tool" / "what went wrong"),
persisted to a `Feedback` table so the team can mine it later to improve
extraction/enrichment/scoring — no in-app review UI ships in this step,
just durable storage; and (2) a "raise a query" support form that persists
a `SupportQuery` row, assigns it a human-readable ticket id, and sends a
server-side email with the full query details to info@dashrtech.com. This
sits alongside review/export as a post-launch support surface — it does
not touch extraction, enrichment, scoring, or billing logic.

The repo already has a public, unauthenticated enquiry flow at
`apps/web/app/(marketing)/contact/page.tsx` → `POST /contact` →
`ContactEmailProvider`, which emails info@dashrtech.com but never persists
the enquiry and never returns a ticket id. This spec follows the same
`Protocol` + `Console*Provider` + `deps.py` wiring convention for its own
new provider, but is not a reuse of `/contact`: this page is inside the
logged-in shell (so submissions carry `user_id`/`org_id`), and its query
path must persist a row and mint a ticket id, which `/contact` deliberately
does not.

## Depends on
- `03-user-login-logout` — the Feedback page lives behind the same session
  auth as Dashboard/Cardex/Wallet/Settings/FAQ (`get_current_user`).
- `18-faq` — establishes the sidebar `NAV` array and the
  `(marketing)`-group-but-also-sidebar-linked pattern this step's sidebar
  entry follows (though this page itself is not a marketing route).

## API endpoints (apps/api)
All endpoints require a valid session (`get_current_user`) — this is an
authenticated-app feature, not a public marketing form.

- `POST /feedback` — submit open-ended product feedback — org-authenticated
  — request: `{ what_worked: str | None, what_went_wrong: str | None }`
  (at least one of the two must be non-empty) — response: `204 No Content`.
  Writes one `Feedback` row with `user_id`/`org_id` from the session; no
  email is sent.
- `POST /feedback/queries` — raise a support query — org-authenticated —
  request: `{ subject: str, message: str }` — response:
  `{ ticket_id: str, created_at: datetime }`. Writes one `SupportQuery`
  row (`user_id`/`org_id` from the session, status `open`), assigns the
  ticket id from a Postgres sequence, and sends the email to
  info@dashrtech.com via `SupportQueryEmailProvider` in the same request
  (email failure still leaves the row persisted — see Rules below).

## Frontend surface (apps/web)
- **New pages/components**:
  - `apps/web/app/feedback/page.tsx` — the Feedback page. Uses `Sidebar`
    (`active="feedback"`), matching the layout of `app/settings/page.tsx`
    and `app/wallet/page.tsx`. Two stacked sections on one page (no tabs,
    both are short forms):
    - **Feedback**: two textareas — "What's working well?" and "What went
      wrong?" — at least one required, submit via `POST /feedback`, then
      show an inline "Thanks — noted" confirmation and clear the form
      (mirrors the submitted-state pattern in
      `app/(marketing)/contact/page.tsx`).
    - **Raise a query**: subject + message fields, submit via
      `POST /feedback/queries`, then replace the form with the returned
      ticket id (e.g. "Query submitted — reference **DASHR-TKT-000042**.
      We've emailed your team.") and a "Submit another query" reset,
      following the same submitted/error/loading state shape as
      `app/(marketing)/contact/page.tsx`.
- **Modified pages/components**:
  - `apps/web/components/sidebar.tsx` — add
    `{ id: "feedback", label: "Feedback", icon: MessageSquare, path: "/feedback" }`
    to `NAV`, positioned immediately after the `"faq"` entry (i.e. just
    below Settings and FAQ, per CLAUDE.md).
  - `apps/web/lib/api.ts` — add `submitFeedback(data)` and
    `submitSupportQuery(data)` client functions plus their response types
    (`SupportQueryOut`), following the existing `submitContactEnquiry`
    shape (`request()` wrapper, `ApiError` on failure).

## Database changes
Two new tables, both org-scoped per CLAUDE.md's rule that every non-global
row carries `org_id` even where (as with Wallet/Invoice) billing/visibility
scope is the User, not the Org. Both mirror `Invoice`'s pattern: `org_id`
denormalized from `User.org_id` at write time (nullable, `ON DELETE SET
NULL`, matching `User.org_id`'s own nullability), with `user_id` as the
non-nullable FK owning the row.

- **`feedback`**
  - `feedback_id` — UUID PK, `gen_random_uuid()`
  - `user_id` — UUID, FK → `users.user_id`, not null
  - `org_id` — UUID, FK → `organizations.org_id` (`ON DELETE SET NULL`),
    nullable
  - `what_worked` — text, nullable
  - `what_went_wrong` — text, nullable
  - `created_at` — timestamptz, `server_default=now()`
  - `CHECK` constraint: `what_worked IS NOT NULL OR what_went_wrong IS NOT NULL`
    (reject an all-blank submission at the DB level, not just in the
    Pydantic schema)
  - Index on `(user_id, created_at)` for any future per-user review tooling

- **`support_queries`**
  - `support_query_id` — UUID PK, `gen_random_uuid()`
  - `user_id` — UUID, FK → `users.user_id`, not null
  - `org_id` — UUID, FK → `organizations.org_id` (`ON DELETE SET NULL`),
    nullable
  - `ticket_id` — text, unique, not null (e.g. `DASHR-TKT-000042`)
  - `subject` — text, not null
  - `message` — text, not null
  - `status` — text, `server_default='open'`, `CHECK (status IN ('open', 'closed'))`
    (no UI to close a ticket in this step; the column exists so a later
    admin-facing step doesn't need a migration to add it)
  - `email_sent` — boolean, `server_default=false` (set true only after
    `SupportQueryEmailProvider.send` returns without raising — lets a
    later ops step find queries whose notification email failed and needs
    a resend, without re-deriving that from logs)
  - `created_at` — timestamptz, `server_default=now()`
  - Index on `(user_id, created_at)`

- New Postgres sequence `support_query_ticket_seq`, `nextval()`'d inside
  `feedback_service.py` and formatted as `f"DASHR-TKT-{sequence_value:06d}"`,
  mirroring `invoicing.py`'s `invoice_number_seq` / `DASHR-INV-{n:06d}`
  pattern exactly (same race-safety rationale: a DB sequence, not a
  `SELECT COUNT(*)`, is what makes concurrent ticket creation collision-free).

New Alembic migration: `apps/api/migrations/versions/0024_feedback_and_support.py`
(next sequential number after `0023_lead_scoring_v2_product_fit_geocoding.py`),
creating both tables, the sequence, and registering `Feedback`/`SupportQuery`
in `apps/api/app/models/__init__.py`.

## Background jobs
No background job changes. Both submissions are small, single-row writes
plus one outbound email — synchronous in the request handler is correct
here (CLAUDE.md's "never block on OCR/enrichment" rule is about bulk card
processing, not a two-field form post). If a real email provider is later
found to be slow/unreliable enough to need retries, that's a follow-up, not
part of this step.

## Files to change
- `apps/api/app/main.py` — register the new `feedback` router
- `apps/api/app/deps.py` — add `get_support_query_email_provider()`,
  mirroring `get_contact_email_provider()`/`get_invite_email_provider()`
  (raises in production until a real provider is wired in)
- `apps/api/app/models/__init__.py` — export `Feedback`, `SupportQuery`
- `apps/web/components/sidebar.tsx` — add the Feedback `NAV` entry
- `apps/web/lib/api.ts` — add `submitFeedback`, `submitSupportQuery`,
  `SupportQueryOut`

## Files to create
- `apps/api/migrations/versions/0024_feedback_and_support.py`
- `apps/api/app/models/feedback.py` — `Feedback` model
- `apps/api/app/models/support_query.py` — `SupportQuery` model
- `apps/api/app/schemas/feedback.py` — `FeedbackCreate`, `SupportQueryCreate`, `SupportQueryOut`
- `apps/api/app/services/feedback_service.py` — validation (reject
  all-blank feedback below the DB CHECK too, so the API returns a clean
  422 instead of a 500 from the constraint), row creation, ticket id
  minting, and calling the email provider
- `apps/api/app/services/support_query_email_provider.py` — `Protocol` +
  `ConsoleSupportQueryEmailProvider`, mirroring
  `contact_email_provider.py`/`invite_email_provider.py` exactly (dev-only
  console logger; `deps.py` refuses to hand it out when
  `settings.environment == "production"`)
- `apps/api/app/routers/feedback.py` — `POST /feedback`, `POST /feedback/queries`
- `apps/web/app/feedback/page.tsx` — the Feedback page

## New dependencies
No new dependencies.

## Rules for implementation
- Every query and insert on `feedback`/`support_queries` filters/sets
  `org_id` from the authenticated user's session — never trust an
  `org_id` from the request body (there isn't one in these schemas; don't
  add one)
- No raw SQL string interpolation — SQLAlchemy query builder or bound
  params only (the sequence `nextval` call is the one exception, and it
  takes no user input, mirroring `invoicing.py`)
- Business logic (validation, ticket id minting, email dispatch) lives in
  `feedback_service.py`, not in `routers/feedback.py`
- `POST /feedback/queries` must still return the ticket id and leave the
  `SupportQuery` row committed even if `SupportQueryEmailProvider.send`
  raises — log the failure, leave `email_sent=false`, and let the user
  keep their reference number rather than losing the whole submission
  because outbound email hiccupped. Do not swallow the exception so
  silently that nothing is logged.
- `ConsoleSupportQueryEmailProvider` must never be reachable when
  `settings.environment == "production"`, exactly like
  `ConsoleContactEmailProvider`/`ConsoleInviteEmailProvider` — fail loudly
  in `deps.py`, don't silently no-op
- Reject an all-blank `Feedback` submission (`what_worked` and
  `what_went_wrong` both empty/whitespace-only) in `feedback_service.py`
  with a 422 before it ever reaches the DB CHECK constraint
- API contracts are Pydantic models (`schemas/feedback.py`) — no TS type
  hand-duplication beyond the thin request/response shapes already used
  elsewhere in `lib/api.ts`
- The Feedback page requires an active session like every other sidebar
  page (Dashboard/Cardex/Wallet/Settings/FAQ) — do not add it to any
  public/unauthenticated route group, and do not special-case it to work
  logged-out

## Definition of done
- [ ] A logged-in user sees "Feedback" in the sidebar, positioned directly
      below "FAQ", and it navigates to `/feedback`
- [ ] Submitting only "What went wrong?" (leaving "What's working well?"
      blank) succeeds; submitting both blank is rejected client-side and
      server-side (422)
- [ ] A submitted Feedback entry appears as a new row in the `feedback`
      table with the correct `user_id`/`org_id` and no email is sent for it
- [ ] Submitting a query returns a ticket id in the form `DASHR-TKT-XXXXXX`
      and displays it in the UI; the `support_queries` table has a matching
      row with `email_sent=true` and the console log shows the email
      content (dev provider)
- [ ] Two queries raised back-to-back get distinct, sequential ticket ids
      (no collision under rapid submission)
- [ ] Logging out and visiting `/feedback` directly redirects to login
      (same behavior as `/settings`, `/wallet`, etc.)
- [ ] A sub-user's `Feedback`/`SupportQuery` rows carry that sub-user's own
      `user_id`, not their org admin's
- [ ] `ConsoleSupportQueryEmailProvider` cannot be resolved when
      `settings.environment == "production"` (raises, per the existing
      `get_contact_email_provider` test pattern if one exists, or a new
      unit test mirroring it)
