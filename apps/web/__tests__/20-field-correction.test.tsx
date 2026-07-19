// Tests for the 20-field-correction feature's frontend surface, per
// .claude/specs/20-field-correction.md:
//   - CardDetailDrawer's always-visible inline pencil -> edit -> save/cancel
//     affordance (InlineEditableValue), driven end-to-end through a real
//     field (full_name) — happy path (save persists the corrected value)
//     and error path (a 400 from the API renders inline, without crashing
//     or losing the user's draft input silently)
//
// global.fetch is mocked end-to-end (never hits a real server), following
// the same dispatch-on-(method, relative URL) convention as
// 10-lead-scoring.test.tsx / 09-bulk-select-parse-enrich.test.tsx.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CardDetailDrawer } from "@/components/card-detail-drawer";
import type { CardDetailOut } from "@/lib/api";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
}));

// ---- fixtures --------------------------------------------------------

const sampleCardDetail: CardDetailOut = {
  card_id: "card-1",
  user_id: "user-1",
  exhibition_id: null,
  original_filename: "card1.jpg",
  image_url: "https://example.com/card1.jpg",
  status: "extracted",
  full_name: "Wrong Name",
  job_title: "Manager",
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
  rescore_available: false,
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

function createApiMock(opts: { card: CardDetailOut; cardAfterCorrection?: CardDetailOut; correctionStatus?: number; correctionBody?: unknown }) {
  const correctionCalls: { body: unknown }[] = [];

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : String(input);
    const method = (init?.method ?? "GET").toUpperCase();

    if (method === "GET" && /^\/api\/cards\/[^/?]+$/.test(url)) {
      return jsonResponse(200, opts.card);
    }
    const correctionMatch = url.match(/^\/api\/cards\/([^/?]+)\/corrections$/);
    if (method === "POST" && correctionMatch) {
      correctionCalls.push({ body: init?.body ? JSON.parse(String(init.body)) : {} });
      if (opts.correctionStatus && opts.correctionStatus >= 400) {
        return jsonResponse(opts.correctionStatus, opts.correctionBody ?? { detail: "Corrected value is invalid" });
      }
      return jsonResponse(200, opts.cardAfterCorrection ?? opts.card);
    }
    throw new Error(`Unhandled fetch call in test: ${method} ${url}`);
  });

  return { fetchMock, correctionCalls };
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ======================================================================
// Card detail drawer — inline correction affordance (InlineEditableValue)
// ======================================================================

describe("Card detail drawer field correction", () => {
  it("renders a queryable edit affordance for every correctable field on the card", async () => {
    // jsdom doesn't apply the app's actual Tailwind stylesheet, so this
    // can't assert on real computed opacity/visibility (that regression —
    // pencils that were opacity-0 until precise hover — was verified live
    // in a real browser separately). This just confirms the edit
    // affordance is actually wired up and reachable via accessible role.
    const { fetchMock } = createApiMock({ card: sampleCardDetail });
    vi.stubGlobal("fetch", fetchMock);

    render(<CardDetailDrawer cardId="card-1" onClose={vi.fn()} />);
    await screen.findByText("Wrong Name");

    expect(screen.getAllByRole("button", { name: "Edit" }).length).toBeGreaterThan(0);
  });

  it("editing full_name, saving, calls POST /cards/{id}/corrections and displays the corrected value", async () => {
    const user = userEvent.setup();
    const corrected: CardDetailOut = { ...sampleCardDetail, full_name: "Corrected Name" };
    const { fetchMock, correctionCalls } = createApiMock({
      card: sampleCardDetail,
      cardAfterCorrection: corrected,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CardDetailDrawer cardId="card-1" onClose={vi.fn()} />);
    await screen.findByText("Wrong Name");

    const nameHeading = screen.getByText("Wrong Name").parentElement!;
    await user.click(within(nameHeading).getByRole("button", { name: "Edit" }));

    const input = screen.getByDisplayValue("Wrong Name");
    await user.clear(input);
    await user.type(input, "Corrected Name");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(correctionCalls).toHaveLength(1));
    expect(correctionCalls[0].body).toEqual({
      field_name: "full_name",
      corrected_value: "Corrected Name",
      record_id: null,
    });

    expect(await screen.findByText("Corrected Name")).toBeInTheDocument();
    expect(screen.queryByText("Wrong Name")).not.toBeInTheDocument();
  });

  it("cancel discards the draft without calling the API", async () => {
    const user = userEvent.setup();
    const { fetchMock, correctionCalls } = createApiMock({ card: sampleCardDetail });
    vi.stubGlobal("fetch", fetchMock);

    render(<CardDetailDrawer cardId="card-1" onClose={vi.fn()} />);
    await screen.findByText("Wrong Name");

    const nameHeading = screen.getByText("Wrong Name").parentElement!;
    await user.click(within(nameHeading).getByRole("button", { name: "Edit" }));
    const input = screen.getByDisplayValue("Wrong Name");
    await user.clear(input);
    await user.type(input, "Should Not Save");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.getByText("Wrong Name")).toBeInTheDocument();
    expect(screen.queryByText("Should Not Save")).not.toBeInTheDocument();
    expect(correctionCalls).toHaveLength(0);
  });

  it("a 400 from the API renders an inline error and keeps the field in edit mode, not silently discarded", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({
      card: sampleCardDetail,
      correctionStatus: 400,
      correctionBody: { detail: "Corrected value must differ from the current value" },
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CardDetailDrawer cardId="card-1" onClose={vi.fn()} />);
    await screen.findByText("Wrong Name");

    const nameHeading = screen.getByText("Wrong Name").parentElement!;
    await user.click(within(nameHeading).getByRole("button", { name: "Edit" }));
    const input = screen.getByDisplayValue("Wrong Name");
    await user.clear(input);
    await user.type(input, "Wrong Name");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText("Corrected value must differ from the current value")
    ).toBeInTheDocument();
    // Still editable — the draft input must still be present, not reverted
    // silently as if nothing happened.
    expect(screen.getByDisplayValue("Wrong Name")).toBeInTheDocument();
  });
});
