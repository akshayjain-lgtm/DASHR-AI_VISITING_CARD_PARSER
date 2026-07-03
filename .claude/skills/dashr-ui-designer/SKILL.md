---
name: dashr-ui-designer
description: Designs and generates modern, production-ready UI for DASHR AI, a B2B SaaS built on Next.js (App Router) + TypeScript + Tailwind CSS + shadcn/ui. Produces clean, data-dense B2B dashboard pages and components - upload flows, lead tables, scoring/enrichment detail views, exhibition summaries - with consistent spacing, restrained color, and Lucide icons. Use this skill whenever the user asks to design, build, create, redesign, improve, or style any DASHR AI page, screen, section, or component - including phrasings like "design the X page", "create UI for X", "build a component for X", "make the X look better", "redesign X", or any request about DASHR AI's frontend, layout, or visual polish - even when DASHR AI isn't named explicitly if the conversation context is clearly about it.
disable-model-invocation: true
---

# DASHR AI UI Designer

You are designing frontend UI for **DASHR AI**, a B2B SaaS platform for industrial/manufacturing sellers. It scans visiting cards collected at trade exhibitions in bulk, enriches contacts with public company data, and scores leads for product-fit. The primary user is a sales rep or sales ops person triaging a batch of scanned leads after a trade show — the UI needs to support fast scanning, sorting, and filtering of dozens-to-hundreds of leads, not just single-record forms.

## What DASHR AI's stack looks like

- **Frontend:** Next.js 14 (App Router), TypeScript, server components by default
- **Styling:** Tailwind CSS + shadcn/ui component primitives — no vanilla CSS, no CSS-in-JS, no Bootstrap
- **Icons:** Lucide (`lucide-react`)
- **Data:** the frontend is a client of the FastAPI backend — assume paginated, filterable list endpoints for leads, not everything loaded client-side

Generate output that fits this stack. Do not introduce Flask/Jinja templates, vanilla JS, or a different component library unless the user explicitly asks for a migration.

## Before you design: check what already exists

If the project's frontend files are available, open the root layout, `tailwind.config`, and one or two existing pages/components before generating anything new. The goal is *consistency* — DASHR AI should feel like one coherent product, not a collage.

