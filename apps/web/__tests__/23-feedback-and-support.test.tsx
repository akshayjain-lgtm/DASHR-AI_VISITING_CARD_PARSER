// Tests for the 23-feedback-and-support feature's frontend surface, per
// .claude/specs/23-feedback-and-support.md:
//   - Sidebar carries a "Feedback" nav item just below FAQ, routing to
//     /feedback.
//   - The Feedback page's feedback form blocks submit when both fields are
//     blank (no fetch call), and POSTs /feedback with whichever field(s)
//     were filled in, then shows a confirmation.
//   - The Feedback page's "raise a query" form POSTs /feedback/queries and
//     renders the ticket id the API returns.
//
// global.fetch is mocked end-to-end (never hits a real server), following
// the same dispatch-on-(method, relative URL) convention as
// 20-field-correction.test.tsx.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import FeedbackPage from "@/app/feedback/page";
import { Sidebar } from "@/components/sidebar";

const pushMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn(), back: vi.fn() }),
}));

beforeEach(() => {
  pushMock.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---- fetch mock plumbing --------------------------------------------------

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

function createApiMock() {
  const feedbackCalls: { body: unknown }[] = [];
  const queryCalls: { body: unknown }[] = [];

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : String(input);
    const method = (init?.method ?? "GET").toUpperCase();

    if (method === "POST" && url === "/api/feedback") {
      feedbackCalls.push({ body: init?.body ? JSON.parse(String(init.body)) : {} });
      return jsonResponse(204, undefined);
    }
    if (method === "POST" && url === "/api/feedback/queries") {
      queryCalls.push({ body: init?.body ? JSON.parse(String(init.body)) : {} });
      return jsonResponse(200, { ticket_id: "DASHR-TKT-000042", created_at: "2026-07-22T00:00:00Z" });
    }
    throw new Error(`Unhandled fetch call in test: ${method} ${url}`);
  });

  return { fetchMock, feedbackCalls, queryCalls };
}

// ==========================================================================
// 1. Sidebar entry point
// ==========================================================================

describe("Sidebar -- Feedback nav item", () => {
  it("shows Feedback just below FAQ and routes to /feedback", async () => {
    const user = userEvent.setup();
    render(<Sidebar active="dashboard" />);

    const buttons = screen.getAllByRole("button");
    const labels = buttons.map((b) => b.textContent?.trim());
    const faqIndex = labels.findIndex((l) => l === "FAQ");
    const feedbackIndex = labels.findIndex((l) => l === "Feedback");
    expect(faqIndex).toBeGreaterThanOrEqual(0);
    expect(feedbackIndex).toBe(faqIndex + 1);

    await user.click(screen.getByRole("button", { name: /feedback/i }));
    expect(pushMock).toHaveBeenCalledWith("/feedback");
  });
});

// ==========================================================================
// 2. Feedback form
// ==========================================================================

describe("/feedback page -- Feedback section", () => {
  it("does not call the API when both fields are left blank", async () => {
    const { fetchMock, feedbackCalls } = createApiMock();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(<FeedbackPage />);
    await user.click(screen.getByRole("button", { name: /submit feedback/i }));

    expect(feedbackCalls).toHaveLength(0);
    expect(screen.getByText(/tell us at least one/i)).toBeInTheDocument();
  });

  it("submits whichever field was filled in and shows a confirmation", async () => {
    const { fetchMock, feedbackCalls } = createApiMock();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(<FeedbackPage />);
    await user.type(screen.getByLabelText(/what went wrong/i), "Extraction missed my phone number");
    await user.click(screen.getByRole("button", { name: /submit feedback/i }));

    expect(await screen.findByText(/thanks — noted/i)).toBeInTheDocument();
    expect(feedbackCalls).toEqual([
      { body: { what_worked: undefined, what_went_wrong: "Extraction missed my phone number" } },
    ]);
  });
});

// ==========================================================================
// 3. Raise a query form
// ==========================================================================

describe("/feedback page -- Raise a Query section", () => {
  it("submits the query and renders the returned ticket id", async () => {
    const { fetchMock, queryCalls } = createApiMock();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(<FeedbackPage />);
    await user.type(screen.getByLabelText(/subject/i), "Wallet recharge failed");
    await user.type(screen.getByLabelText(/message/i), "Payment succeeded but balance didn't update");
    await user.click(screen.getByRole("button", { name: /raise query/i }));

    expect(await screen.findByText(/DASHR-TKT-000042/)).toBeInTheDocument();
    expect(queryCalls).toEqual([
      {
        body: {
          subject: "Wallet recharge failed",
          message: "Payment succeeded but balance didn't update",
        },
      },
    ]);
  });
});
