// Tests for the 11-export-data feature's frontend surface, per
// .claude/specs/11-export-data.md:
//   - the "Export" bulk-action button on apps/web/app/upload/page.tsx,
//     grouped with the existing Parse/Enrich/Score/Delete bulk actions and
//     reusing the same `useCardSelection` checkbox UI (step 09)
//   - unlike Parse/Enrich/Score, Export has NO eligibility filter — it is
//     enabled for any non-empty selection regardless of card status/score
//   - Export never mutates a card, so (unlike the other bulk handlers) it
//     must NOT clear the selection or re-fetch the card list afterward
//   - the CTA was relocated off apps/dashboard/page.tsx entirely — this file
//     also asserts no Export control remains there
//
// Backend behavior (POST /cards/export, card_service.export_cards,
// export_service.build_csv) is already covered at
// apps/api/tests/test_11-export-data.py and is explicitly out of scope here.
//
// global.fetch is mocked end-to-end (never hits a real server), dispatching
// on method + relative URL, matching apps/web/lib/api.ts's actual request
// shapes — same pattern as apps/web/__tests__/09-bulk-select-parse-enrich.test.tsx.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import UploadPage from "@/app/upload/page";
import Dashboard from "@/app/dashboard/page";
import type { CardOut, ExhibitionOut } from "@/lib/api";

// Both pages render <Sidebar>, which calls next/navigation's useRouter().
// That throws outside a real Next.js app-router tree, so it must be mocked.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
}));

// Dashboard also calls lib/auth's getCurrentUser() on mount — stub it so the
// page renders without needing a real session.
vi.mock("@/lib/auth", () => ({
  getCurrentUser: () => Promise.resolve(null),
}));

// jsdom does not implement the Blob-URL download machinery exportCards()
// drives (URL.createObjectURL/revokeObjectURL, anchor .click() navigation).
// Stub them so a successful export resolves deterministically instead of
// throwing/logging jsdom "not implemented" noise — the actual browser
// download mechanics are exportCards()'s concern (apps/web/lib/api.ts), not
// this page-level suite's.
beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
  URL.createObjectURL = vi.fn(() => "blob:mock-url");
  URL.revokeObjectURL = vi.fn();
  // `.click()` on an <a> is inherited from HTMLElement.prototype (per the
  // HTML click()-method mixin), not redeclared on HTMLAnchorElement itself —
  // spy on the prototype that actually owns it.
  vi.spyOn(HTMLElement.prototype, "click").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---- fixtures ----------------------------------------------------------

const baseCardFields = {
  user_id: "user-1",
  exhibition_id: null,
  original_filename: "card.jpg",
  image_url: "https://example.com/card.jpg",
  job_title: "Manager",
  merged_into_card_id: null,
  created_at: "2026-07-01T00:00:00Z",
  score_breakdown: null,
  scored_at: null,
} as const;

function makeCard(params: {
  card_id: string;
  full_name: string;
  status: string;
  company_enrichment_status: string | null;
  company_id?: string | null;
  lead_score?: number | null;
}): CardOut {
  return {
    ...baseCardFields,
    card_id: params.card_id,
    full_name: params.full_name,
    status: params.status,
    company_enrichment_status: params.company_enrichment_status,
    company_id:
      params.company_id !== undefined
        ? params.company_id
        : params.company_enrichment_status
        ? `company-${params.card_id}`
        : null,
    company_name: null,
    lead_score: params.lead_score ?? null,
  };
}

// Ineligible for Parse (status != "new"), Enrich (company not "pending"), and
// Score (lead_score already set) — the key fixture demonstrating Export's
// lack of an eligibility filter, unlike its sibling bulk actions.
const cardFullyIneligible = makeCard({
  card_id: "card-ineligible-1",
  full_name: "Erin Ineligible",
  status: "extracted",
  company_enrichment_status: "enriched",
  lead_score: 92,
});

// A second, distinct "also ineligible for the other actions" card, used to
// test that Export sends exactly the selected ids (order-sensitive) rather
// than reusing the same single-card fixture across assertions.
const cardFullyIneligible2 = makeCard({
  card_id: "card-ineligible-2",
  full_name: "Frank Ineligible",
  status: "extracted",
  company_enrichment_status: "enriched",
  lead_score: 40,
});

