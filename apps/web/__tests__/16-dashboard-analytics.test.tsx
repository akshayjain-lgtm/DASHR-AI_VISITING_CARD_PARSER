// Tests for the `16-dashboard-analytics` feature (spec:
// `.claude/specs/16-dashboard-analytics.md`), written directly against the
// spec's documented frontend contract for `/dashboard`, not against the
// current implementation of `app/dashboard/page.tsx`:
//
//   - The page is a *pure analytics surface*: a stat band at the top
//     (Total Leads only -- High Fit/Low Fit tiles are removed for the time
//     being), a filter bar (multi-select exhibitions + a time-range preset)
//     above a chart grid of six charts (Lead Volume, Industry Mix, Score
//     Distribution, Exhibition Performance, Role Mix, Region Mix), all fed
//     by `getDashboardAnalytics()` (-> `GET /analytics/dashboard`).
//   - The lead table, its name/company search box, the non-functional
//     "Filter" button, and all `CardDetailDrawer` wiring have been removed
//     entirely from this page -- per-card review lives on `/upload` instead.
//   - The exhibition filter is a *multi-select* (not a native
//     `<select multiple>`): empty selection means "all exhibitions", the
//     trigger label summarizes the selection, and selected ids are sent as
//     repeated `exhibition_ids` query params.
//   - The time-range preset defaults to "Last 30 days" on first load.
//     Options are Last 30 days / Last 90 days / Last 1 year / All time /
//     Custom range; selecting Custom range reveals explicit start/end date
//     pickers that drive the fetch once both are set.
//   - Changing any filter control re-fetches and re-renders every chart
//     from the same slice, so all chart numbers always agree with each
//     other (no stale chart left showing a prior filter's data).
//   - Each chart renders a sensible empty state for a zero-lead account
//     instead of crashing.
//
// `global.fetch` is mocked end-to-end for every test in this file -- nothing
// here ever hits a real network call (no OCR/enrichment/website-fetch
// provider is even reachable from this page, but the general project rule
// still applies: mock all external calls, including this page's own backend
// calls via `fetch`).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Dashboard from "@/app/dashboard/page";
import type { DashboardAnalyticsOut, ExhibitionOut, UserOut } from "@/lib/api";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
}));

const sampleUser: UserOut = {
  user_id: "user-1",
  name: "Priya Sharma",
  email: "priya@example.com",
  phone_no: null,
  org_id: null,
  role: null,
  phone_verified: true,
};

const sampleExhibitions: ExhibitionOut[] = [
  {
    exhibition_id: "expo-1",
    name: "Auto Expo 2026",
    location: "Mumbai",
    start_date: "2026-06-01",
    end_date: "2026-06-03",
    created_at: "2026-05-01T00:00:00Z",
  },
  {
    exhibition_id: "expo-2",
    name: "Industrial Fair 2026",
    location: "Pune",
    start_date: "2026-06-10",
    end_date: "2026-06-12",
    created_at: "2026-05-05T00:00:00Z",
  },
];

const emptyAnalytics: DashboardAnalyticsOut = {
  lead_volume: [],
  industry_mix: [],
  score_distribution: { high: 0, medium: 0, low: 0, unscored: 0 },
  exhibition_performance: [],
  role_mix: [],
  region_mix: [],
};

const fullAccountAnalytics: DashboardAnalyticsOut = {
  lead_volume: [
    { date: "2026-06-01", count: 3 },
    { date: "2026-06-02", count: 5 },
  ],
  industry_mix: [
    { industry: "Automotive & Auto Components", count: 4 },
    { industry: "Unclassified", count: 2 },
  ],
  score_distribution: { high: 2, medium: 3, low: 1, unscored: 1 }, // total = 7
  exhibition_performance: [{ exhibition_id: "expo-1", exhibition_name: "Auto Expo 2026", lead_count: 6 }],
  role_mix: [
    { role: "c_level", count: 3 },
    { role: "Unclassified", count: 4 },
  ],
  region_mix: [
    { region: "Maharashtra", count: 5 },
    { region: "Unclassified", count: 2 },
  ],
};

