"use client";

import { useEffect, useState } from "react";
import { Upload } from "lucide-react";
import { useRouter } from "next/navigation";
import { Sidebar } from "@/components/sidebar";
import { OBtn } from "@/components/buttons";
import { DashboardFilterBar, rangeToDates, type DashboardFilters } from "@/components/dashboard-filter-bar";
import { LeadVolumeChart } from "@/components/charts/lead-volume-chart";
import { IndustryMixChart } from "@/components/charts/industry-mix-chart";
import { ScoreDistributionChart } from "@/components/charts/score-distribution-chart";
import { ExhibitionPerformanceChart } from "@/components/charts/exhibition-performance-chart";
import { RoleMixChart } from "@/components/charts/role-mix-chart";
import { RegionMixChart } from "@/components/charts/region-mix-chart";
import { getCurrentUser } from "@/lib/auth";
import {
  listExhibitions,
  listOrgMembers,
  getDashboardAnalytics,
  type ExhibitionOut,
  type OrgMemberOut,
  type UserOut,
  type DashboardAnalyticsOut,
} from "@/lib/api";

export default function Dashboard() {
  const router = useRouter();
  const [user, setUser] = useState<UserOut | null>(null);
  const [exhibitions, setExhibitions] = useState<ExhibitionOut[]>([]);
  const [filters, setFilters] = useState<DashboardFilters>({ exhibitionIds: [], range: "30d" });
  const [analytics, setAnalytics] = useState<DashboardAnalyticsOut | null>(null);

  // "Uploaded by" filter — admin-only, mirrors the same pattern already on
  // /upload. orgMembers only loads once we know the current user is an
  // admin (the members endpoint 403s otherwise).
  const [orgMembers, setOrgMembers] = useState<OrgMemberOut[]>([]);
  const isAdmin = user?.role === "admin";
  const showUserFilter = isAdmin && orgMembers.length > 1;

  useEffect(() => {
    getCurrentUser().then(setUser);
    listExhibitions().then(setExhibitions);
  }, []);

  useEffect(() => {
    if (!isAdmin) return;
    listOrgMembers()
      .then(setOrgMembers)
      .catch(() => {});
  }, [isAdmin]);

  useEffect(() => {
    const { startDate, endDate } = rangeToDates(filters);
    // filters.userId can only ever be set via UploadedByFilter, which only
    // renders once showUserFilter is true — no separate gate needed here,
    // and keeping showUserFilter out of this effect's deps avoids an
    // unnecessary duplicate fetch the moment orgMembers finishes loading.
    const userId = filters.userId && filters.userId !== "all" ? filters.userId : undefined;
    getDashboardAnalytics({ exhibitionIds: filters.exhibitionIds, startDate, endDate, userId }).then(
      setAnalytics
    );
  }, [filters]);

  const totalLeads = analytics
    ? analytics.score_distribution.high +
      analytics.score_distribution.medium +
      analytics.score_distribution.low +
      analytics.score_distribution.unscored
    : 0;

  return (
    <div className="min-h-screen bg-white flex flex-col sm:flex-row">
      <Sidebar active="dashboard" />
      <main className="flex-1 flex flex-col min-h-screen overflow-auto">
        {/* Topbar */}
        <div className="border-b border-black/10 px-4 sm:px-8 py-4 flex items-center justify-between gap-3 bg-white sticky top-0 z-10">
          <div className="min-w-0">
            {user && (
              <p className="text-xs font-bold text-[#E65527] mb-1 truncate">Hi {user.name ?? user.email}</p>
            )}
            <h1 className="font-black text-lg">Dashboard</h1>
            <p className="text-xs text-black/35 mt-0.5">{totalLeads} leads analyzed</p>
          </div>
          <OBtn onClick={() => router.push("/upload")} className="text-sm gap-2 shrink-0">
            <Upload size={13} /> Bulk Upload
          </OBtn>
        </div>

        <div className="p-4 sm:p-8 space-y-6">
          {/* Stats — High Fit / Low Fit tiles removed for the time being,
              until scoring itself is revisited; Total Leads only. */}
          <div className="border border-black/8 bg-white p-5 w-full sm:max-w-xs">
            <div className="text-3xl font-black mb-1 text-black">{totalLeads}</div>
            <div className="text-sm font-bold">Total Leads</div>
            <div className="text-xs text-black/35 mt-0.5">Across all exhibitions</div>
          </div>

          {/* Filters — one row (stacks on narrow screens), above the
              charts; every chart re-scopes to the same slice so all
              numbers always agree. */}
          <DashboardFilterBar
            exhibitions={exhibitions}
            filters={filters}
            onFiltersChange={setFilters}
            showUserFilter={showUserFilter}
            orgMembers={orgMembers}
            currentUserId={user?.user_id}
          />

          {/* Analytics */}
          {analytics ? (
            <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4">
              <LeadVolumeChart data={analytics.lead_volume} />
              <IndustryMixChart data={analytics.industry_mix} />
              <ScoreDistributionChart data={analytics.score_distribution} />
              <ExhibitionPerformanceChart data={analytics.exhibition_performance} />
              <RoleMixChart data={analytics.role_mix} />
              <RegionMixChart data={analytics.region_mix} />
            </div>
          ) : (
            <div className="text-sm text-black/30 py-8 text-center">Loading analytics…</div>
          )}
        </div>
      </main>
    </div>
  );
}
