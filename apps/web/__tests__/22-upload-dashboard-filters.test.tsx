// Tests for the `22-upload-dashboard-filters` feature (spec:
// `.claude/specs/22-upload-dashboard-filters.md`), written directly against
// the spec's documented frontend contract for `/upload`:
//
//   - `/upload` gains the same date-range preset filter `/dashboard` already
//     has (Last 30 days / Last 90 days / Last 1 year / All time / Custom
//     range), reusing `RANGE_OPTIONS`/`rangeToDates` from
//     `dashboard-filter-bar.tsx` rather than a second date-picker
//     implementation — but defaults to "All time" (not "Last 30 days" like
//     /dashboard), since /upload historically shows every un-actioned card
//     with no date scoping and a 30-day default would silently hide older
//     cards a seller still needs to parse/enrich/score.
//   - Switching the preset (or setting a custom range) re-fetches
//     `GET /cards` with matching `start_date`/`end_date` query params.
//   - The existing admin-only "Uploaded by" control (already live on
//     /upload before this feature) is now rendered via the shared
//     `UploadedByFilter` component from `dashboard-filter-bar.tsx`, so its
//     behavior — hidden for non-admins and for an admin whose org has only
//     themself, otherwise populated from `GET /orgs/members` and narrowing
//     `GET /cards` by `user_id` — must be unchanged.
//
// `global.fetch` is mocked end-to-end for every test in this file, matching
// this codebase's established convention (`09-bulk-select-parse-enrich.test.tsx`,
// `16-dashboard-analytics.test.tsx`).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import UploadPage from "@/app/upload/page";
import type { CardOut, ExhibitionOut, OrgMemberOut, UserOut } from "@/lib/api";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
}));

const sampleUser: UserOut = {
  user_id: "user-1",
  name: "Priya Sharma",
  email: "priya@example.com",
  phone_no: null,
  org_id: null,
  org_name: null,
  role: null,
  phone_verified: true,
  is_active: true,
  admin_name: null,
  admin_email: null,
};

const adminUser: UserOut = {
  ...sampleUser,
  user_id: "admin-1",
  name: "Admin Alex",
  email: "admin@example.com",
  org_id: "org-1",
  role: "admin",
};

const sampleOrgMembers: OrgMemberOut[] = [
  {
    user_id: "admin-1",
    name: "Admin Alex",
    email: "admin@example.com",
    role: "admin",
    phone_no: null,
    phone_verified: true,
    is_active: true,
    created_at: "2026-01-01T00:00:00Z",
  },
  {
    user_id: "member-1",
    name: "Member Mira",
    email: "mira@example.com",
    role: "member",
    phone_no: null,
    phone_verified: true,
    is_active: true,
    created_at: "2026-01-02T00:00:00Z",
  },
];

const sampleExhibitions: ExhibitionOut[] = [];

function makeCard(card_id: string, full_name: string, user_id = "user-1"): CardOut {
  return {
    card_id,
    user_id,
    exhibition_id: null,
    original_filename: `${card_id}.jpg`,
    image_url: `https://example.com/${card_id}.jpg`,
    status: "new",
    full_name,
    job_title: null,
    merged_into_card_id: null,
    created_at: "2026-07-01T00:00:00Z",
    company_id: null,
    company_name: null,
    company_enrichment_status: null,
    lead_score: null,
    score_breakdown: null,
    scored_at: null,
    rescore_available: false,
  };
}

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

// Dispatches by method+URL. Unlike 09-bulk-select-parse-enrich.test.tsx's
// mock (which ignores GET /cards query params entirely), this one records
// every GET /cards URL so tests can assert on start_date/end_date/user_id.
function createApiMock(opts: {
  user?: UserOut;
  cards?: CardOut[];
  orgMembers?: OrgMemberOut[];
}) {
  const cardsCalls: string[] = [];

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : String(input);
    const method = (init?.method ?? "GET").toUpperCase();

    if (method === "GET" && url === "/api/auth/me") {
      return jsonResponse(200, opts.user ?? sampleUser);
    }
    if (method === "GET" && /^\/api\/exhibitions/.test(url)) {
      return jsonResponse(200, sampleExhibitions);
    }
    if (method === "GET" && url === "/api/wallet") {
      return jsonResponse(200, {
        balance_inr: "0",
        currency: "INR",
        transactions: [],
        free_actions_remaining: { parse: 20, enrichment: 20, scoring: 20 },
      });
    }
    if (method === "GET" && url === "/api/orgs/members") {
      return jsonResponse(200, opts.orgMembers ?? []);
    }
    if (method === "GET" && /^\/api\/cards\?/.test(url)) {
      cardsCalls.push(url);
      return jsonResponse(200, opts.cards ?? []);
    }
    throw new Error(`Unhandled fetch call in test: ${method} ${url}`);
  });

  return { fetchMock, cardsCalls };
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ==========================================================================
// 1. Date-range filter — defaults to "All time", options match /dashboard's,
//    switching presets/custom re-fetches GET /cards with matching dates.
// ==========================================================================