// A distinct dataset a filter change should switch to, so tests can prove
// every chart re-fetches to the *new* slice rather than staying stale.
const filteredAnalytics: DashboardAnalyticsOut = {
  lead_volume: [{ date: "2026-06-01", count: 1 }],
  industry_mix: [{ industry: "Automotive & Auto Components", count: 1 }],
  score_distribution: { high: 1, medium: 0, low: 0, unscored: 0 }, // total = 1
  exhibition_performance: [{ exhibition_id: "expo-1", exhibition_name: "Auto Expo 2026", lead_count: 1 }],
  role_mix: [{ role: "c_level", count: 1 }],
  region_mix: [{ region: "Maharashtra", count: 1 }],
};

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

// Dispatches by method+URL, mirroring this codebase's established fetch-mock
// convention (`10-lead-scoring.test.tsx`). `analyticsResponder` lets a test
// script different responses across successive analytics fetches (e.g. to
// prove a filter change swaps in a genuinely different slice).
function createApiMock(opts: {
  exhibitions?: ExhibitionOut[];
  analyticsResponder: (url: string, callIndex: number) => DashboardAnalyticsOut;
}) {
  const analyticsCalls: string[] = [];

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : String(input);
    const method = (init?.method ?? "GET").toUpperCase();

    if (method === "GET" && url === "/api/auth/me") {
      return jsonResponse(200, sampleUser);
    }
    if (method === "GET" && url === "/api/exhibitions") {
      return jsonResponse(200, opts.exhibitions ?? []);
    }
    if (method === "GET" && /^\/api\/analytics\/dashboard(\?.*)?$/.test(url)) {
      const callIndex = analyticsCalls.length;
      analyticsCalls.push(url);
      return jsonResponse(200, opts.analyticsResponder(url, callIndex));
    }
    throw new Error(`Unhandled fetch call in test: ${method} ${url}`);
  });

  return { fetchMock, analyticsCalls };
}

function staticAnalytics(data: DashboardAnalyticsOut) {
  return createApiMock({ analyticsResponder: () => data });
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ==========================================================================
// 1. Page structure -- stat band, filter bar, six charts; no removed
//    surfaces (table/search/drawer/High-Low tiles).
// ==========================================================================

describe("Dashboard page structure", () => {
  it("renders the Total Leads stat derived from score_distribution, with no High/Low Fit tiles", async () => {
    const { fetchMock } = staticAnalytics(fullAccountAnalytics);
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);

    await screen.findByText("Total Leads");
    // 2 high + 3 medium + 1 low + 1 unscored = 7.
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.queryByText("High Fit")).not.toBeInTheDocument();
    expect(screen.queryByText("Low Fit")).not.toBeInTheDocument();
  });

  it("renders all six chart sections once analytics resolves", async () => {
    const { fetchMock } = staticAnalytics(fullAccountAnalytics);
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);

    expect(await screen.findByText("Lead Volume")).toBeInTheDocument();
    expect(screen.getByText("Industry Mix")).toBeInTheDocument();
    expect(screen.getByText("Score Distribution")).toBeInTheDocument();
    expect(screen.getByText("Exhibition Performance")).toBeInTheDocument();
    expect(screen.getByText("Role Mix")).toBeInTheDocument();
    expect(screen.getByText("Region Mix")).toBeInTheDocument();
  });

  it("never renders a lead table, a name/company search box, or a card detail drawer", async () => {
    const { fetchMock } = staticAnalytics(fullAccountAnalytics);
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);
    await screen.findByText("Total Leads");

    // No searchbox of any kind on this page anymore.
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/search/i)).not.toBeInTheDocument();
    // No per-row lead table (would render column headers like these).
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText(/^UNSCORED$/)).not.toBeInTheDocument();
    // No leftover non-functional "Filter" button from the old table toolbar.
    expect(screen.queryByRole("button", { name: /^Filter$/ })).not.toBeInTheDocument();
  });
});

// ==========================================================================
// 2. Empty state -- a zero-lead account must not crash and must show some
//    sensible "no data" indication for every chart.
// ==========================================================================

describe("Empty account", () => {
  it("renders without crashing and shows a Total Leads count of 0 for a zero-card account", async () => {
    const { fetchMock } = staticAnalytics(emptyAnalytics);
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);

    await screen.findByText("Total Leads");
    expect(screen.getByText("0")).toBeInTheDocument();
  });

  it("shows a sensible empty-state indicator (not a blank/broken chart) in every one of the six chart sections", async () => {
    const { fetchMock } = staticAnalytics(emptyAnalytics);
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);
    await screen.findByText("Total Leads");

    const chartTitles = [
      "Lead Volume",
      "Industry Mix",
      "Score Distribution",
      "Exhibition Performance",
      "Role Mix",
      "Region Mix",
    ];

    for (const title of chartTitles) {
      const heading = screen.getByText(title);
      // Walk up to the chart's card container and assert it contains some
      // "no data yet" style copy rather than an empty/broken render.
      const container = heading.closest("div")?.parentElement ?? heading.parentElement;
      expect(container).not.toBeNull();
      expect(within(container as HTMLElement).getByText(/no .*yet/i)).toBeInTheDocument();
    }
  });
});