Specifically, look for and reuse:
- **Design tokens** (Tailwind theme extensions, CSS variables for color/radius if shadcn's theming is set up)
- **Existing shadcn components already installed** (`Table`, `Card`, `Badge`, `Button`, `Dialog`, etc.) — don't hand-roll a component shadcn already provides
- **The base layout** — sidebar nav? topbar? Follow it.

If you can't see the existing files and the request is non-trivial, ask the user to share a screenshot or paste a relevant page before you generate.

## The DASHR AI design language

When you have no existing reference to follow, default to this. It's a restrained, data-dense B2B SaaS aesthetic — closer to Linear, Retool, or a modern sales/ops tool than a consumer app. Sales reps live in this UI for extended sessions scanning lead lists; it needs to be scannable and fast, not decorative.

**Palette (defaults, override to match existing):**
- Background: very light neutral (`#F7F8FA` / Tailwind `slate-50`)
- Surface (cards, table rows): white with a soft border (`slate-200`) — minimal shadow
- Text: near-black for primary (`slate-900`), muted gray for secondary (`slate-500`)
- Primary accent: a single confident, industrial-leaning color — deep blue or teal (`#2563EB` / `#0F766E`) rather than a consumer-app purple/pink. Pick one and stick with it.
- Score semantics: a 3-tier scale for lead fit — strong fit (green, `#059669`), moderate fit (amber, `#D97706`), weak fit (gray/red only if explicitly "poor fit", `#DC2626`) — score color must be immediately scannable down a long table column

**Spacing:** 4px/8px grid via Tailwind's default scale — don't reach for arbitrary values.

**Radius:** `rounded-lg` (8px) for inputs/buttons, `rounded-xl` (12px) for cards. Tables stay square-cornered inside a rounded container.

**Shadows:** subtle only — `shadow-sm` is usually the ceiling for cards; table rows use borders, not shadows.

**Typography:** system font stack or Inter. Numbers (scores, counts, revenue bands) use `tabular-nums`. Table body text can run smaller (`text-sm`) than a consumer app to fit more rows on screen — density matters more than whitespace here.

**Layout patterns:**
- **Lead tables are the core surface** — sortable columns, right-aligned numeric/score columns, row hover, sticky header for long lists, pagination or virtualized scroll for hundreds of rows
- **Batch upload flow** — drag-and-drop zone for bulk card images, per-card processing status (queued/extracting/enriching/scored/failed), not a single-file-at-a-time form
- **Score as a first-class visual element** — a badge or small bar, not buried in a detail panel, since triage happens at the list level
- **Filters live above the table** — by exhibition, score tier, industry, date — not hidden in a separate settings page
- **Detail views** (single lead) open as a side panel/drawer over the table, not a full page navigation, so reps don't lose their place in the list
- Left-aligned content with clear hierarchy; centered layouts only for empty states and auth
- Forms: label above input, helper text below, error state in red with icon

## Icons: Lucide

```tsx
import { Wallet, TrendingUp, Plus } from "lucide-react"
```

Size: 16px inline with text, 20px in buttons, 24px for section headers. Pick icons that carry meaning for this domain:
- Card scan/upload: `scan-line`, `upload`, `image-plus`
- Lead/contact: `user`, `users`, `contact`
- Company/enrichment: `building-2`, `factory`, `globe`
- Score/fit: `target`, `gauge`, `trending-up`
- Exhibition/batch: `calendar`, `layers`, `package`
- Export/CRM push: `download`, `send`, `external-link`
- Processing status: `loader-2` (spinning, for in-progress), `check-circle-2` (done), `alert-circle` (failed)

Don't sprinkle icons everywhere. One icon per button, one per section heading, one per table row action/status — that's usually the right density.

## Output structure

When fulfilling a design request, structure your response like this:

### 1. Short UI plan (2-5 bullets)
Name the key sections of the page/component and any notable UX decisions. Keep it tight. Example: "Lead review page has a filter bar (exhibition, score tier, industry) above a sortable table with columns: contact, company, title, score badge, status. Row click opens a detail drawer with full enrichment data and raw card image."

### 2. The code
- **Component file(s)** — full TypeScript React, using server components by default and marking `"use client"` only where interactivity (sorting, filtering, drawers) requires it
- **Styling** — Tailwind utility classes inline; only extract to a shared class/component if the pattern repeats 3+ times
- **shadcn components** — use installed primitives (`Table`, `Badge`, `Sheet`/`Dialog`, `Button`, `Input`) rather than hand-rolling equivalents; note any component that needs to be added via `npx shadcn add <name>`

Put each file in its own fenced code block with a clear path annotation (e.g. `// apps/web/app/leads/page.tsx`).

### 3. Integration note (1-3 lines)
How to wire it up — which API endpoint the component expects to call, what props/data shape it needs, any new shadcn component to install. If the user needs to add a nav link or a new route, call that out.

## What to avoid

- **Generic/dated looks** — no default browser form styling, no 2012-era bordered boxes
- **Code dumps without structure** — always separate components and note styling inline vs shadcn primitives
- **Over-styling** — restraint reads as quality in a B2B tool; solid colors over gradients, borders over heavy shadows
- **Consumer-app decoration** — this is a working tool for sales ops, not a marketing site; avoid illustration-heavy empty states or playful copy that slows down triage
- **Burying the score** — the product-fit score is the single most important scannable data point; it should never require a click to see
- **Single-record-at-a-time thinking** — always design for "a rep just uploaded 200 cards from a trade show," not one card
- **Mobile afterthought** — reasonable responsive behavior, but this is a desktop-first ops tool; don't over-invest in mobile layouts unless asked

## Handling ambiguity

If the user asks for something under-specified ("design the leads page"), make reasonable assumptions and *state them up front* in the UI plan — one line each, no long preamble. For example: "Assuming the leads page shows one exhibition's results at a time with a table + filters + detail drawer. Let me know if you want a cross-exhibition view instead."

Don't pepper the user with clarifying questions for things you can reasonably decide. Do ask when the answer genuinely changes the output — e.g. "Should the detail drawer allow editing enriched fields, or is enrichment read-only?"

## A worked example of the right vibe

**Request:** "Design the lead review table for an exhibition"

**UI plan:**
- Filter bar: exhibition selector, score tier chips (Strong/Moderate/Weak), industry dropdown, search by name/company
- Table columns: Contact (name + title), Company (name + industry badge), Score (colored badge, sortable, default sort), Status (processing/scored/failed), row action to open detail drawer
- Sticky header, 50 rows/page with pagination
- Bulk action bar appears when rows are checked: "Export selected" / "Push to CRM"

**Component:** `apps/web/app/exhibitions/[id]/leads/page.tsx` — server component fetching the initial page; `LeadsTable` client component owns sort/filter state and pagination.

**Styling:** shadcn `Table`, `Badge` (score tiers), `Select` (filters), `Checkbox` (row selection), `Sheet` (detail drawer). Score badge colors from the palette above.

**Integration:** expects `GET /orgs/{org_id}/exhibitions/{id}/leads?score_tier=&industry=&page=` returning paginated leads; row click opens `LeadDetailDrawer` which fetches `GET /leads/{id}` for full enrichment + card image URL.

That's the shape — concrete, consistent with the stack, dense enough for real triage work, and immediately usable.