describe("Upload page date-range filter", () => {
  it("defaults to 'All time' and sends no start_date/end_date on the first fetch", async () => {
    const { fetchMock, cardsCalls } = createApiMock({ cards: [makeCard("card-1", "Alice")] });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Alice");

    expect(screen.getByDisplayValue("All time")).toBeInTheDocument();
    expect(cardsCalls[cardsCalls.length - 1]).not.toContain("start_date=");
    expect(cardsCalls[cardsCalls.length - 1]).not.toContain("end_date=");
  });

  it("offers the same five presets as /dashboard", async () => {
    const { fetchMock } = createApiMock({ cards: [] });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Date range");

    for (const label of ["Last 30 days", "Last 90 days", "Last 1 year", "All time", "Custom range"]) {
      expect(screen.getByText(label, { selector: "option" })).toBeInTheDocument();
    }
  });

  it("selecting 'Last 30 days' re-fetches GET /cards with start_date and end_date", async () => {
    const user = userEvent.setup();
    const { fetchMock, cardsCalls } = createApiMock({ cards: [makeCard("card-1", "Alice")] });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Alice");

    await user.selectOptions(screen.getByDisplayValue("All time"), "30d");

    await vi.waitFor(() => {
      const lastCall = cardsCalls[cardsCalls.length - 1];
      expect(lastCall).toContain("start_date=");
      expect(lastCall).toContain("end_date=");
    });
  });

  it("selecting 'Custom range' reveals date pickers that drive the fetch once both are set", async () => {
    const user = userEvent.setup();
    const { fetchMock, cardsCalls } = createApiMock({ cards: [makeCard("card-1", "Alice")] });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Alice");

    await user.selectOptions(screen.getByDisplayValue("All time"), "custom");

    const startInput = await screen.findByLabelText("Custom range start date");
    const endInput = screen.getByLabelText("Custom range end date");
    await user.type(startInput, "2026-06-01");
    await user.type(endInput, "2026-06-30");

    await vi.waitFor(() => {
      const lastCall = cardsCalls[cardsCalls.length - 1];
      expect(lastCall).toContain("start_date=2026-06-01");
      expect(lastCall).toContain("end_date=2026-06-30");
    });
  });
});

// ==========================================================================
// 2. "Uploaded by" filter — pre-existing admin-only control, now rendered
//    via the shared UploadedByFilter component; behavior must be unchanged.
// ==========================================================================

describe("Upload page uploaded-by filter (admin-only, unchanged behavior)", () => {
  it("never renders the Uploaded by control for a non-admin user", async () => {
    const { fetchMock } = createApiMock({ user: sampleUser, cards: [makeCard("card-1", "Alice")] });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Alice");

    expect(screen.queryByText("Uploaded by")).not.toBeInTheDocument();
  });

  it("never renders the Uploaded by control for an admin whose org has only themself", async () => {
    const { fetchMock } = createApiMock({
      user: adminUser,
      orgMembers: [sampleOrgMembers[0]],
      cards: [makeCard("card-1", "Alice", "admin-1")],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Alice");

    expect(screen.queryByText("Uploaded by")).not.toBeInTheDocument();
  });

  it("renders Uploaded by for an admin with other org members, and narrows GET /cards by user_id", async () => {
    const user = userEvent.setup();
    const { fetchMock, cardsCalls } = createApiMock({
      user: adminUser,
      orgMembers: sampleOrgMembers,
      cards: [makeCard("card-1", "Alice", "admin-1"), makeCard("card-2", "Bob", "member-1")],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Alice");

    const uploadedBySelect = await screen.findByDisplayValue("All users");
    await user.selectOptions(uploadedBySelect, "member-1");

    await vi.waitFor(() => {
      const lastCall = cardsCalls[cardsCalls.length - 1];
      expect(lastCall).toContain("user_id=member-1");
    });
  });
});

// ==========================================================================
// 3. Combining both filters narrows to their intersection.
// ==========================================================================

describe("Combining date-range and uploaded-by filters", () => {
  it("sends both start_date/end_date and user_id together once both are set", async () => {
    const user = userEvent.setup();
    const { fetchMock, cardsCalls } = createApiMock({
      user: adminUser,
      orgMembers: sampleOrgMembers,
      cards: [makeCard("card-1", "Alice", "admin-1"), makeCard("card-2", "Bob", "member-1")],
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<UploadPage />);
    await screen.findByText("Alice");

    await user.selectOptions(screen.getByDisplayValue("All time"), "30d");
    await user.selectOptions(await screen.findByDisplayValue("All users"), "member-1");

    await vi.waitFor(() => {
      const lastCall = cardsCalls[cardsCalls.length - 1];
      expect(lastCall).toContain("start_date=");
      expect(lastCall).toContain("end_date=");
      expect(lastCall).toContain("user_id=member-1");
    });
  });
});