// ==========================================================================
// 3. Time-range preset -- defaults to Last 30 days, options match spec,
//    Custom range reveals date pickers that drive the fetch once both set.
// ==========================================================================

describe("Time range filter", () => {
  it("defaults to 'Last 30 days' and includes start_date/end_date on the very first fetch", async () => {
    const { fetchMock, analyticsCalls } = staticAnalytics(fullAccountAnalytics);
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);
    await screen.findByText("Total Leads");

    expect(screen.getByDisplayValue("Last 30 days")).toBeInTheDocument();
    expect(analyticsCalls[0]).toContain("start_date=");
    expect(analyticsCalls[0]).toContain("end_date=");
  });

  it("offers exactly the documented preset options", async () => {
    const { fetchMock } = staticAnalytics(fullAccountAnalytics);
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);
    await screen.findByText("Total Leads");

    const select = screen.getByDisplayValue("Last 30 days") as HTMLSelectElement;
    const optionLabels = Array.from(select.options).map((o) => o.textContent);
    expect(optionLabels).toEqual(["Last 30 days", "Last 90 days", "Last 1 year", "All time", "Custom range"]);
  });

  it("reveals start/end date pickers only after 'Custom range' is selected", async () => {
    const user = userEvent.setup();
    const { fetchMock } = staticAnalytics(fullAccountAnalytics);
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);
    await screen.findByText("Total Leads");

    expect(screen.queryByLabelText("Custom range start date")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Custom range end date")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByDisplayValue("Last 30 days"), "custom");

    expect(await screen.findByLabelText("Custom range start date")).toBeInTheDocument();
    expect(screen.getByLabelText("Custom range end date")).toBeInTheDocument();
  });

  it("re-fetches with the chosen custom start/end dates once both are set, and every chart reflects the new slice", async () => {
    const user = userEvent.setup();
    const { fetchMock, analyticsCalls } = createApiMock({
      analyticsResponder: (_url, callIndex) => (callIndex === 0 ? fullAccountAnalytics : filteredAnalytics),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);
    await screen.findByText("Total Leads");
    expect(screen.getByText("7")).toBeInTheDocument(); // initial full-account total

    await user.selectOptions(screen.getByDisplayValue("Last 30 days"), "custom");
    await user.type(await screen.findByLabelText("Custom range start date"), "2026-01-01");
    await user.type(screen.getByLabelText("Custom range end date"), "2026-01-31");

    const lastCall = analyticsCalls[analyticsCalls.length - 1];
    expect(lastCall).toContain("start_date=2026-01-01");
    expect(lastCall).toContain("end_date=2026-01-31");

    // The stat band (and, by extension, every chart fed by the same
    // `analytics` state) now reflects the new, filtered slice -- no chart
    // is left showing the prior slice's numbers.
    await screen.findByText("1");
    expect(screen.queryByText("7")).not.toBeInTheDocument();
  });

  it("selecting 'All time' omits both start_date and end_date from the fetch", async () => {
    const user = userEvent.setup();
    const { fetchMock, analyticsCalls } = staticAnalytics(fullAccountAnalytics);
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);
    await screen.findByText("Total Leads");

    await user.selectOptions(screen.getByDisplayValue("Last 30 days"), "all");

    const lastCall = analyticsCalls[analyticsCalls.length - 1];
    expect(lastCall).not.toContain("start_date=");
    expect(lastCall).not.toContain("end_date=");
  });
});

// ==========================================================================
// 4. Exhibition multi-select filter.
// ==========================================================================