const sampleExhibitions: ExhibitionOut[] = [];

// ---- fetch mock plumbing ------------------------------------------------

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

// A minimal stand-in for the CSV file response POST /cards/export returns —
// exportCards() reads it via res.blob(), not res.json().
function csvResponse(status: number, opts: { csv?: string; errorBody?: unknown } = {}): Response {
  const ok = status >= 200 && status < 300;
  return {
    ok,
    status,
    blob: async () => new Blob([opts.csv ?? "Full Name\n"], { type: "text/csv" }),
    json: async () => opts.errorBody ?? { detail: "Export failed" },
  } as unknown as Response;
}

type JsonOutcome = { status: number; body?: unknown };

/**
 * Builds a global-fetch stand-in covering every endpoint the upload/
 * dashboard pages can call in these tests:
 *   GET  /api/exhibitions       -> exhibitions list
 *   GET  /api/wallet            -> wallet summary (upload page only)
 *   GET  /api/cards?...         -> card list (listCards) — call count tracked
 *   POST /api/cards/export      -> export; captures the parsed request body
 *   GET  /api/cards/:id         -> card detail (unused here; 404s by default)
 *
 * `exportCalls` / `listCardsCallCount` are exposed so tests can assert on
 * exactly what was sent, and that no unexpected re-fetch happened.
 */
function createApiMock(opts: {
  cards?: CardOut[];
  exhibitions?: ExhibitionOut[];
  exportOutcome?: JsonOutcome;
}) {
  const cardsState = [...(opts.cards ?? [])];
  const exportCalls: { card_ids: string[] }[] = [];
  let listCardsCallCount = 0;

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : String(input);
    const method = (init?.method ?? "GET").toUpperCase();

    if (method === "GET" && /^\/api\/exhibitions/.test(url)) {
      return jsonResponse(200, opts.exhibitions ?? sampleExhibitions);
    }
    if (method === "GET" && url === "/api/wallet") {
      return jsonResponse(200, {
        balance_inr: "0",
        currency: "INR",
        transactions: [],
        free_actions_remaining: { parse: 20, enrichment: 20, scoring: 20 },
      });
    }
    // Matches both the upload page's GET /api/cards?... (always sends query
    // params) and the dashboard page's GET /api/cards (called with no
    // params, so no querystring at all).
    if (method === "GET" && /^\/api\/cards(\?.*)?$/.test(url)) {
      listCardsCallCount += 1;
      return jsonResponse(200, cardsState);
    }
    if (method === "POST" && url === "/api/cards/export") {
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      exportCalls.push(body);
      if (opts.exportOutcome) {
        return opts.exportOutcome.status >= 200 && opts.exportOutcome.status < 300
          ? csvResponse(opts.exportOutcome.status)
          : csvResponse(opts.exportOutcome.status, { errorBody: opts.exportOutcome.body ?? {} });
      }
      return csvResponse(200);
    }
    if (method === "GET" && /^\/api\/cards\/[^/?]+$/.test(url)) {
      return jsonResponse(404, { detail: "not found" });
    }
    throw new Error(`Unhandled fetch call in test: ${method} ${url}`);
  });

  return { fetchMock, exportCalls, getListCardsCallCount: () => listCardsCallCount };
}

// ======================================================================
// Upload page — Export bulk action
// ======================================================================

