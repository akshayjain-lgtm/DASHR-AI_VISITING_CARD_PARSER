// Tests for the 08-delete-card feature's frontend surface, per
// .claude/specs/08-delete-card.md:
//   - CardDetailDrawer's "Delete Card" button
//   - the upload page's row-level trash icon
// Both entry points must drive the same two-step confirm flow:
//   1. generic confirm ("Delete this card? This can't be undone.") before
//      any API call is made
//   2. a *second*, distinct confirm — only shown after the API responds 409
//      with a {child_count} body — naming the child count, which (only if
//      confirmed) re-issues the DELETE with confirm_cascade=true
//
// global.fetch is mocked end-to-end (never hits a real server). We dispatch
// on method + relative URL so the same fixture can serve every request a
// rendered page/drawer fires (exhibitions list, card list, card detail,
// delete), matching apps/web/lib/api.ts's actual request shapes.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CardDetailDrawer } from "@/components/card-detail-drawer";
import UploadPage from "@/app/upload/page";
import type { CardDetailOut, CardOut, ExhibitionOut } from "@/lib/api";

// UploadPage renders <Sidebar>, which calls next/navigation's useRouter().
// That throws outside a real Next.js app-router tree, so it must be mocked
// for any test that renders UploadPage. Harmless for the drawer-only tests.
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
  full_name: "Jane Doe",
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

const sampleCard: CardOut = {
  card_id: "card-1",
  user_id: "user-1",
  exhibition_id: null,
  original_filename: "card1.jpg",
  image_url: "https://example.com/card1.jpg",
  status: "extracted",
  full_name: "Jane Doe",
  job_title: "Procurement Manager",
  merged_into_card_id: null,
  created_at: "2026-07-01T00:00:00Z",
  company_id: null,
  company_name: null,
  company_enrichment_status: null,
  lead_score: null,
  score_breakdown: null,
  scored_at: null,
};

const sampleExhibitions: ExhibitionOut[] = [];

// ---- fetch mock plumbing ----------------------------------------------

type DeleteOutcome = { status: number; body?: unknown };

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

/**
 * Builds a global-fetch stand-in that routes on (method, relative URL),
 * covering every endpoint the drawer / upload page can call:
 *   GET  /api/exhibitions        -> exhibitions list
 *   GET  /api/cards?...          -> card list (listCards)
 *   GET  /api/cards/:id          -> card detail (getCard)
 *   DELETE /api/cards/:id[...]   -> deleteCard, consuming `deleteOutcomes`
 *                                    in order (one entry per DELETE call;
 *                                    the last entry repeats if exhausted)
 *
 * On a 204 delete outcome, the card is also removed from the in-memory
 * `cards` list so a subsequent listCards() (triggered by refreshCards())
 * reflects the deletion the way the real API would.
 */
function createApiMock(opts: {
  card?: CardDetailOut | null;
  cards?: CardOut[];
  exhibitions?: ExhibitionOut[];
  deleteOutcomes: DeleteOutcome[];
}) {
  let cardsState = [...(opts.cards ?? [])];
  let deleteCallCount = 0;

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : String(input);
    const method = (init?.method ?? "GET").toUpperCase();

    if (method === "GET" && /^\/api\/exhibitions/.test(url)) {
      return jsonResponse(200, opts.exhibitions ?? sampleExhibitions);
    }
    if (method === "GET" && /^\/api\/cards\?/.test(url)) {
      return jsonResponse(200, cardsState);
    }
    if (method === "GET" && /^\/api\/cards\/[^/?]+$/.test(url)) {
      if (!opts.card) return jsonResponse(404, { detail: "not found" });
      return jsonResponse(200, opts.card);
    }
    if (method === "DELETE" && /^\/api\/cards\/[^/?]+/.test(url)) {
      const idx = Math.min(deleteCallCount, opts.deleteOutcomes.length - 1);
      const outcome = opts.deleteOutcomes[idx];
      deleteCallCount += 1;
      if (outcome.status === 204) {
        const match = url.match(/^\/api\/cards\/([^/?]+)/);
        const deletedId = match?.[1];
        cardsState = cardsState.filter((c) => c.card_id !== deletedId);
      }
      return jsonResponse(outcome.status, outcome.body ?? {});
    }
    throw new Error(`Unhandled fetch call in test: ${method} ${url}`);
  });

  return {
    fetchMock,
    getDeleteCallCount: () => deleteCallCount,
  };
}

