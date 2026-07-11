// Tests for the 10-lead-scoring feature's frontend surface, per
// .claude/specs/10-lead-scoring.md:
//   - the dashboard "Leads" table rendering real lead_score via ScoreBadge
//     (HIGH/MED/LOW buckets) or an "UNSCORED" badge when lead_score is null
//   - the dashboard's "Score Selected (N)" bulk action, scoped to the
//     extracted-status subset of the current selection
//   - CardDetailDrawer's "Score Card"/"Re-score Card" CTA, disabled unless
//     the card's status is "extracted", which re-fetches the card after
//     scoring completes
//   - the upload page's row-level "Score card" (Target) icon, whose spinner
//     stays visible until the card's scored_at actually changes (not just
//     until the enqueue POST resolves), and the "Scored" status pill that
//     appears once a card has a lead_score
//
// global.fetch is mocked end-to-end (never hits a real server). We dispatch
// on method + relative URL, matching apps/web/lib/api.ts's actual request
// shapes, following the same pattern as 09-bulk-select-parse-enrich.test.tsx.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Dashboard from "@/app/dashboard/page";
import UploadPage from "@/app/upload/page";
import { CardDetailDrawer } from "@/components/card-detail-drawer";
import type { CardDetailOut, CardOut, ExhibitionOut, UserOut } from "@/lib/api";

// Dashboard renders <Sidebar>, which calls next/navigation's useRouter().
// That throws outside a real Next.js app-router tree, so it must be mocked.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
}));

// ---- fixtures --------------------------------------------------------

const sampleUser: UserOut = {
  user_id: "user-1",
  name: "Priya Sharma",
  email: "priya@example.com",
  phone_no: null,
  org_id: null,
  role: null,
  phone_verified: true,
};

const baseCardFields = {
  user_id: "user-1",
  exhibition_id: null,
  original_filename: "card.jpg",
  image_url: "https://example.com/card.jpg",
  merged_into_card_id: null,
  created_at: "2026-07-01T00:00:00Z",
  company_id: null,
  company_name: null,
  company_enrichment_status: null,
} as const;

function makeCard(params: {
  card_id: string;
  full_name: string;
  job_title?: string | null;
  status: string;
  lead_score: number | null;
}): CardOut {
  return {
    ...baseCardFields,
    card_id: params.card_id,
    full_name: params.full_name,
    job_title: params.job_title ?? "Manager",
    status: params.status,
    lead_score: params.lead_score,
    score_breakdown: null,
    scored_at: null,
  };
}

const sampleCardDetail: CardDetailOut = {
  card_id: "card-1",
  user_id: "user-1",
  exhibition_id: null,
  original_filename: "card1.jpg",
  image_url: "https://example.com/card1.jpg",
  status: "extracted",
  full_name: "Jane Doe",
  job_title: "Procurement Manager",
  designation_level: "manager",
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

// ---- fetch mock plumbing ----------------------------------------------

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

function createApiMock(opts: {
  cards?: CardOut[];
  card?: CardDetailOut | null;
  cardAfterScore?: CardDetailOut | null;
}) {
  const cardsState = [...(opts.cards ?? [])];
  const scoreCallsBulk: { card_ids: string[] }[] = [];
  const scoreCallsSingle: string[] = [];
  let cardDetailRequestCount = 0;

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : String(input);
    const method = (init?.method ?? "GET").toUpperCase();

    if (method === "GET" && url === "/api/auth/me") {
      return jsonResponse(200, sampleUser);
    }
    if (method === "GET" && /^\/api\/cards(\?.*)?$/.test(url)) {
      return jsonResponse(200, cardsState);
    }
    if (method === "POST" && url === "/api/cards/score") {
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      scoreCallsBulk.push(body);
      return jsonResponse(200, { enqueued_count: (body.card_ids ?? []).length, skipped_count: 0 });
    }
    const singleScoreMatch = url.match(/^\/api\/cards\/([^/?]+)\/score$/);
    if (method === "POST" && singleScoreMatch) {
      const cardId = singleScoreMatch[1];
      scoreCallsSingle.push(cardId);
      const matched = cardsState.find((c) => c.card_id === cardId) ?? null;
      return jsonResponse(200, matched ?? { card_id: cardId });
    }
    if (method === "GET" && /^\/api\/cards\/[^/?]+$/.test(url)) {
      cardDetailRequestCount += 1;
      // First fetch returns the pre-score card, every subsequent fetch
      // (after the "Score Card" click re-fetches) returns the post-score
      // version, if provided — mirrors the drawer's real refetch-after-mutate flow.
      if (cardDetailRequestCount > 1 && opts.cardAfterScore) {
        return jsonResponse(200, opts.cardAfterScore);
      }
      if (!opts.card) return jsonResponse(404, { detail: "not found" });
      return jsonResponse(200, opts.card);
    }
    throw new Error(`Unhandled fetch call in test: ${method} ${url}`);
  });

  return { fetchMock, scoreCallsBulk, scoreCallsSingle };
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ======================================================================
// Dashboard — real lead_score rendering + bulk "Score Selected"
// ======================================================================

