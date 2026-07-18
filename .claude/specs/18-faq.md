# Spec: FAQ

## Overview
A public, unauthenticated FAQ page that answers common prospect/customer
questions about how DASHR AI works end-to-end — bulk card capture,
AI extraction, company enrichment, lead scoring, review/export, and the
per-user prepaid wallet/free-tier billing model. It exists alongside the
homepage as a static marketing surface: prospects evaluating the product
and existing users with billing questions both land here before ever
signing in. This is a content/navigation feature only — it does not touch
extraction, enrichment, scoring, or billing logic, 
only documents it.

## Depends on
None. This is a standalone static marketing page and can be built
independently of every other step. (It documents behavior from the wallet
recharge/usage steps — 14-wallet-recharge and 15-wallet-usage — so its FAQ
copy should stay consistent with what those already shipped, but no code
from those steps is required to build this page.)

## API endpoints (apps/api)
No new endpoints. FAQ content is static and rendered entirely in
`apps/web`; it does not read from or write to the database.

## Frontend surface (apps/web)
- **New pages/components**:
  - `apps/web/app/(marketing)/faq/page.tsx` — the FAQ page: a page header,
    a set of grouped, expand/collapse question-and-answer entries (an
    accordion), and a closing CTA pointing to `/product` (demo) and a
    support mailto link, matching the visual language of the homepage
    (`apps/web/app/page.tsx`) and using the existing `Navbar`, `DashrLogo`,
    and `OBtn`/`GBtn` components. No client-side data fetching — content is
    a static array of `{ category, question, answer }` entries defined in
    the page file, grouped into sections: Getting Started, Extraction &
    Enrichment, Lead Scoring, Wallet & Billing, Team & Roles.
  - `apps/web/components/faq-accordion.tsx` — a small reusable
    expand/collapse client component (single-open or multi-open list of
    Q&A items) used by the FAQ page. Pure presentational component, no
    props beyond the items array and no data fetching.
- **Modified pages/components**:
  - `apps/web/components/navbar.tsx` — add an "FAQ" link (routes to
    `/faq`) alongside the existing Product/Pricing/Login links.
  - `apps/web/app/page.tsx` — add an "FAQ" link to the footer link row
    (next to Product/Pricing/Login), matching the existing footer button
    style.
  - `apps/web/components/sidebar.tsx` — add "FAQ" as its own top-level nav
    item in the authenticated app shell's `NAV` array (routes to `/faq`),
    alongside — not nested inside — the existing Wallet and Settings items.
    This is the authenticated entry point; `/settings` itself carries no
    FAQ link.

If Privacy Policy / Terms of Use pages (referenced in CLAUDE.md but not
yet built) land before this step, this FAQ page should live in the same
`(marketing)` route group as those — confirmed no such pages/group exist
yet in this repo, so this spec creates the `(marketing)` group for the
first time.

## Database changes
No database changes. FAQ content is static and does not need a table —
there is no admin editing workflow for FAQ content in this spec.

## Background jobs
No background job changes.

## Files to change
- `apps/web/components/navbar.tsx` — add FAQ nav link
- `apps/web/app/page.tsx` — add FAQ footer link
- `apps/web/components/sidebar.tsx` — add FAQ as its own top-level nav item, alongside Wallet and Settings

## Files to create
- `apps/web/app/(marketing)/faq/page.tsx`
- `apps/web/components/faq-accordion.tsx`

## New dependencies
No new dependencies. Use `lucide-react` (already installed) for the
expand/collapse chevron icon, matching icon usage elsewhere in
`apps/web`.

## Rules for implementation
- This page is public — no auth check, no session/org lookup, no
  redirect-if-unauthenticated logic (same treatment as `/product`)
- No new API calls from this page — all content is a local static array,
  no `fetch`/API client usage
- Do not duplicate billing/free-tier/scoring logic here — FAQ copy may
  *describe* the wallet/free-tier/scoring model in CLAUDE.md, but must
  never encode it as executable logic (e.g. no computing a live free-tier
  count or wallet balance on this page)
- Match existing visual system exactly: `#E65527` accent color, black/white
  palette, `font-black`/`font-bold` weight conventions, `max-w-6xl` content
  width, and the section-divider pattern already used in
  `apps/web/app/page.tsx` — do not introduce a new color or new component
  library
- `faq-accordion.tsx` stays a dumb presentational component: items in,
  expand/collapse state out — no business logic, no data fetching
- Keep the FAQ content itself factually consistent with the billing/wallet
  rules in CLAUDE.md (per-user wallet, 20 free actions per action type,
  ₹5/₹3/₹2 pricing, no self-serve withdrawal) — if pricing/free-tier
  numbers change later in `apps/api/app/services/billing.py`, this static
  copy will need a follow-up edit, since it is not sourced from
  `PricingRate` at read time
- Keep the FAQ content honest about what the product actually does today —
  never describe a capability (e.g. manually correcting a misread card
  field) that has no corresponding feature in `apps/web`/`apps/api`

## Definition of done
- [ ] Visiting `/faq` while logged out renders the page with no redirect
      and no auth-related network calls
- [ ] Each FAQ category section renders its questions; clicking a question
      expands its answer and clicking again collapses it
- [ ] The homepage navbar and footer both show a working "FAQ" link that
      routes to `/faq`
- [ ] The authenticated app's Sidebar shows "FAQ" as its own top-level nav
      item — alongside Wallet and Settings, not nested inside Settings —
      for every logged-in user, and routes to `/faq`
- [ ] The FAQ page uses `Navbar`, `DashrLogo`, and existing button
      components rather than introducing new nav/logo/button markup
- [ ] `apps/web` builds and type-checks with no new TypeScript errors
      (strict mode, no `any`)
- [ ] No new files touch `apps/api` — confirmed via `git diff --stat`
      against main showing changes scoped to `apps/web` only