/**
 * The shared ConfirmDialog always renders exactly "Cancel" plus one other
 * (confirm) button, and only one ConfirmDialog is ever mounted at a time
 * in these flows. Locating the confirm button relative to "Cancel" (rather
 * than by its label, which the spec doesn't pin down) keeps these tests
 * agnostic to the exact confirmLabel copy used for the generic vs cascade
 * prompt.
 */
function getConfirmDialogButtons() {
  const cancelBtn = screen.getByRole("button", { name: "Cancel" });
  const wrapper = cancelBtn.parentElement as HTMLElement;
  const confirmBtn = within(wrapper)
    .getAllByRole("button")
    .find((b) => b !== cancelBtn)!;
  return { cancelBtn, confirmBtn };
}

function noConfirmDialogVisible() {
  return screen.queryByRole("button", { name: "Cancel" }) === null;
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ======================================================================
// CardDetailDrawer — "Delete Card" button
// ======================================================================

describe("CardDetailDrawer delete flow", () => {
  it("shows the generic confirm prompt and makes no API call when Delete Card is clicked", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({
      card: sampleCardDetail,
      deleteOutcomes: [{ status: 204 }],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CardDetailDrawer cardId="card-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    const deleteBtn = await screen.findByRole("button", { name: "Delete Card" });
    await user.click(deleteBtn);

    expect(
      screen.getByRole("button", { name: "Cancel" }),
      "generic confirm prompt should appear before any delete request is sent"
    ).toBeInTheDocument();
    expect(document.body.textContent).toMatch(/can't be undone/i);
    expect(
      fetchMock.mock.calls.some(([, init]) => (init as RequestInit)?.method === "DELETE"),
      "no DELETE request should be sent before the generic confirm is accepted"
    ).toBe(false);
  });

  it("confirming the generic prompt calls deleteCard without confirm_cascade and closes the drawer on success", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onChanged = vi.fn();
    const { fetchMock } = createApiMock({
      card: sampleCardDetail,
      deleteOutcomes: [{ status: 204 }],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CardDetailDrawer cardId="card-1" onClose={onClose} onChanged={onChanged} />);

    await user.click(await screen.findByRole("button", { name: "Delete Card" }));
    const { confirmBtn } = getConfirmDialogButtons();
    await user.click(confirmBtn);

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(onChanged, "parent list should be told to refresh on success").toHaveBeenCalledTimes(1);

    const deleteCalls = fetchMock.mock.calls.filter(
      ([, init]) => (init as RequestInit)?.method === "DELETE"
    );
    expect(deleteCalls).toHaveLength(1);
    const deleteUrl = String(deleteCalls[0][0]);
    expect(deleteUrl).toBe("/api/cards/card-1");
    expect(deleteUrl).not.toContain("confirm_cascade=true");
  });

  it("shows a distinct cascade-specific confirm naming the child count when the delete call returns 409", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({
      card: sampleCardDetail,
      deleteOutcomes: [
        { status: 409, body: { detail: { message: "has children", child_count: 2 } } },
      ],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CardDetailDrawer cardId="card-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Delete Card" }));
    const { confirmBtn: genericConfirmBtn } = getConfirmDialogButtons();
    await user.click(genericConfirmBtn);

    await waitFor(() => {
      expect(document.body.textContent).toMatch(/2/);
    });
    expect(
      document.body.textContent,
      "cascade prompt should mention the merged/duplicate children, not just repeat the generic message"
    ).toMatch(/merged|duplicate|child/i);
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  });

  it("confirming the cascade prompt re-issues the delete with confirm_cascade=true and succeeds", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onChanged = vi.fn();
    const { fetchMock, getDeleteCallCount } = createApiMock({
      card: sampleCardDetail,
      deleteOutcomes: [
        { status: 409, body: { detail: { message: "has children", child_count: 2 } } },
        { status: 204 },
      ],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CardDetailDrawer cardId="card-1" onClose={onClose} onChanged={onChanged} />);

    await user.click(await screen.findByRole("button", { name: "Delete Card" }));
    await user.click(getConfirmDialogButtons().confirmBtn); // generic confirm -> 409

    await waitFor(() => expect(document.body.textContent).toMatch(/2/));
    await user.click(getConfirmDialogButtons().confirmBtn); // cascade confirm -> confirm_cascade=true

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(getDeleteCallCount()).toBe(2);

    const deleteCalls = fetchMock.mock.calls.filter(
      ([, init]) => (init as RequestInit)?.method === "DELETE"
    );
    expect(String(deleteCalls[1][0])).toContain("confirm_cascade=true");
  });

  it("canceling the generic confirm aborts with no request sent and leaves the card in place", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onChanged = vi.fn();
    const { fetchMock } = createApiMock({
      card: sampleCardDetail,
      deleteOutcomes: [{ status: 204 }],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CardDetailDrawer cardId="card-1" onClose={onClose} onChanged={onChanged} />);

    await user.click(await screen.findByRole("button", { name: "Delete Card" }));
    await user.click(getConfirmDialogButtons().cancelBtn);

    await waitFor(() => expect(noConfirmDialogVisible()).toBe(true));
    expect(
      fetchMock.mock.calls.some(([, init]) => (init as RequestInit)?.method === "DELETE")
    ).toBe(false);
    expect(onClose).not.toHaveBeenCalled();
    expect(onChanged).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Delete Card" })).toBeInTheDocument();
  });

  it("canceling the cascade confirm aborts without re-issuing the request and leaves the card untouched", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onChanged = vi.fn();
    const { fetchMock, getDeleteCallCount } = createApiMock({
      card: sampleCardDetail,
      deleteOutcomes: [
        { status: 409, body: { detail: { message: "has children", child_count: 2 } } },
        { status: 204 }, // should never be consumed
      ],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CardDetailDrawer cardId="card-1" onClose={onClose} onChanged={onChanged} />);

    await user.click(await screen.findByRole("button", { name: "Delete Card" }));
    await user.click(getConfirmDialogButtons().confirmBtn); // generic confirm -> 409
    await waitFor(() => expect(document.body.textContent).toMatch(/2/));

    await user.click(getConfirmDialogButtons().cancelBtn); // decline cascade

    await waitFor(() => expect(noConfirmDialogVisible()).toBe(true));
    expect(getDeleteCallCount(), "declining the cascade prompt must not re-issue the request").toBe(1);
    expect(onClose).not.toHaveBeenCalled();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("shows an inline error message (not a crash) when the delete call fails with a generic error", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const { fetchMock } = createApiMock({
      card: sampleCardDetail,
      deleteOutcomes: [{ status: 500, body: { detail: "Something went wrong on the server" } }],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CardDetailDrawer cardId="card-1" onClose={onClose} onChanged={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Delete Card" }));
    await user.click(getConfirmDialogButtons().confirmBtn);

    expect(
      await screen.findByText("Something went wrong on the server")
    ).toBeInTheDocument();
    expect(onClose, "a failed delete must not close the drawer").not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Delete Card" })).toBeInTheDocument();
  });

  it("treats a 409 without a child_count body as a generic failure, not a cascade prompt", async () => {
    // Contract documented in apps/web/lib/api.ts: only a 409 whose body has
    // detail.child_count means "needs cascade confirmation"; any other 409
    // (e.g. a concurrent change) is a plain retryable ApiError.
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({
      card: sampleCardDetail,
      deleteOutcomes: [{ status: 409, body: { detail: "conflict, please retry" } }],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CardDetailDrawer cardId="card-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Delete Card" }));
    await user.click(getConfirmDialogButtons().confirmBtn);

    expect(await screen.findByText("conflict, please retry")).toBeInTheDocument();
    expect(
      noConfirmDialogVisible(),
      "a non-cascade 409 must not surface the cascade confirmation dialog"
    ).toBe(true);
  });
});

// ======================================================================
// Upload page — row-level trash icon
// ======================================================================

describe("Upload page row-level delete flow", () => {
  it("clicking the row's trash icon opens the generic confirm without opening the detail drawer", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({
      card: sampleCardDetail,
      cards: [sampleCard],
      deleteOutcomes: [{ status: 204 }],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);

    await screen.findByText("Jane Doe");
    await user.click(screen.getByRole("button", { name: "Delete card" }));

    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    expect(document.body.textContent).toMatch(/can't be undone/i);
    await waitFor(() => {
      expect(
        screen.queryByText("Card Detail"),
        "the row's own onClick (which opens the drawer) must not fire — the icon click must stop propagation"
      ).not.toBeInTheDocument();
    });
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          (init as RequestInit | undefined)?.method === undefined &&
          /^\/api\/cards\/[^/?]+$/.test(String(url))
      ),
      "the drawer's own card-detail fetch must never have been issued"
    ).toBe(false);
  });

  it("sanity check: clicking the row itself (not the icon) does open the detail drawer", async () => {
    // Establishes that "Card Detail" appearing/not-appearing is a valid
    // signal for whether the drawer opened, so the propagation test above
    // is meaningful rather than vacuously true.
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({
      card: sampleCardDetail,
      cards: [sampleCard],
      deleteOutcomes: [{ status: 204 }],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);

    await user.click(await screen.findByText("Jane Doe"));

    expect(await screen.findByText("Card Detail")).toBeInTheDocument();
  });

  it("confirming the generic prompt via the row icon deletes the card and removes it from the list", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({
      card: sampleCardDetail,
      cards: [sampleCard],
      deleteOutcomes: [{ status: 204 }],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);

    await screen.findByText("Jane Doe");
    await user.click(screen.getByRole("button", { name: "Delete card" }));
    await user.click(getConfirmDialogButtons().confirmBtn);

    await waitFor(() => expect(screen.queryByText("Jane Doe")).not.toBeInTheDocument());
    expect(screen.queryByText("Card Detail")).not.toBeInTheDocument();

    const deleteCalls = fetchMock.mock.calls.filter(
      ([, init]) => (init as RequestInit)?.method === "DELETE"
    );
    expect(deleteCalls).toHaveLength(1);
    expect(String(deleteCalls[0][0])).not.toContain("confirm_cascade=true");
  });

  it("shows the cascade-specific confirm on a row delete's 409, and confirming re-issues with confirm_cascade=true", async () => {
    const user = userEvent.setup();
    const { fetchMock, getDeleteCallCount } = createApiMock({
      card: sampleCardDetail,
      cards: [sampleCard],
      deleteOutcomes: [
        { status: 409, body: { detail: { message: "has children", child_count: 3 } } },
        { status: 204 },
      ],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);

    await screen.findByText("Jane Doe");
    await user.click(screen.getByRole("button", { name: "Delete card" }));
    await user.click(getConfirmDialogButtons().confirmBtn); // generic -> 409

    await waitFor(() => expect(document.body.textContent).toMatch(/3/));
    expect(document.body.textContent).toMatch(/merged|duplicate|child/i);
    expect(screen.getByText("Jane Doe"), "card must remain until cascade is confirmed").toBeInTheDocument();

    await user.click(getConfirmDialogButtons().confirmBtn); // cascade confirm

    await waitFor(() => expect(screen.queryByText("Jane Doe")).not.toBeInTheDocument());
    expect(getDeleteCallCount()).toBe(2);
    const deleteCalls = fetchMock.mock.calls.filter(
      ([, init]) => (init as RequestInit)?.method === "DELETE"
    );
    expect(String(deleteCalls[1][0])).toContain("confirm_cascade=true");
  });

  it("canceling the generic confirm on a row leaves the card in the list with no request sent", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({
      card: sampleCardDetail,
      cards: [sampleCard],
      deleteOutcomes: [{ status: 204 }],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);

    await screen.findByText("Jane Doe");
    await user.click(screen.getByRole("button", { name: "Delete card" }));
    await user.click(getConfirmDialogButtons().cancelBtn);

    await waitFor(() => expect(noConfirmDialogVisible()).toBe(true));
    expect(screen.getByText("Jane Doe")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([, init]) => (init as RequestInit)?.method === "DELETE")
    ).toBe(false);
  });

  it("canceling the cascade confirm on a row leaves the card and its children untouched, with no second request", async () => {
    const user = userEvent.setup();
    const { fetchMock, getDeleteCallCount } = createApiMock({
      card: sampleCardDetail,
      cards: [sampleCard],
      deleteOutcomes: [
        { status: 409, body: { detail: { message: "has children", child_count: 3 } } },
        { status: 204 },
      ],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);

    await screen.findByText("Jane Doe");
    await user.click(screen.getByRole("button", { name: "Delete card" }));
    await user.click(getConfirmDialogButtons().confirmBtn); // generic -> 409
    await waitFor(() => expect(document.body.textContent).toMatch(/3/));

    await user.click(getConfirmDialogButtons().cancelBtn); // decline cascade

    await waitFor(() => expect(noConfirmDialogVisible()).toBe(true));
    expect(screen.getByText("Jane Doe")).toBeInTheDocument();
    expect(getDeleteCallCount()).toBe(1);
  });

  it("shows an inline error banner (not a crash) when a row delete fails with a generic error, and keeps the card listed", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({
      card: sampleCardDetail,
      cards: [sampleCard],
      deleteOutcomes: [{ status: 500, body: { detail: "Delete failed, please retry" } }],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);

    await screen.findByText("Jane Doe");
    await user.click(screen.getByRole("button", { name: "Delete card" }));
    await user.click(getConfirmDialogButtons().confirmBtn);

    expect(await screen.findByText("Delete failed, please retry")).toBeInTheDocument();
    expect(screen.getByText("Jane Doe")).toBeInTheDocument();
  });
});
