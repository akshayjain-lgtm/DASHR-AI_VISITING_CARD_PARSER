"use client";

import { useEffect, useState } from "react";
import { Wallet as WalletIcon, IndianRupee } from "lucide-react";
import { Sidebar } from "@/components/sidebar";
import { OBtn } from "@/components/buttons";
import { RazorpayCheckoutScript } from "@/components/razorpay-checkout-script";
import {
  ApiError,
  createWalletRecharge,
  getWallet,
  listWalletTransactions,
  type WalletOut,
  type WalletTransactionOut,
} from "@/lib/api";

declare global {
  interface Window {
    Razorpay?: new (options: Record<string, unknown>) => { open: () => void };
  }
}

const TRANSACTION_TYPE_LABEL: Record<string, string> = {
  recharge_credit: "Recharge",
  parse_debit: "Card Parse",
  enrichment_debit: "Enrichment",
  scoring_debit: "Scoring",
  adjustment: "Adjustment",
};

function TransactionTypeBadge({ type }: { type: string }) {
  const isCredit = type === "recharge_credit" || type === "adjustment";
  return (
    <span
      className={`inline-flex px-2.5 py-0.5 text-[11px] font-black tracking-wide ${
        isCredit ? "bg-[#E65527]/10 text-[#E65527]" : "bg-black/6 text-black/50"
      }`}
    >
      {TRANSACTION_TYPE_LABEL[type] ?? type.toUpperCase()}
    </span>
  );
}

export default function WalletPage() {
  const [wallet, setWallet] = useState<WalletOut | null>(null);
  const [transactions, setTransactions] = useState<WalletTransactionOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [amount, setAmount] = useState("500");
  const [recharging, setRecharging] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function refresh() {
    return Promise.all([getWallet(), listWalletTransactions({ limit: 50 })]).then(
      ([walletData, transactionData]) => {
        setWallet(walletData);
        setTransactions(transactionData);
      }
    );
  }

  useEffect(() => {
    let cancelled = false;
    refresh()
      .catch(() => {
        if (!cancelled) setError("Couldn't load your wallet. Try refreshing the page.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleRecharge() {
    setRecharging(true);
    setError(null);
    try {
      const order = await createWalletRecharge(amount);
      if (!window.Razorpay) {
        throw new Error("Payment provider is still loading — try again in a moment.");
      }
      const razorpay = new window.Razorpay({
        key: order.razorpay_key_id,
        order_id: order.razorpay_order_id,
        amount: Math.round(parseFloat(order.gross_amount_inr) * 100),
        currency: order.currency,
        name: "DASHR AI",
        description: "Wallet recharge",
        handler: () => {
          refresh();
        },
      });
      razorpay.open();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't start the recharge. Try again.");
    } finally {
      setRecharging(false);
    }
  }

  const balance = wallet ? parseFloat(wallet.balance_inr) : 0;

  // Client-side display estimate only — the authoritative GST breakdown is
  // whatever createWalletRecharge() actually returns (used above in
  // handleRecharge), never this value.
  const parsedAmount = parseFloat(amount);
  const gstPreview = Number.isFinite(parsedAmount) && parsedAmount > 0 ? parsedAmount * 1.18 : null;

  return (
    <div className="min-h-screen bg-white flex flex-col sm:flex-row">
      <RazorpayCheckoutScript />
      <Sidebar active="wallet" />
      <main className="flex-1 p-10 max-w-3xl">
        <div className="mb-8">
          <h1 className="text-2xl font-black mb-1">Wallet</h1>
          <p className="text-sm text-black/45">
            Every card parse, enrichment, and score is a prepaid action billed to your wallet.
          </p>
        </div>

        <div className="border border-black/10 px-6 py-6 mb-8 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <div className="w-11 h-11 bg-[#E65527]/10 flex items-center justify-center shrink-0">
              <WalletIcon size={18} className="text-[#E65527]" />
            </div>
            <div>
              <p className="text-[11px] font-black uppercase tracking-wider text-black/40 mb-1">
                Current Balance
              </p>
              <p className="text-3xl font-black flex items-center gap-0.5">
                <IndianRupee size={22} strokeWidth={2.5} />
                {loading ? "…" : balance.toLocaleString("en-IN")}
              </p>
              {wallet && (
                <p className="text-[11px] text-black/40 mt-1">
                  Free actions left — Parse {wallet.free_actions_remaining.parse}, Enrichment{" "}
                  {wallet.free_actions_remaining.enrichment}, Scoring{" "}
                  {wallet.free_actions_remaining.scoring}
                </p>
              )}
            </div>
          </div>

          <div className="flex items-end gap-3">
            <div>
              <label className="text-[11px] font-black uppercase tracking-wider text-black/50 block mb-1.5">
                Amount (INR)
              </label>
              <input
                type="number"
                min={100}
                max={500000}
                step={100}
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                disabled={recharging}
                className="w-36 border border-black/15 px-4 py-2.5 text-sm focus:outline-none focus:border-[#E65527] transition-colors bg-white disabled:opacity-60"
              />
              {gstPreview != null && (
                <p className="text-[11px] text-black/40 mt-1">
                  + 18% GST → ₹{gstPreview.toLocaleString("en-IN", { maximumFractionDigits: 2 })} total
                </p>
              )}
            </div>
            <OBtn onClick={handleRecharge} disabled={loading || recharging} className="gap-2">
              {recharging ? "Starting…" : "Add Money"}
            </OBtn>
          </div>
        </div>

        {error && <p className="text-sm text-red-600 mb-6">{error}</p>}

        <h2 className="text-[11px] font-black uppercase tracking-wider text-black/40 mb-3">
          Transaction History
        </h2>
        <div className="border border-black/10 overflow-hidden">
          <div className="grid grid-cols-[1fr_1fr_1fr_1fr] gap-4 bg-[#fafafa] border-b border-black/8 px-5 py-3 text-[11px] font-black uppercase tracking-wider text-black/35 items-center">
            <div>Type</div>
            <div>Amount</div>
            <div>Balance After</div>
            <div>Date</div>
          </div>
          {transactions.length === 0 ? (
            <div className="px-5 py-10 text-center text-sm text-black/30">
              {loading ? "Loading…" : "No transactions yet."}
            </div>
          ) : (
            transactions.map((txn) => (
              <div
                key={txn.wallet_transaction_id}
                className="grid grid-cols-[1fr_1fr_1fr_1fr] gap-4 px-5 py-4 border-b border-black/5 text-sm items-center"
              >
                <div>
                  <TransactionTypeBadge type={txn.transaction_type} />
                  {txn.quantity > 1 && (
                    <p className="text-[11px] text-black/40 mt-1">on {txn.quantity} cards</p>
                  )}
                </div>
                <div className={parseFloat(txn.amount_inr) < 0 ? "text-black/70" : "text-[#E65527] font-bold"}>
                  {parseFloat(txn.amount_inr) < 0 ? "" : "+"}
                  {"₹"}
                  {Math.abs(parseFloat(txn.amount_inr)).toLocaleString("en-IN")}
                </div>
                <div className="text-black/60">
                  {"₹"}
                  {parseFloat(txn.balance_after_inr).toLocaleString("en-IN")}
                </div>
                <div className="text-black/40 text-xs">
                  {new Date(txn.created_at).toLocaleString("en-IN")}
                </div>
              </div>
            ))
          )}
        </div>
      </main>
    </div>
  );
}