describe("Dashboard leads table scoring", () => {
  it("renders ScoreBadge buckets for HIGH/MED/LOW and an UNSCORED badge for a null lead_score", async () => {
    const cards = [
      makeCard({ card_id: "card-high", full_name: "High Fit Lead", status: "extracted", lead_score: 85 }),
      makeCard({ card_id: "card-med", full_name: "Medium Fit Lead", status: "extracted", lead_score: 65 }),
      makeCard({ card_id: "card-low", full_name: "Low Fit Lead", status: "extracted", lead_score: 40 }),
      makeCard({ card_id: "card-unscored", full_name: "Unscored Lead", status: "extracted", lead_score: null }),
    ];
    const { fetchMock } = createApiMock({ cards });
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);
    await screen.findByText("High Fit Lead");

    expect(screen.getByText("85% HIGH")).toBeInTheDocument();
    expect(screen.getByText("65% MED")).toBeInTheDocument();
    expect(screen.getByText("40% LOW")).toBeInTheDocument();
    expect(screen.getByText("UNSCORED")).toBeInTheDocument();
  });

  it("renders Score Selected disabled at zero, enables once an extracted card is checked", async () => {
    const user = userEvent.setup();
    const cards = [
      makeCard({ card_id: "card-new", full_name: "Not Yet Parsed", status: "new", lead_score: null }),
      makeCard({ card_id: "card-extracted", full_name: "Ready To Score", status: "extracted", lead_score: null }),
    ];
    const { fetchMock } = createApiMock({ cards });
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);
    await screen.findByText("Ready To Score");

    expect(screen.getByRole("button", { name: "Score Selected (0)" })).toBeDisabled();

    await user.click(screen.getByRole("checkbox", { name: "Select all cards" }));

    // Only the "extracted" card counts toward Score Selected — the "new"
    // card isn't eligible yet.
    expect(screen.getByRole("button", { name: "Score Selected (1)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Score Selected (1)" })).not.toBeDisabled();
  });

  it("clicking Score Selected sends exactly the selected-and-extracted card ids, then clears the selection", async () => {
    const user = userEvent.setup();
    const cards = [
      makeCard({ card_id: "card-new", full_name: "Not Yet Parsed", status: "new", lead_score: null }),
      makeCard({ card_id: "card-extracted", full_name: "Ready To Score", status: "extracted", lead_score: null }),
    ];
    const { fetchMock, scoreCallsBulk } = createApiMock({ cards });
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);
    await screen.findByText("Ready To Score");

    await user.click(screen.getByRole("checkbox", { name: "Select all cards" }));
    await user.click(await screen.findByRole("button", { name: "Score Selected (1)" }));

    await waitFor(() => expect(scoreCallsBulk).toHaveLength(1));
    expect(scoreCallsBulk[0].card_ids).toEqual(["card-extracted"]);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Score Selected (0)" })).toBeInTheDocument()
    );
  });
});

// ======================================================================
// Card detail drawer — "Score Card" / "Re-score Card" CTA
// ======================================================================

