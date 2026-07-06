"use client";

import { useEffect, useState } from "react";
import { Search, Filter, Upload } from "lucide-react";
import { useRouter } from "next/navigation";
import { Sidebar } from "@/components/sidebar";
import { OBtn } from "@/components/buttons";
import { getCurrentUser } from "@/lib/auth";
import type { UserOut } from "@/lib/api";

const LEADS = [
  { id: 1, name: "Rajesh Kumar", company: "Bharat Heavy Electricals", designation: "Head of Procurement", score: 91, exhibition: "IMTEX 2024" },
  { id: 2, name: "Anand Sharma", company: "Thermax Limited", designation: "Director Engineering", score: 88, exhibition: "IMTEX 2024" },
  { id: 3, name: "Priya Nair", company: "Larsen & Toubro", designation: "VP Operations", score: 84, exhibition: "Hannover Messe India" },
  { id: 4, name: "Sunita Rao", company: "Siemens India", designation: "Procurement Lead", score: 83, exhibition: "IMTEX 2024" },
  { id: 5, name: "Divya Menon", company: "ABB India", designation: "Purchase Manager", score: 79, exhibition: "Hannover Messe India" },
  { id: 6, name: "Suresh Patel", company: "Mahindra Manufacturing", designation: "Plant Manager", score: 72, exhibition: "ENGIMACH 2024" },
  { id: 7, name: "Vikram Joshi", company: "Tata Steel", designation: "Senior Buyer", score: 67, exhibition: "Metal Steel India 2024" },
  { id: 8, name: "Mohan Das", company: "CPCL Refinery", designation: "Sr. Process Engineer", score: 55, exhibition: "PETRO INDIA 2024" },
  { id: 9, name: "Meena Krishnan", company: "Kirloskar Brothers", designation: "GM Procurement", score: 45, exhibition: "PLASTIVISION 2024" },
  { id: 10, name: "Arun Iyer", company: "KOEL India", designation: "Director Operations", score: 38, exhibition: "ENGIMACH 2024" },
];

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

export default function Dashboard() {
  const router = useRouter();
  const [search, setSearch] = useState("");
  const [user, setUser] = useState<UserOut | null>(null);

  useEffect(() => {
    getCurrentUser().then(setUser);
  }, []);

  const filtered = LEADS.filter(
    (l) =>
      l.name.toLowerCase().includes(search.toLowerCase()) ||
      l.company.toLowerCase().includes(search.toLowerCase()) ||
      l.exhibition.toLowerCase().includes(search.toLowerCase())
  );

  const highCount = LEADS.filter((l) => l.score >= 80).length;
  const lowCount = LEADS.filter((l) => l.score < 60).length;

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
            <p className="text-xs text-black/35 mt-0.5">
              {LEADS.length} contacts · Last upload: IMTEX 2024
            </p>
          </div>
          <OBtn onClick={() => router.push("/upload")} className="text-sm gap-2">
            <Upload size={13} /> Bulk Upload
          </OBtn>
        </div>

        <div className="p-8 space-y-6">
          {/* Stats */}
          <div className="grid grid-cols-3 gap-4">
            {[
              { label: "Total Leads", value: LEADS.length, sub: "Across all exhibitions", accent: false },
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
                placeholder="Search name, company, or exhibition…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="w-full border border-black/12 pl-8 pr-4 py-2 text-sm focus:outline-none focus:border-[#E65527] bg-white transition-colors"
              />
            </div>
            <button className="border border-black/12 px-3 py-2 text-sm flex items-center gap-2 text-black/50 hover:border-black/25 transition-colors">
              <Filter size={12} /> Filter
            </button>
          </div>

          {/* Table */}
          <div className="border border-black/10 overflow-hidden">
            <div className="grid grid-cols-5 gap-4 bg-[#fafafa] border-b border-black/8 px-5 py-3 text-[11px] font-black uppercase tracking-wider text-black/35">
              <span>Name</span>
              <span>Company</span>
              <span>Designation</span>
              <span>Score</span>
              <span>Exhibition</span>
            </div>
            {filtered.map((lead) => (
              <div
                key={lead.id}
                className="grid grid-cols-5 gap-4 px-5 py-4 border-b border-black/5 text-sm hover:bg-[#E65527]/2 transition-colors cursor-pointer items-center"
              >
                <span className="font-semibold">{lead.name}</span>
                <span className="text-black/55">{lead.company}</span>
                <span className="text-black/50">{lead.designation}</span>
                <ScoreBadge score={lead.score} />
                <span className="text-black/40 text-xs">{lead.exhibition}</span>
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
    </div>
  );
}