describe("Exhibition multi-select filter", () => {
  it("defaults to 'All exhibitions' with no exhibition_ids sent on first fetch", async () => {
    const { fetchMock, analyticsCalls } = createApiMock({
      exhibitions: sampleExhibitions,
      analyticsResponder: () => fullAccountAnalytics,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);
    await screen.findByText("Total Leads");

    expect(screen.getByRole("button", { name: "All exhibitions" })).toBeInTheDocument();
    expect(analyticsCalls[0]).not.toContain("exhibition_ids=");
  });

  it("is a checkbox-based multi-select listbox, not a native <select multiple>", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({
      exhibitions: sampleExhibitions,
      analyticsResponder: () => fullAccountAnalytics,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);
    await screen.findByText("Total Leads");

    const trigger = screen.getByRole("button", { name: "All exhibitions" });
    expect(trigger).toHaveAttribute("aria-haspopup", "listbox");

    await user.click(trigger);
    const listbox = await screen.findByRole("listbox");
    expect(listbox).toHaveAttribute("aria-multiselectable", "true");
    expect(within(listbox).getAllByRole("checkbox").length).toBeGreaterThan(0);
  });

  it("sends selected exhibitions as repeated exhibition_ids params, additively (not replacing)", async () => {
    const user = userEvent.setup();
    const { fetchMock, analyticsCalls } = createApiMock({
      exhibitions: sampleExhibitions,
      analyticsResponder: () => fullAccountAnalytics,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);
    await screen.findByText("Total Leads");
    expect(analyticsCalls).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "All exhibitions" }));
    await user.click(await screen.findByText("Auto Expo 2026"));

    expect(analyticsCalls).toHaveLength(2);
    expect(analyticsCalls[1]).toContain("exhibition_ids=expo-1");
    expect(analyticsCalls[1]).not.toContain("exhibition_ids=expo-2");

    await user.click(await screen.findByText("Industrial Fair 2026"));

    expect(analyticsCalls).toHaveLength(3);
    expect(analyticsCalls[2]).toContain("exhibition_ids=expo-1");
    expect(analyticsCalls[2]).toContain("exhibition_ids=expo-2");
  });

  it("summarizes the trigger label based on selection count (one name vs. 'N exhibitions')", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({
      exhibitions: sampleExhibitions,
      analyticsResponder: () => fullAccountAnalytics,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);
    await screen.findByText("Total Leads");

    await user.click(screen.getByRole("button", { name: "All exhibitions" }));
    await user.click(await screen.findByText("Auto Expo 2026"));
    expect(await screen.findByRole("button", { name: "Auto Expo 2026" })).toBeInTheDocument();

    // The dropdown stays open across selections (only closed by clicking
    // outside), so the second exhibition can be selected without reopening.
    await user.click(await screen.findByText("Industrial Fair 2026"));
    expect(await screen.findByRole("button", { name: "2 exhibitions" })).toBeInTheDocument();
  });

  it("re-selecting the 'All exhibitions' checkbox clears the selection back to empty", async () => {
    const user = userEvent.setup();
    const { fetchMock, analyticsCalls } = createApiMock({
      exhibitions: sampleExhibitions,
      analyticsResponder: () => fullAccountAnalytics,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);
    await screen.findByText("Total Leads");

    await user.click(screen.getByRole("button", { name: "All exhibitions" }));
    await user.click(await screen.findByText("Auto Expo 2026"));
    expect(await screen.findByRole("button", { name: "Auto Expo 2026" })).toBeInTheDocument();

    // Dropdown is still open from the click above (only closes on an
    // outside click) -- select the "All exhibitions" option directly.
    await user.click(screen.getByText("All exhibitions", { selector: "label" }));

    expect(await screen.findByRole("button", { name: "All exhibitions" })).toBeInTheDocument();
    const lastCall = analyticsCalls[analyticsCalls.length - 1];
    expect(lastCall).not.toContain("exhibition_ids=");
  });
});

// ==========================================================================
// 5. Filter-composition consistency -- every chart is fed from the same
//    filtered slice, never a mix of old and new data.
// ==========================================================================

describe("Filter composition consistency", () => {
  it("re-fetches all six charts together from the same new slice when the exhibition filter changes", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({
      exhibitions: sampleExhibitions,
      analyticsResponder: (_url, callIndex) => (callIndex === 0 ? fullAccountAnalytics : filteredAnalytics),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<Dashboard />);
    await screen.findByText("Total Leads");
    expect(screen.getByText("7")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "All exhibitions" }));
    await user.click(await screen.findByText("Auto Expo 2026"));

    // A single fetch backs every chart -- the Total Leads stat (derived from
    // the same `analytics` object every chart reads) flips to the new
    // slice's total, proving no chart is left stale.
    await screen.findByText("1");
    expect(screen.queryByText("7")).not.toBeInTheDocument();
  });
});
