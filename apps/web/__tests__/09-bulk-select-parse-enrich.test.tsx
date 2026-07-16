// Tests for the 09-bulk-select-parse-enrich feature's frontend surface, per
// .claude/specs/09-bulk-select-parse-enrich.md:
//   - the upload page's header "select all" + per-row checkboxes, backed by
//     a single `selectedCardIds` state
//   - the "Parse Selected (N)" / "Enrich Selected (N)" bulk-action buttons,
//     each scoped to the eligible subset of the current selection
//   - the new per-row "Enrich company" (Sparkles) icon, shown only when
//     `company_enrichment_status === "pending"`, reusing the existing
//     single-card enrich endpoint
//
// global.fetch is mocked end-to-end (never hits a real server). We dispatch
// on method + relative URL so the same fixture can serve every request a
// rendered UploadPage fires (exhibitions list, card list, card detail,
// bulk parse, bulk enrich, single-card enrich), matching
// apps/web/lib/api.ts's actual request shapes.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import UploadPage from "@/app/upload/page";
import type { CardDetailOut, CardOut, ExhibitionOut } from "@/lib/api";

// UploadPage renders <Sidebar>, which calls next/navigation's useRouter().
// That throws outside a real Next.js app-router tree, so it must be mocked.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
}));

// ---- fixtures --------------------------------------------------------

const sampleCardDetail: CardDetailOut = {
  card_id: "card-detail-only",
  user_id: "user-1",
  exhibition_id: null,
  original_filename: "card-detail-only.jpg",
  image_url: "https://example.com/card-detail-only.jpg",
  status: "extracted",
  full_name: "Detail Drawer Occupant",
  job_title: "Procurement Manager",
  designation_level: null,
  special_remark: null,
  website: null,
  address: null,
  products_offered: null,
  gst_number: null,
  raw_ocr_text: null,
  extraction_error: null,
  merged_into_card_id: null,
  created_at: "2026-07-01T00:00:00Z",
  lead_score: null,
  score_breakdown: null,
  scored_at: null,
  company: null,
  emails: [],
  phones: [],
};

const baseCardFields = {
  user_id: "user-1",
  exhibition_id: null,
  original_filename: "card.jpg",
  image_url: "https://example.com/card.jpg",
  job_title: "Manager",
  merged_into_card_id: null,
  created_at: "2026-07-01T00:00:00Z",
  lead_score: null,
  score_breakdown: null,
  scored_at: null,
} as const;

function makeCard(params: {
  card_id: string;
  full_name: string;
  status: string;
  company_enrichment_status: string | null;
  company_id?: string | null;
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
  };
}

// A "new" card is parse-eligible only.
const cardNew1 = makeCard({
  card_id: "card-new-1",
  full_name: "Alice New",
  status: "new",
  company_enrichment_status: null,
});
const cardNew2 = makeCard({
  card_id: "card-new-2",
  full_name: "Bob New",
  status: "new",
  company_enrichment_status: null,
});
// An "extracted" card whose linked company is still pending is enrich-eligible only.
const cardPending1 = makeCard({
  card_id: "card-pending-1",
  full_name: "Carol Pending",
  status: "extracted",
  company_enrichment_status: "pending",
});
// An "extracted" card whose company is already enriched is eligible for neither.
const cardEnriched1 = makeCard({
  card_id: "card-enriched-1",
  full_name: "Dave Enriched",
  status: "extracted",
  company_enrichment_status: "enriched",
});

const sampleExhibitions: ExhibitionOut[] = [];

// ---- fetch mock plumbing ----------------------------------------------

type JsonOutcome = { status: number; body?: unknown };

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

/**
 * Builds a global-fetch stand-in that routes on (method, relative URL),
 * covering every endpoint the upload page can call:
 *   GET  /api/exhibitions              -> exhibitions list
 *   GET  /api/cards?...                -> card list (listCards)
 *   GET  /api/cards/:id                -> card detail (getCard, via the drawer)
 *   DELETE /api/cards/:id[...]         -> deleteCard (always 204 unless configured)
 *   POST /api/cards/process            -> bulk parse; captures the parsed body
 *   POST /api/cards/enrich-companies   -> bulk enrich; captures the parsed body
 *   POST /api/cards/:id/enrich-company -> single-card enrich; captures the id
 *
 * `processCalls` / `enrichCompaniesCalls` / `singleEnrichCalls` are mutated
 * in place (pushed to) as matching requests come in, so tests can assert on
 * them directly after awaiting the relevant user interaction.
 */