describe("Upload page Export bulk action", () => {
  it("renders the Export button disabled when zero cards are selected", async () => {
    const { fetchMock } = createApiMock({ cards: [cardFullyIneligible, cardFullyIneligible2] });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Erin Ineligible");

    expect(
      screen.getByRole("button", { name: "Export (0)" }),
      "with no cards selected, Export must be disabled"
    ).toBeDisabled();
  });

  it("enables Export with any selection regardless of card status/score, with no eligibility filter unlike Parse/Enrich/Score", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({ cards: [cardFullyIneligible] });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Erin Ineligible");

    // This card is ineligible for parse (not "new"), enrich (not "pending"),
    // and score (already has a lead_score) — Parse/Enrich/Score should stay
    // at 0/disabled, while Export becomes enabled purely off selection size.
    await user.click(screen.getByRole("checkbox", { name: "Select Erin Ineligible" }));

    expect(
      screen.getByRole("button", { name: "Export (1)" }),
      "Export must enable for any selected card, with no per-card eligibility check"
    ).toBeEnabled();
    expect(screen.getByRole("button", { name: "Parse (0)" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Enrich (0)" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Score (0)" })).toBeDisabled();
  });

  it("clicking Export calls POST /cards/export with exactly the selected card ids", async () => {
    const user = userEvent.setup();
    const { fetchMock, exportCalls } = createApiMock({
      cards: [cardFullyIneligible, cardFullyIneligible2],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Erin Ineligible");

    await user.click(screen.getByRole("checkbox", { name: "Select Erin Ineligible" }));
    await user.click(screen.getByRole("checkbox", { name: "Select Frank Ineligible" }));
    await user.click(await screen.findByRole("button", { name: "Export (2)" }));

    await waitFor(() => expect(exportCalls).toHaveLength(1));
    expect(
      exportCalls[0].card_ids,
      "the export request must carry exactly the ids of the selected cards, nothing more or less"
    ).toEqual([cardFullyIneligible.card_id, cardFullyIneligible2.card_id]);
  });

  it("leaves selection state and the card list untouched after a successful export", async () => {
    const user = userEvent.setup();
    const { fetchMock, exportCalls, getListCardsCallCount } = createApiMock({
      cards: [cardFullyIneligible, cardFullyIneligible2],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Erin Ineligible");

    await user.click(screen.getByRole("checkbox", { name: "Select Erin Ineligible" }));
    const listCallsBeforeExport = getListCardsCallCount();

    await user.click(await screen.findByRole("button", { name: "Export (1)" }));
    await waitFor(() => expect(exportCalls).toHaveLength(1));

    // Export must not clear the selection (unlike Parse/Enrich/Score, which
    // call clearSelection()) — the button label and checkbox state should be
    // exactly as they were before the click.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Export (1)" })).toBeInTheDocument()
    );
    expect(
      screen.getByRole("checkbox", { name: "Select Erin Ineligible" }),
      "the selected card's checkbox must remain checked after a successful export"
    ).toBeChecked();

    // Export must not trigger a card-list refresh (unlike Parse/Enrich/Score,
    // which call refreshCards()) — no additional GET /api/cards call.
    expect(
      getListCardsCallCount(),
      "a successful export must not re-fetch the card list — export doesn't mutate any card"
    ).toBe(listCallsBeforeExport);

    // Both cards are still listed, confirming the list itself wasn't reset.
    expect(screen.getByText("Erin Ineligible")).toBeInTheDocument();
    expect(screen.getByText("Frank Ineligible")).toBeInTheDocument();
  });

  it("shows an inline error banner (not a crash) when export fails, and keeps the card list intact", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({
      cards: [cardFullyIneligible],
      exportOutcome: { status: 500, body: { detail: "Export failed, please retry" } },
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Erin Ineligible");

    await user.click(screen.getByRole("checkbox", { name: "Select Erin Ineligible" }));
    await user.click(await screen.findByRole("button", { name: "Export (1)" }));

    expect(await screen.findByText("Export failed, please retry")).toBeInTheDocument();
    expect(
      screen.getByText("Erin Ineligible"),
      "the app must remain usable — the card list should still render after a failed export"
    ).toBeInTheDocument();
  });
});

// ======================================================================
// Dashboard page — Export CTA relocation
// ======================================================================

describe("Dashboard page no longer has an Export CTA", () => {
  it("does not render an Export button/control on /dashboard", async () => {
    const { fetchMock } = createApiMock({ cards: [cardFullyIneligible, cardFullyIneligible2] });
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);
    await screen.findByText("Erin Ineligible");

    expect(
      screen.queryByRole("button", { name: /export/i }),
      "the Export CTA was relocated to /upload — /dashboard must not have any Export control left"
    ).not.toBeInTheDocument();
  });
});
