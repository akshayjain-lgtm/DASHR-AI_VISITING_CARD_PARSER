// Tests for the 14-wallet-recharge feature's frontend surface, per
// .claude/specs/14-wallet-recharge.md:
//   - the Wallet page renders the current balance and transaction history
//     fetched from GET /wallet and GET /wallet/transactions
//   - clicking "Add Money" calls POST /wallet/recharge, then opens Razorpay
//     Checkout (window.Razorpay(...).open()) with the order details the
//     backend returned — never credits anything client-side
//   - a failed recharge call surfaces an error message instead of opening
//     Checkout
//
// global.fetch is mocked end-to-end (never hits a real server), dispatching
// on method + relative URL, matching apps/web/lib/api.ts's actual request
// shapes — same pattern as 10-lead-scoring.test.tsx. window.Razorpay is
// stubbed as a vi.fn() constructor so tests can assert what it was called
// with and that .open() ran, without ever loading the real Checkout script.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import WalletPage from "@/app/wallet/page";
import type { WalletOut, WalletTransactionOut } from "@/lib/api";

// WalletPage renders <Sidebar>, which calls next/navigation's useRouter().
// That throws outside a real Next.js app-router tree, so it must be mocked.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
}));

// next/script's <Script> is irrelevant to what these tests assert (loading
// the real Razorpay checkout.js would be a network call) — render it as an
// inert element so it never fires an actual script load.
vi.mock("next/script", () => ({
  default: () => null,
}));

// ---- fixtures ----------------------------------------------------------

const emptyWallet: WalletOut = {
  balance_inr: "0",
  currency: "INR",
  transactions: [],
  free_actions_remaining: { parse: 20, enrichment: 20, scoring: 20 },
};

function makeTransaction(params: {
  wallet_transaction_id: string;
  transaction_type: string;
  amount_inr: string;
  balance_after_inr: string;
  quantity?: number;
  created_at?: string;
}): WalletTransactionOut {
  return {
    wallet_transaction_id: params.wallet_transaction_id,
    transaction_type: params.transaction_type,
    amount_inr: params.amount_inr,
    balance_after_inr: params.balance_after_inr,
    razorpay_order_id: null,
    razorpay_payment_id: null,
    reference_id: null,
    quantity: params.quantity ?? 1,
    created_at: params.created_at ?? "2026-07-10T12:00:00Z",
  };
}

// ---- fetch mock plumbing ------------------------------------------------

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

function createApiMock(opts: {
  wallet?: WalletOut;
  transactions?: WalletTransactionOut[];
  rechargeStatus?: number;
  rechargeBody?: unknown;
}) {
  const rechargeCalls: { amount_inr: string }[] = [];

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : String(input);
    const method = (init?.method ?? "GET").toUpperCase();

    if (method === "GET" && url === "/api/wallet") {
      return jsonResponse(200, opts.wallet ?? emptyWallet);
    }
    if (method === "GET" && /^\/api\/wallet\/transactions(\?.*)?$/.test(url)) {
      return jsonResponse(200, opts.transactions ?? opts.wallet?.transactions ?? []);
    }
    if (method === "POST" && url === "/api/wallet/recharge") {
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      rechargeCalls.push(body);
      if (opts.rechargeStatus && opts.rechargeStatus >= 400) {
        return jsonResponse(opts.rechargeStatus, opts.rechargeBody ?? { detail: "Recharge failed" });
      }
      return jsonResponse(
        200,
        opts.rechargeBody ?? {
          razorpay_order_id: "order_test_1",
          razorpay_key_id: "rzp_test_key",
          amount_inr: body.amount_inr,
          currency: "INR",
        }
      );
    }
    throw new Error(`Unhandled fetch call in test: ${method} ${url}`);
  });

  return { fetchMock, rechargeCalls };
}

// ---- window.Razorpay mock plumbing --------------------------------------

