// Tests for the 15-wallet-usage feature's frontend surface, per
// .claude/specs/15-wallet-usage.md:
//   - the upload page shows the current wallet balance and each action
//     type's free-actions-remaining count, sourced from GET /wallet
//   - a bulk parse/enrich/score response with wallet_blocked_count > 0
//     surfaces a message via the existing error-banner pattern, distinct
//     from a hard failure of the request itself
//   - a per-row action blocked by the wallet (402) surfaces the backend's
//     descriptive detail message inline, via the existing ApiError-message
//     catch idiom
//
// global.fetch is mocked end-to-end (never hits a real server), following
// the same dispatch-on-(method, relative URL) convention as
// 09-bulk-select-parse-enrich.test.tsx / 10-lead-scoring.test.tsx.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import UploadPage from "@/app/upload/page";
import type { CardDetailOut, CardOut, ExhibitionOut, WalletOut } from "@/lib/api";

// UploadPage renders <Sidebar>, which calls next/navigation's useRouter().
// That throws outside a real Next.js app-router tree, so it must be mocked.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
}));

// ---- fixtures ----------------------------------------------------------

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

const sampleExhibitions: ExhibitionOut[] = [];

function walletFixture(overrides: Partial<WalletOut> = {}): WalletOut {
  return {
    balance_inr: "0",
    currency: "INR",
    transactions: [],
    free_actions_remaining: { parse: 20, enrichment: 20, scoring: 20 },
    ...overrides,
  };
}

// ---- fetch mock plumbing ------------------------------------------------

type JsonOutcome = { status: number; body?: unknown };

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

