// Tests for the `18-faq` feature (spec: `.claude/specs/18-faq.md`), written
// directly against the spec's documented frontend contract:
//
//   - `/faq` is a public, static page: no auth check, no data fetching,
//     content grouped into categories, each question expands/collapses its
//     answer independently.
//   - The homepage navbar and footer both carry a working "FAQ" link.
//   - The authenticated app's Sidebar carries its own top-level "FAQ" nav
//     item, alongside Wallet and Settings — not nested inside Settings.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import FaqPage from "@/app/(marketing)/faq/page";
import HomePage from "@/app/page";
import { Navbar } from "@/components/navbar";
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

// ==========================================================================
// 1. /faq page
// ==========================================================================

describe("/faq page", () => {
  it("renders every FAQ category with no fetch calls", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(<FaqPage />);

    expect(screen.getByText("Getting Started")).toBeInTheDocument();
    expect(screen.getByText("Extraction & Enrichment")).toBeInTheDocument();
    expect(screen.getByText("Lead Scoring")).toBeInTheDocument();
    expect(screen.getByText("Wallet & Billing")).toBeInTheDocument();
    expect(screen.getByText("Team & Roles")).toBeInTheDocument();
    expect(screen.queryByText("Data & Security")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not claim cards can be manually corrected after a misread", async () => {
    const user = userEvent.setup();
    render(<FaqPage />);

    await user.click(screen.getByText("What if the AI misreads a card, like bad handwriting?"));
    expect(screen.getByText(/no manual field-correction tool today/i)).toBeInTheDocument();
    expect(screen.queryByText(/review and correct any field/i)).not.toBeInTheDocument();
  });

  it("explains the Admin role", async () => {
    const user = userEvent.setup();
    render(<FaqPage />);

    await user.click(screen.getByText("What's the difference between an Admin and a team member?"));
    expect(screen.getByText(/only controls data visibility/i)).toBeInTheDocument();
  });

  it("expands an answer on click and collapses it on a second click", async () => {
    const user = userEvent.setup();
    render(<FaqPage />);

    const question = screen.getByText("Is there a free tier?");
    expect(
      screen.queryByText(/first 20 parses, 20 enrichments, and 20 scorings free/i)
    ).not.toBeInTheDocument();

    await user.click(question);
    expect(
      screen.getByText(/first 20 parses, 20 enrichments, and 20 scorings free/i)
    ).toBeInTheDocument();

    await user.click(question);
    expect(
      screen.queryByText(/first 20 parses, 20 enrichments, and 20 scorings free/i)
    ).not.toBeInTheDocument();
  });

  it("routes to /product from the closing CTA", async () => {
    const user = userEvent.setup();
    render(<FaqPage />);

    // Navbar (rendered on every page) has its own "Try Demo" button, so the
    // FAQ page has two — this exercises the closing CTA's, not the navbar's.
    const demoButtons = screen.getAllByRole("button", { name: /try demo/i });
    await user.click(demoButtons[demoButtons.length - 1]);
    expect(pushMock).toHaveBeenCalledWith("/product");
  });
});

// ==========================================================================
// 2. Navbar + homepage footer links (public/marketing pages)
// ==========================================================================

describe("FAQ entry points on public pages", () => {
  it("navbar FAQ link routes to /faq", async () => {
    const user = userEvent.setup();
    render(<Navbar />);

    await user.click(screen.getByRole("button", { name: "FAQ" }));
    expect(pushMock).toHaveBeenCalledWith("/faq");
  });

  it("homepage footer FAQ link routes to /faq", async () => {
    const user = userEvent.setup();
    render(<HomePage />);

    const faqLinks = screen.getAllByRole("button", { name: "FAQ" });
    await user.click(faqLinks[faqLinks.length - 1]);
    expect(pushMock).toHaveBeenCalledWith("/faq");
  });
});

// ==========================================================================
// 3. Sidebar FAQ entry point (authenticated app)
// ==========================================================================

describe("Sidebar -- FAQ nav item", () => {
  it("shows FAQ as its own item, alongside Wallet and Settings, and routes to /faq", async () => {
    const user = userEvent.setup();
    render(<Sidebar active="dashboard" />);

    expect(screen.getByRole("button", { name: /wallet/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /settings/i })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /faq/i }));
    expect(pushMock).toHaveBeenCalledWith("/faq");
  });
});