function stubRazorpay() {
  const openMock = vi.fn();
  // Arrow functions have no [[Construct]] slot, so `new window.Razorpay(...)`
  // would throw if the mock implementation were one — a plain `function`
  // expression is required here.
  const constructorMock = vi.fn().mockImplementation(function RazorpayMock() {
    return { open: openMock };
  });
  window.Razorpay = constructorMock;
  return { constructorMock, openMock };
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
  // Cleared between tests so a leftover stub from a prior test never leaks
  // into one that doesn't call stubRazorpay().
  delete window.Razorpay;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ======================================================================
// Balance + transaction history rendering
// ======================================================================

describe("Wallet page balance and history", () => {
  it("renders the current balance and transaction rows from GET /wallet and GET /wallet/transactions", async () => {
    const wallet: WalletOut = {
      balance_inr: "450",
      currency: "INR",
      free_actions_remaining: { parse: 20, enrichment: 20, scoring: 20 },
      transactions: [
        makeTransaction({
          wallet_transaction_id: "txn-1",
          transaction_type: "recharge_credit",
          amount_inr: "500",
          balance_after_inr: "500",
        }),
        makeTransaction({
          wallet_transaction_id: "txn-2",
          transaction_type: "parse_debit",
          amount_inr: "-50",
          balance_after_inr: "450",
        }),
      ],
    };
    const { fetchMock } = createApiMock({ wallet, transactions: wallet.transactions });
    vi.stubGlobal("fetch", fetchMock);

    render(<WalletPage />);

    await screen.findByText("450");
    expect(screen.getByText("Recharge")).toBeInTheDocument();
    expect(screen.getByText("Card Parse")).toBeInTheDocument();
    expect(screen.getByText("+₹500")).toBeInTheDocument();
    expect(screen.getByText("₹50")).toBeInTheDocument();
  });

  it("shows 'on N cards' for a collective bulk-batch transaction, and nothing extra for a single-card one", async () => {
    const wallet: WalletOut = {
      balance_inr: "450",
      currency: "INR",
      free_actions_remaining: { parse: 20, enrichment: 20, scoring: 20 },
      transactions: [
        makeTransaction({
          wallet_transaction_id: "txn-bulk",
          transaction_type: "parse_debit",
          amount_inr: "-50",
          balance_after_inr: "450",
          quantity: 10,
        }),
        makeTransaction({
          wallet_transaction_id: "txn-single",
          transaction_type: "scoring_debit",
          amount_inr: "-2",
          balance_after_inr: "500",
          quantity: 1,
        }),
      ],
    };
    const { fetchMock } = createApiMock({ wallet, transactions: wallet.transactions });
    vi.stubGlobal("fetch", fetchMock);

    render(<WalletPage />);

    expect(await screen.findByText("on 10 cards")).toBeInTheDocument();
    expect(screen.queryByText(/on 1 cards?/)).not.toBeInTheDocument();
  });

  it("shows an empty state when there are no transactions yet", async () => {
    const { fetchMock } = createApiMock({ wallet: emptyWallet, transactions: [] });
    vi.stubGlobal("fetch", fetchMock);

    render(<WalletPage />);

    await screen.findByText("No transactions yet.");
    expect(screen.getByText("0")).toBeInTheDocument();
  });
});

// ======================================================================
// Recharge flow — "Add Money" -> POST /wallet/recharge -> Razorpay Checkout
// ======================================================================

describe("Wallet page recharge flow", () => {
  it("calls POST /wallet/recharge then opens Razorpay Checkout with the returned order details", async () => {
    const user = userEvent.setup();
    const { fetchMock, rechargeCalls } = createApiMock({
      wallet: emptyWallet,
      rechargeBody: {
        razorpay_order_id: "order_xyz",
        razorpay_key_id: "rzp_test_key_id",
        amount_inr: "500",
        currency: "INR",
      },
    });
    vi.stubGlobal("fetch", fetchMock);
    const { constructorMock, openMock } = stubRazorpay();

    render(<WalletPage />);
    await screen.findByText("0");

    await user.click(screen.getByRole("button", { name: "Add Money" }));

    await waitFor(() => expect(rechargeCalls).toEqual([{ amount_inr: "500" }]));
    await waitFor(() => expect(constructorMock).toHaveBeenCalledTimes(1));

    const optionsPassed = constructorMock.mock.calls[0][0];
    expect(optionsPassed.order_id).toBe("order_xyz");
    expect(optionsPassed.key).toBe("rzp_test_key_id");
    expect(optionsPassed.currency).toBe("INR");
    expect(optionsPassed.amount).toBe(50000); // paise

    expect(openMock).toHaveBeenCalledTimes(1);
  });

  it("shows an error and never opens Checkout when the recharge request fails", async () => {
    const user = userEvent.setup();
    const { fetchMock } = createApiMock({
      wallet: emptyWallet,
      rechargeStatus: 400,
      rechargeBody: { detail: "amount_inr must be between 100 and 500000" },
    });
    vi.stubGlobal("fetch", fetchMock);
    const { constructorMock } = stubRazorpay();

    render(<WalletPage />);
    await screen.findByText("0");

    await user.click(screen.getByRole("button", { name: "Add Money" }));

    expect(
      await screen.findByText("amount_inr must be between 100 and 500000")
    ).toBeInTheDocument();
    expect(constructorMock).not.toHaveBeenCalled();
  });
});
