"use client";

import { useEffect, useState } from "react";
import { Search, Filter, Upload } from "lucide-react";
import { useRouter } from "next/navigation";
import { Sidebar } from "@/components/sidebar";
import { OBtn } from "@/components/buttons";
import { CardDetailDrawer } from "@/components/card-detail-drawer";
import { getCurrentUser } from "@/lib/auth";
import { ApiError, exportCards, listCards, scoreCards, type CardOut, type UserOut } from "@/lib/api";
import { useCardSelection } from "@/lib/use-card-selection";

function ScoreBadge({ score }: { score: number }) {
  if (score >= 80)
    return (
      <span className="inline-flex px-2.5 py-0.5 text-[11px] font-black bg-[#E65527] text-white tracking-wide">
        {score}% HIGH
      </span>
    );
  if (score >= 60)
    return (
      <span className="inline-flex px-2.5 py-0.5 text-[11px] font-black bg-black/8 text-black/60 tracking-wide">
        {score}% MED
      </span>
    );
  return (
    <span className="inline-flex px-2.5 py-0.5 text-[11px] font-black bg-black/4 text-black/35 tracking-wide">
      {score}% LOW
    </span>
  );
}

function UnscoredBadge() {
  return (
    <span className="inline-flex px-2.5 py-0.5 text-[11px] font-black bg-black/4 text-black/30 tracking-wide">
      UNSCORED
    </span>
  );
}