function createApiMock(opts: {
  cards?: CardOut[];
  exhibitions?: ExhibitionOut[];
  wallet?: WalletOut;
  cardDetail?: CardDetailOut;
  processOutcome?: JsonOutcome;
  singleEnrichOutcome?: JsonOutcome;
  reprocessOutcome?: JsonOutcome;
}) {
  const cardsState = [...(opts.cards ?? [])];
  const processCalls: { card_ids: string[] | null }[] = [];
  const singleEnrichCalls: string[] = [];
  const reprocessCalls: string[] = [];
  let walletCallCount = 0;

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : String(input);
    const method = (init?.method ?? "GET").toUpperCase();

    if (method === "GET" && /^\/api\/exhibitions/.test(url)) {
      return jsonResponse(200, opts.exhibitions ?? sampleExhibitions);
    }
    if (method === "GET" && url === "/api/wallet") {
      walletCallCount += 1;
      return jsonResponse(200, opts.wallet ?? walletFixture());
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
      return jsonResponse(200, {
        enqueued_count: (body.card_ids ?? []).length,
        wallet_blocked_count: 0,
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
    const reprocessMatch = url.match(/^\/api\/cards\/([^/?]+)\/reprocess$/);
    if (method === "POST" && reprocessMatch) {
      const cardId = reprocessMatch[1];
      reprocessCalls.push(cardId);
      if (opts.reprocessOutcome) {
        return jsonResponse(opts.reprocessOutcome.status, opts.reprocessOutcome.body ?? {});
      }
      return jsonResponse(200, opts.cardDetail ?? { card_id: cardId });
    }
    if (method === "GET" && /^\/api\/cards\/[^/?]+$/.test(url)) {
      if (opts.cardDetail) return jsonResponse(200, opts.cardDetail);
      return jsonResponse(404, { detail: "not found" });
    }
    throw new Error(`Unhandled fetch call in test: ${method} ${url}`);
  });

  return {
    fetchMock,
    processCalls,
    singleEnrichCalls,
    reprocessCalls,
    getWalletCallCount: () => walletCallCount,
  };
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ======================================================================
// Wallet balance / free-actions-remaining indicator
// ======================================================================

describe("Upload page wallet indicator", () => {
  it("shows the current balance and each action type's free actions remaining", async () => {
    const { fetchMock } = createApiMock({
      cards: [],
      wallet: walletFixture({
        balance_inr: "120",
        free_actions_remaining: { parse: 5, enrichment: 20, scoring: 12 },
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);

    expect(
      await screen.findByText(/Wallet: ₹120/),
      "must show the current wallet balance"
    ).toBeInTheDocument();
    expect(screen.getByText(/Parse 5/)).toBeInTheDocument();
    expect(screen.getByText(/Enrich 20/)).toBeInTheDocument();
    expect(screen.getByText(/Score 12/)).toBeInTheDocument();
  });
});

// ======================================================================
// Bulk wallet_blocked_count banner
// ======================================================================

describe("Upload page bulk wallet-blocked banner", () => {
  it("shows a wallet-blocked message when a bulk parse response reports wallet_blocked_count > 0", async () => {
    const user = userEvent.setup();
    const cardNew1 = makeCard({
      card_id: "card-new-1",
      full_name: "Alice New",
      status: "new",
      company_enrichment_status: null,
    });
    const { fetchMock, processCalls } = createApiMock({
      cards: [cardNew1],
      processOutcome: { status: 200, body: { enqueued_count: 0, wallet_blocked_count: 1 } },
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Alice New");

    await user.click(screen.getByRole("checkbox", { name: "Select Alice New" }));
    await user.click(await screen.findByRole("button", { name: "Parse (1)" }));

    await waitFor(() => expect(processCalls).toHaveLength(1));
    expect(
      await screen.findByText(/wallet balance too low/i),
      "a wallet-blocked bulk parse must surface a message distinct from a hard failure"
    ).toBeInTheDocument();
  });
});

// ======================================================================
// Per-row action blocked by the wallet (402)
// ======================================================================

describe("Upload page per-row action blocked by the wallet", () => {
  it("shows the backend's message when a per-row enrich action returns 402", async () => {
    const user = userEvent.setup();
    const cardPending1 = makeCard({
      card_id: "card-pending-1",
      full_name: "Carol Pending",
      status: "extracted",
      company_enrichment_status: "pending",
    });
    const { fetchMock, singleEnrichCalls } = createApiMock({
      cards: [cardPending1],
      singleEnrichOutcome: {
        status: 402,
        body: {
          detail:
            "Wallet balance too low to enrich this company — recharge your wallet to continue",
        },
      },
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Carol Pending");

    await user.click(screen.getByRole("button", { name: "Enrich company" }));

    await waitFor(() => expect(singleEnrichCalls).toEqual([cardPending1.card_id]));
    expect(
      await screen.findByText(/Wallet balance too low to enrich this company/),
    ).toBeInTheDocument();
  });
});

// ======================================================================
// Card detail drawer actions also refresh the wallet indicator
// ======================================================================

describe("Upload page wallet refresh from the card detail drawer", () => {
  it("refetches the wallet balance after a billable action inside the drawer, not just the row/bulk actions", async () => {
    const user = userEvent.setup();
    const failedCard = makeCard({
      card_id: "card-failed-1",
      full_name: "Dave Failed",
      status: "failed",
      company_enrichment_status: null,
    });
    const cardDetail: CardDetailOut = {
      card_id: "card-failed-1",
      user_id: "user-1",
      exhibition_id: null,
      original_filename: "card.jpg",
      image_url: "https://example.com/card.jpg",
      status: "failed",
      full_name: "Dave Failed",
      job_title: null,
      designation_level: null,
      special_remark: null,
      website: null,
      address: null,
      products_offered: null,
      gst_number: null,
      raw_ocr_text: null,
      extraction_error: "Vision extraction failed after multiple attempts. You can retry.",
      merged_into_card_id: null,
      created_at: "2026-07-01T00:00:00Z",
      lead_score: null,
      score_breakdown: null,
      scored_at: null,
      company: null,
      emails: [],
      phones: [],
    };
    const { fetchMock, reprocessCalls, getWalletCallCount } = createApiMock({
      cards: [failedCard],
      cardDetail,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Dave Failed");

    const walletCallsBeforeDrawerAction = getWalletCallCount();
    expect(walletCallsBeforeDrawerAction).toBeGreaterThan(0);

    // Clicking the row (not a checkbox/icon) opens the CardDetailDrawer.
    await user.click(screen.getByText("Dave Failed"));
    await user.click(await screen.findByRole("button", { name: "Retry" }));

    await waitFor(() => expect(reprocessCalls).toEqual(["card-failed-1"]));
    await waitFor(() =>
      expect(getWalletCallCount()).toBeGreaterThan(walletCallsBeforeDrawerAction)
    );
  });
});