function createApiMock(opts: {
  card?: CardDetailOut | null;
  cards?: CardOut[];
  exhibitions?: ExhibitionOut[];
  processOutcome?: JsonOutcome;
  enrichCompaniesOutcome?: JsonOutcome;
  singleEnrichOutcome?: JsonOutcome;
}) {
  const cardsState = [...(opts.cards ?? [])];
  const processCalls: { exhibition_id: string | null; card_ids: string[] | null }[] = [];
  const enrichCompaniesCalls: { card_ids: string[] }[] = [];
  const singleEnrichCalls: string[] = [];

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
    if (method === "GET" && /^\/api\/cards\?/.test(url)) {
      return jsonResponse(200, cardsState);
    }
    if (method === "POST" && url === "/api/cards/process") {
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      processCalls.push(body);
      if (opts.processOutcome) {
        return jsonResponse(opts.processOutcome.status, opts.processOutcome.body ?? {});
      }
      return jsonResponse(200, { enqueued_count: (body.card_ids ?? []).length });
    }
    if (method === "POST" && url === "/api/cards/enrich-companies") {
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      enrichCompaniesCalls.push(body);
      if (opts.enrichCompaniesOutcome) {
        return jsonResponse(opts.enrichCompaniesOutcome.status, opts.enrichCompaniesOutcome.body ?? {});
      }
      return jsonResponse(200, {
        enqueued_count: (body.card_ids ?? []).length,
        skipped_count: 0,
      });
    }
    const singleEnrichMatch = url.match(/^\/api\/cards\/([^/?]+)\/enrich-company$/);
    if (method === "POST" && singleEnrichMatch) {
      const cardId = singleEnrichMatch[1];
      singleEnrichCalls.push(cardId);
      if (opts.singleEnrichOutcome) {
        return jsonResponse(opts.singleEnrichOutcome.status, opts.singleEnrichOutcome.body ?? {});
      }
      const matched = cardsState.find((c) => c.card_id === cardId) ?? null;
      return jsonResponse(200, matched ?? { card_id: cardId });
    }
    if (method === "GET" && /^\/api\/cards\/[^/?]+$/.test(url)) {
      if (!opts.card) return jsonResponse(404, { detail: "not found" });
      return jsonResponse(200, opts.card);
    }
    if (method === "DELETE" && /^\/api\/cards\/[^/?]+/.test(url)) {
      return jsonResponse(204, {});
    }
    throw new Error(`Unhandled fetch call in test: ${method} ${url}`);
  });

  return { fetchMock, processCalls, enrichCompaniesCalls, singleEnrichCalls };
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ======================================================================
// Upload page — bulk select / parse / enrich
// ======================================================================