export default function Dashboard() {
  const router = useRouter();
  const [search, setSearch] = useState("");
  const [user, setUser] = useState<UserOut | null>(null);
  const [cards, setCards] = useState<CardOut[]>([]);
  const [selectedCardId, setSelectedCardId] = useState<string | null>(null);
  const [isScoring, setIsScoring] = useState(false);
  const [scoreError, setScoreError] = useState<string | null>(null);
  const [isExporting, setIsExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const { selectedCardIds, allSelected, toggleSelectAll, toggleCardSelected, clearSelection } =
    useCardSelection(cards);

  useEffect(() => {
    getCurrentUser().then(setUser);
  }, []);

  function refreshCards() {
    return listCards().then(setCards);
  }

  useEffect(() => {
    refreshCards();
  }, []);

  const filtered = cards.filter(
    (c) =>
      (c.full_name ?? "").toLowerCase().includes(search.toLowerCase()) ||
      (c.company_name ?? "").toLowerCase().includes(search.toLowerCase())
  );

  const highCount = cards.filter((c) => c.lead_score != null && c.lead_score >= 80).length;
  const lowCount = cards.filter((c) => c.lead_score != null && c.lead_score < 60).length;

  const scoreEligibleSelected = cards.filter(
    (c) => selectedCardIds.has(c.card_id) && c.status === "extracted"
  );

  async function handleScoreCards() {
    setIsScoring(true);
    setScoreError(null);
    try {
      await scoreCards(scoreEligibleSelected.map((c) => c.card_id));
      clearSelection();
      await refreshCards();
    } catch (err) {
      setScoreError(err instanceof ApiError ? err.message : "Failed to start scoring");
    } finally {
      setIsScoring(false);
    }
  }

  async function handleExportCards() {
    setIsExporting(true);
    setExportError(null);
    try {
      await exportCards([...selectedCardIds]);
    } catch (err) {
      setExportError(err instanceof ApiError ? err.message : "Failed to export cards");
    } finally {
      setIsExporting(false);
    }
  }

  return (
    <div className="min-h-screen bg-white flex">
      <Sidebar active="dashboard" />
      <main className="flex-1 flex flex-col min-h-screen overflow-auto">
        {/* Topbar */}
        <div className="border-b border-black/10 px-8 py-4 flex items-center justify-between bg-white sticky top-0 z-10">
          <div>
            {user && (
              <p className="text-xs font-bold text-[#E65527] mb-1">Hi {user.name ?? user.email}</p>
            )}
            <h1 className="font-black text-lg">Leads</h1>
            <p className="text-xs text-black/35 mt-0.5">{cards.length} contacts</p>
          </div>
          <OBtn onClick={() => router.push("/upload")} className="text-sm gap-2">
            <Upload size={13} /> Bulk Upload
          </OBtn>
        </div>

        <div className="p-8 space-y-6">
          {/* Stats */}
          <div className="grid grid-cols-3 gap-4">
            {[
              { label: "Total Leads", value: cards.length, sub: "Across all exhibitions", accent: false },
              { label: "High Fit", value: highCount, sub: "Score ≥ 80% — call first", accent: true },
              { label: "Low Fit", value: lowCount, sub: "Score < 60% — deprioritise", accent: false },
            ].map(({ label, value, sub, accent }) => (
              <div
                key={label}
                className={`border p-5 ${
                  accent ? "border-[#E65527]/25 bg-[#E65527]/4" : "border-black/8 bg-white"
                }`}
              >
                <div className={`text-3xl font-black mb-1 ${accent ? "text-[#E65527]" : "text-black"}`}>
                  {value}
                </div>
                <div className="text-sm font-bold">{label}</div>
                <div className="text-xs text-black/35 mt-0.5">{sub}</div>
              </div>
            ))}
          </div>

          {/* Filters */}
          <div className="flex items-center gap-3">
            <div className="relative max-w-xs flex-1">
              <Search size={13} className="absolute left-3 top-1/2 -translate-y-1/2 text-black/30" />
              <input
                type="text"
                placeholder="Search name or company…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="w-full border border-black/12 pl-8 pr-4 py-2 text-sm focus:outline-none focus:border-[#E65527] bg-white transition-colors"
              />
            </div>
            <button className="border border-black/12 px-3 py-2 text-sm flex items-center gap-2 text-black/50 hover:border-black/25 transition-colors">
              <Filter size={12} /> Filter
            </button>
            <div className="flex-1" />
            {cards.length > 0 && (
              <>
                <OBtn
                  onClick={handleScoreCards}
                  disabled={isScoring || scoreEligibleSelected.length === 0}
                  className="text-xs"
                >
                  {isScoring ? "Starting…" : `Score Selected (${scoreEligibleSelected.length})`}
                </OBtn>
                <OBtn
                  onClick={handleExportCards}
                  disabled={isExporting || selectedCardIds.size === 0}
                  className="text-xs"
                >
                  {isExporting ? "Exporting…" : `Export CSV (${selectedCardIds.size})`}
                </OBtn>
              </>
            )}
          </div>

          {scoreError && (
            <div className="border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
              {scoreError}
            </div>
          )}

          {exportError && (
            <div className="border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
              {exportError}
            </div>
          )}

          {/* Table */}
          <div className="border border-black/10 overflow-hidden">
            <div className="grid grid-cols-[auto_1fr_1fr_1fr_1fr] gap-4 bg-[#fafafa] border-b border-black/8 px-5 py-3 text-[11px] font-black uppercase tracking-wider text-black/35 items-center">
              <input
                type="checkbox"
                checked={allSelected}
                onChange={toggleSelectAll}
                aria-label="Select all cards"
              />
              <span>Name</span>
              <span>Company</span>
              <span>Designation</span>
              <span>Score</span>
            </div>
            {filtered.map((card) => (
              <div
                key={card.card_id}
                onClick={() => setSelectedCardId(card.card_id)}
                className="grid grid-cols-[auto_1fr_1fr_1fr_1fr] gap-4 px-5 py-4 border-b border-black/5 text-sm hover:bg-[#E65527]/2 transition-colors cursor-pointer items-center"
              >
                <input
                  type="checkbox"
                  checked={selectedCardIds.has(card.card_id)}
                  onClick={(e) => e.stopPropagation()}
                  onChange={() => toggleCardSelected(card.card_id)}
                  aria-label={`Select ${card.full_name ?? "card"}`}
                />
                <span className="font-semibold">{card.full_name ?? "Unnamed contact"}</span>
                <span className="text-black/55">{card.company_name ?? "—"}</span>
                <span className="text-black/50">{card.job_title ?? "—"}</span>
                {card.lead_score == null ? (
                  <UnscoredBadge />
                ) : (
                  <ScoreBadge score={card.lead_score} />
                )}
              </div>
            ))}
            {filtered.length === 0 && (
              <div className="px-5 py-12 text-center text-sm text-black/30">
                No leads match your search.
              </div>
            )}
          </div>
        </div>
      </main>

      {selectedCardId && (
        <CardDetailDrawer
          cardId={selectedCardId}
          onClose={() => setSelectedCardId(null)}
          onChanged={refreshCards}
          onNavigateToCard={setSelectedCardId}
        />
      )}
    </div>
  );
}