describe("Card detail drawer scoring CTA", () => {
  it("disables the Score Card button when the card's status is not extracted", async () => {
    const notExtracted: CardDetailOut = { ...sampleCardDetail, status: "new" };
    const { fetchMock } = createApiMock({ card: notExtracted });
    vi.stubGlobal("fetch", fetchMock);

    render(<CardDetailDrawer cardId="card-1" onClose={vi.fn()} />);

    expect(await screen.findByRole("button", { name: "Score Card" })).toBeDisabled();
    expect(screen.getByText("Not scored yet.")).toBeInTheDocument();
  });

  it("enables Score Card once extracted, calls POST /cards/{id}/score, then re-fetches and shows Re-score Card", async () => {
    const user = userEvent.setup();
    const extractedUnscored: CardDetailOut = { ...sampleCardDetail, status: "extracted", lead_score: null };
    const scoredCard: CardDetailOut = {
      ...sampleCardDetail,
      status: "extracted",
      lead_score: 72,
      score_breakdown: {
        designation_score: 14,
        company_size_score: 0,
        industry_fit_score: 0,
        momentum_signal_score: 0,
        remark_signal_score: 3,
        total: 72,
        version: "v1",
      },
      scored_at: "2026-07-10T12:00:00Z",
    };
    const { fetchMock, scoreCallsSingle } = createApiMock({
      card: extractedUnscored,
      cardAfterScore: scoredCard,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CardDetailDrawer cardId="card-1" onClose={vi.fn()} />);

    const scoreButton = await screen.findByRole("button", { name: "Score Card" });
    expect(scoreButton).not.toBeDisabled();

    await user.click(scoreButton);

    await waitFor(() => expect(scoreCallsSingle).toEqual(["card-1"]));
    expect(await screen.findByRole("button", { name: "Re-score Card" })).toBeInTheDocument();
    expect(screen.getByText("72")).toBeInTheDocument();
  });
});

// ======================================================================
// Upload page — row-level "Score card" icon + "Scored" status pill
// ======================================================================

function makeUploadCard(params: {
  card_id: string;
  full_name: string;
  status: string;
  lead_score: number | null;
  scored_at: string | null;
}): CardOut {
  return {
    card_id: params.card_id,
    user_id: "user-1",
    exhibition_id: null,
    original_filename: "card.jpg",
    image_url: "https://example.com/card.jpg",
    status: params.status,
    full_name: params.full_name,
    job_title: "Manager",
    merged_into_card_id: null,
    created_at: "2026-07-01T00:00:00Z",
    company_id: null,
    company_name: null,
    company_enrichment_status: null,
    lead_score: params.lead_score,
    score_breakdown: null,
    scored_at: params.scored_at,
  };
}

// The 3rd GET /cards (mount load, then handleRowScore's own post-enqueue
// refresh, then the first interval-driven poll) is when "scoring" is
// simulated to have actually finished — deliberately later than the POST
// response itself, so a test can prove the spinner is tied to a real
// scored_at change rather than to the enqueue call resolving.
function createUploadApiMock(initialCard: CardOut) {
  let cardsState = [initialCard];
  const scoreCallsSingle: string[] = [];
  let getCallCount = 0;

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : String(input);
    const method = (init?.method ?? "GET").toUpperCase();

    if (method === "GET" && /^\/api\/exhibitions/.test(url)) {
      return jsonResponse(200, [] as ExhibitionOut[]);
    }
    if (method === "GET" && /^\/api\/cards(\?.*)?$/.test(url)) {
      getCallCount += 1;
      if (getCallCount >= 3) {
        cardsState = cardsState.map((c) =>
          c.card_id === initialCard.card_id
            ? { ...c, lead_score: 55, scored_at: "2026-07-10T13:00:00Z" }
            : c
        );
      }
      return jsonResponse(200, cardsState);
    }
    const singleScoreMatch = url.match(/^\/api\/cards\/([^/?]+)\/score$/);
    if (method === "POST" && singleScoreMatch) {
      scoreCallsSingle.push(singleScoreMatch[1]);
      return jsonResponse(200, cardsState[0]);
    }
    if (method === "DELETE" && /^\/api\/cards\/[^/?]+/.test(url)) {
      return jsonResponse(204, {});
    }
    throw new Error(`Unhandled fetch call in test: ${method} ${url}`);
  });

  return { fetchMock, scoreCallsSingle };
}

describe("Upload page row scoring", () => {
  it("shows the row Score icon only for an extracted card", async () => {
    const card = makeUploadCard({
      card_id: "card-1",
      full_name: "Ready To Score",
      status: "extracted",
      lead_score: null,
      scored_at: null,
    });
    const { fetchMock } = createUploadApiMock(card);
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Ready To Score");

    expect(screen.getByRole("button", { name: "Score card" })).toBeInTheDocument();
  });

  it(
    "keeps the row Score spinner visible until scored_at actually changes, then shows the Scored status pill",
    async () => {
      const user = userEvent.setup();
      const card = makeUploadCard({
        card_id: "card-1",
        full_name: "Ready To Score",
        status: "extracted",
        lead_score: null,
        scored_at: null,
      });
      const { fetchMock, scoreCallsSingle } = createUploadApiMock(card);
      vi.stubGlobal("fetch", fetchMock);

      const { container } = render(<UploadPage />);
      await screen.findByText("Ready To Score");

      await user.click(screen.getByRole("button", { name: "Score card" }));

      await waitFor(() => expect(scoreCallsSingle).toEqual(["card-1"]));
      // Right after the enqueue POST resolves, the row must still show the
      // spinner — the simulated Celery task hasn't finished yet (the first
      // two GETs still return the unscored card).
      expect(container.querySelector('[aria-label="Scoring card"]')).toBeInTheDocument();
      expect(screen.queryByText("Scored")).not.toBeInTheDocument();

      // The interval poll (every 2s) eventually observes the real
      // scored_at change; the spinner clears and the Scored pill appears.
      await waitFor(() => expect(screen.getByText("Scored")).toBeInTheDocument(), {
        timeout: 4000,
      });
      expect(container.querySelector('[aria-label="Scoring card"]')).not.toBeInTheDocument();
    },
    8000
  );
});