describe("Upload page bulk select, parse, and enrich", () => {
  it("renders both bulk action buttons disabled when zero cards are selected", async () => {
    const { fetchMock } = createApiMock({ cards: [cardNew1, cardPending1] });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);

    await screen.findByText("Alice New");

    expect(
      screen.getByRole("button", { name: "Parse Selected (0)" }),
      "with no eligible rows selected, Parse Selected must be disabled"
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Enrich Selected (0)" }),
      "with no eligible rows selected, Enrich Selected must be disabled"
    ).toBeDisabled();
  });

  it("selecting all rows via the header checkbox updates both bulk button counts distinctly", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({ cards: [cardNew1, cardNew2, cardPending1] });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Alice New");

    await user.click(screen.getByRole("checkbox", { name: "Select all cards" }));

    // 2 "new" cards -> parse-eligible; 1 "pending"-company card -> enrich-eligible.
    expect(screen.getByRole("button", { name: "Parse Selected (2)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enrich Selected (1)" })).toBeInTheDocument();
  });

  it("unchecking the header checkbox after selecting all clears the selection back to zero", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({ cards: [cardNew1, cardNew2, cardPending1] });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Alice New");

    const headerCheckbox = screen.getByRole("checkbox", { name: "Select all cards" });
    await user.click(headerCheckbox);
    await screen.findByRole("button", { name: "Parse Selected (2)" });

    await user.click(headerCheckbox);

    expect(screen.getByRole("button", { name: "Parse Selected (0)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enrich Selected (0)" })).toBeInTheDocument();
  });

  it("checking a single row's checkbox only reflects that row's eligible contribution", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({ cards: [cardNew1, cardEnriched1] });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Alice New");

    await user.click(screen.getByRole("checkbox", { name: "Select Alice New" }));

    expect(
      screen.getByRole("button", { name: "Parse Selected (1)" }),
      "only the selected 'new' card should count toward Parse Selected"
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Enrich Selected (0)" }),
      "the unselected enriched card must not contribute, and Alice New has no pending company"
    ).toBeInTheDocument();
  });

  it("clicking Parse Selected sends exactly the selected-and-new card ids, then clears the selection", async () => {
    const user = userEvent.setup();
    const { fetchMock, processCalls } = createApiMock({
      cards: [cardNew1, cardNew2, cardPending1],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Alice New");

    await user.click(screen.getByRole("checkbox", { name: "Select all cards" }));
    await user.click(await screen.findByRole("button", { name: "Parse Selected (2)" }));

    await waitFor(() => expect(processCalls).toHaveLength(1));
    expect(
      processCalls[0].card_ids,
      "only the two 'new' cards should be sent, never the pending-company card"
    ).toEqual([cardNew1.card_id, cardNew2.card_id]);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Parse Selected (0)" })).toBeInTheDocument()
    );
    expect(screen.getByRole("button", { name: "Enrich Selected (0)" })).toBeInTheDocument();
  });

  it("clicking Enrich Selected sends exactly the selected-and-pending card ids, then clears the selection", async () => {
    const user = userEvent.setup();
    const { fetchMock, enrichCompaniesCalls } = createApiMock({
      cards: [cardNew1, cardNew2, cardPending1],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Alice New");

    await user.click(screen.getByRole("checkbox", { name: "Select all cards" }));
    await user.click(await screen.findByRole("button", { name: "Enrich Selected (1)" }));

    await waitFor(() => expect(enrichCompaniesCalls).toHaveLength(1));
    expect(
      enrichCompaniesCalls[0].card_ids,
      "only the pending-company card should be sent, never the two 'new' cards"
    ).toEqual([cardPending1.card_id]);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Enrich Selected (0)" })).toBeInTheDocument()
    );
    expect(screen.getByRole("button", { name: "Parse Selected (0)" })).toBeInTheDocument();
  });

  it("shows the row-level Enrich icon only for a card whose company_enrichment_status is pending", async () => {
    const { fetchMock } = createApiMock({ cards: [cardPending1, cardEnriched1] });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Carol Pending");
    await screen.findByText("Dave Enriched");

    const enrichIcons = screen.getAllByRole("button", { name: "Enrich company" });
    expect(
      enrichIcons,
      "the icon must appear for the pending-company row and be absent for the enriched row"
    ).toHaveLength(1);
  });

  it("clicking the row Enrich icon calls the single-card enrich endpoint for just that card and never opens the detail drawer", async () => {
    const user = userEvent.setup();
    const { fetchMock, singleEnrichCalls } = createApiMock({
      card: sampleCardDetail,
      cards: [cardPending1],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Carol Pending");

    await user.click(screen.getByRole("button", { name: "Enrich company" }));

    await waitFor(() => expect(singleEnrichCalls).toEqual([cardPending1.card_id]));
    expect(
      screen.queryByText("Card Detail"),
      "the row's own onClick (which opens the drawer) must not fire — the icon click must stop propagation"
    ).not.toBeInTheDocument();
  });

  it("clicking a row's checkbox never opens the detail drawer", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({ card: sampleCardDetail, cards: [cardNew1] });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Alice New");

    await user.click(screen.getByRole("checkbox", { name: "Select Alice New" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Parse Selected (1)" })).toBeInTheDocument()
    );
    expect(
      screen.queryByText("Card Detail"),
      "the row's own onClick (which opens the drawer) must not fire — the checkbox click must stop propagation"
    ).not.toBeInTheDocument();
  });

  it("shows an inline error banner (not a crash) when the bulk enrich call fails, and keeps the cards listed", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({
      cards: [cardPending1],
      enrichCompaniesOutcome: { status: 500, body: { detail: "Enrichment failed, please retry" } },
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Carol Pending");

    await user.click(screen.getByRole("checkbox", { name: "Select all cards" }));
    await user.click(await screen.findByRole("button", { name: "Enrich Selected (1)" }));

    expect(await screen.findByText("Enrichment failed, please retry")).toBeInTheDocument();
    expect(
      screen.getByText("Carol Pending"),
      "the app must remain usable — the card list should still render after a failed bulk enrich"
    ).toBeInTheDocument();
  });
});
