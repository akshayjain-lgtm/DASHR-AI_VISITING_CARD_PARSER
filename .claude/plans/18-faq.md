# Implementation Plan: FAQ (spec 18-faq.md)

## Context
Prospects evaluating DASHR AI and existing users with billing/product questions have no self-serve answer page today — `apps/web/app/page.tsx` (homepage) and `apps/web/app/product/page.tsx` (demo) are the only public pages, and neither explains the wallet/free-tier model, extraction/enrichment/scoring mechanics, or data isolation. Spec `.claude/specs/18-faq.md` (already written, on branch `feature/faq`) scopes a static, unauthenticated FAQ page plus two small entry points: a nav/footer link from the public marketing chrome, and a "Help & FAQ" link on the authenticated `/settings` page (visible to every logged-in user, not just admins — unlike the team-management section already on that page). No backend, no database, no new API calls — content is a static array rendered client-side.

Repo research confirmed: no `(marketing)` route group exists yet (this creates the first one), no "faq" references exist anywhere in the repo, no existing accordion component to reuse (built from scratch), and `apps/web/__tests__/` follows a `<spec-number>-<spec-slug>.test.tsx` convention with `global.fetch` mocked and `next/navigation`'s `useRouter` mocked in every test touching a page that navigates.

Testing scope: this plan covers dev-level verification (`vitest run`) per CLAUDE.md's subagent policy — the user can separately run `/test-feature` or `/verify` for the full QA/browser pipeline.

## Step 1 — FAQ content + accordion component
New file: `apps/web/components/faq-accordion.tsx`
- Exports `type FaqItem = { question: string; answer: string }` and `FaqAccordion({ items }: { items: FaqItem[] })`.
- `"use client"`, single-open accordion via `useState<number | null>(null)`.
- Visual: `border border-black/10 divide-y divide-black/10` wrapper; each row a `<button>` (question + `ChevronDown` from `lucide-react`, rotates 180° and turns `#E65527` when open) with the answer revealed below when `openIndex === i`.
- Pure presentational — no data fetching, no business logic, matching the spec's constraint.

## Step 2 — FAQ page
New file: `apps/web/app/(marketing)/faq/page.tsx` (first route group in the repo; Next.js strips the parens from the URL, so this serves at `/faq`).
- `"use client"`, mirrors `apps/web/app/product/page.tsx`'s structure: `Navbar` + a header section (`max-w-4xl mx-auto px-6`, eyebrow badge + `h1` + subhead, matching homepage's `#E65527`/`font-black` conventions) + category sections + a closing CTA.
- Static `FAQ_CATEGORIES: { category: string; items: FaqItem[] }[]` defined in-file, five categories: **Getting Started**, **Extraction & Enrichment**, **Lead Scoring**, **Wallet & Billing**, **Team & Roles** (explains the Admin/team-member distinction — data-visibility scoping only, never wallet spend authority). Each renders an uppercase category label (`text-[11px] font-black uppercase tracking-wider text-black/40`, matching `settings/page.tsx`'s section headers) above a `FaqAccordion`.
- Wallet & Billing copy stays factually pinned to CLAUDE.md's billing rules (per-user wallet, 20 free actions per action type, ₹5/₹3/₹2 pricing, no self-serve withdrawal, per-recharge invoicing) — this is prose only, never computed/fetched.
- Closing CTA: reuses the homepage's `bg-[#E65527] py-16` banner pattern (`max-w-6xl mx-auto px-6 flex ... justify-between`) with a "Try Demo" button (`router.push("/product")`) and a `mailto:hello@dashr.ai` link — not a full footer clone (no footer exists on `/product` either, so this isn't duplicating an established component, just the CTA-banner idiom already used twice on the homepage).
- No auth check, no session lookup — public like `/product`.

## Step 3 — Wire in navbar and homepage footer
- `apps/web/components/navbar.tsx`: add `<button onClick={() => router.push("/faq")}>FAQ</button>` between the existing "Product" and "Pricing" buttons (same style as its siblings).
- `apps/web/app/page.tsx`: add the equivalent `FAQ` button into the footer's `flex items-center gap-6` link row, in the same relative position (between "Product" and "Pricing").

## Step 4 — Authenticated app entry point: Sidebar, not Settings
`apps/web/components/sidebar.tsx`:
- Add `HelpCircle` to the existing `lucide-react` import.
- Add `{ id: "faq", label: "FAQ", icon: HelpCircle, path: "/faq" }` to the `NAV` array, after `settings`. This makes FAQ its own top-level item — same tier as Wallet and Settings — visible on every authenticated page's sidebar, not nested inside `/settings`. `/settings/page.tsx` itself is untouched.

## Step 5 — Tests
New file: `apps/web/__tests__/18-faq.test.tsx`, following `17-admin-user-management.test.tsx`'s conventions (mock `next/navigation`'s `useRouter`, mock `global.fetch` per-test where a rendered component calls it).
- **FAQ page**: renders all five category headings; clicking a question reveals its answer and clicking again collapses it (accordion behavior); no `fetch` call is made on render (confirms it's static/no auth).
- **Navbar**: clicking "FAQ" calls `router.push("/faq")`.
- **Homepage footer**: clicking "FAQ" calls `router.push("/faq")`.
- **Sidebar**: renders "FAQ" alongside "Wallet" and "Settings", and clicking it calls `router.push("/faq")`.

## Sequencing
Step 1 (accordion) → Step 2 (FAQ page, depends on accordion) → Step 3 + Step 4 (nav entry points, independent of each other) → Step 5 (tests) → manual visual check → Definition of Done pass.

## Verification
- `cd apps/web && npx vitest run 18-faq.test.tsx`
- `cd apps/web && npx tsc --noEmit` (strict mode, no new TS errors)
- `git diff --stat` against `main` — confirm changes are scoped to `apps/web` only (no `apps/api` touched), matching the spec's Definition of Done
- Manual: dev stack up, visit `/faq` logged out (no redirect), click through the accordion, click FAQ from navbar/footer, visit `/settings` logged in and confirm the "Help & FAQ" link appears and routes correctly

## Critical files
- `apps/web/components/faq-accordion.tsx` (new)
- `apps/web/app/(marketing)/faq/page.tsx` (new)
- `apps/web/components/navbar.tsx`, `apps/web/app/page.tsx`, `apps/web/components/sidebar.tsx` (modified)
- `apps/web/__tests__/18-faq.test.tsx` (new)
- `.claude/specs/18-faq.md`, `.claude/plans/18-faq.md`
