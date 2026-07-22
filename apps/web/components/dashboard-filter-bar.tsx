"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import type { ExhibitionOut, OrgMemberOut } from "@/lib/api";

export type TimeRangePreset = "30d" | "90d" | "1y" | "all" | "custom";

export type DashboardFilters = {
  exhibitionIds: string[];
  range: TimeRangePreset;
  customStart?: string;
  customEnd?: string;
  // Admin-only "uploaded by" filter — absent/"all" means every visible
  // uploader. Mirrors the upload page's existing userFilter convention.
  userId?: string;
};

// Exported so /upload can render the identical preset list rather than
// re-declaring it.
export const RANGE_OPTIONS: { value: TimeRangePreset; label: string }[] = [
  { value: "30d", label: "Last 30 days" },
  { value: "90d", label: "Last 90 days" },
  { value: "1y", label: "Last 1 year" },
  { value: "all", label: "All time" },
  { value: "custom", label: "Custom range" },
];

// Admin-only "uploaded by" select — shared by /dashboard and /upload so both
// pages render an identical control instead of each reimplementing it.
export function UploadedByFilter({
  orgMembers,
  currentUserId,
  value,
  onChange,
}: {
  orgMembers: OrgMemberOut[];
  currentUserId?: string;
  value: string;
  onChange: (userId: string) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <label className="text-xs font-black uppercase tracking-wider text-black/35">Uploaded by</label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="border border-black/12 px-3 py-2 text-sm bg-white focus:outline-none focus:border-[#E65527] transition-colors"
      >
        <option value="all">All users</option>
        {orgMembers.map((m) => (
          <option key={m.user_id} value={m.user_id}>
            {m.name?.trim() || m.email}
            {currentUserId && m.user_id === currentUserId ? " (You)" : ""}
          </option>
        ))}
      </select>
    </div>
  );
}

const SHORT_MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

// Exhibitions recur (the same trade show happens again next year), so the
// name alone doesn't disambiguate one occurrence from another — every
// exhibition-facing label appends "- Mon/yy" from start_date wherever one
// exists. Parsed as a plain string (not `new Date(...)`) since start_date is
// a date-only "YYYY-MM-DD" wire value with no time component — routing it
// through `Date` risks a local-timezone off-by-one-day shift.
export function formatExhibitionLabel(
  exhibition: Pick<ExhibitionOut, "name" | "start_date">
): string {
  const name = exhibition.name?.trim() || "Unnamed exhibition";
  if (!exhibition.start_date) return name;
  const [year, month] = exhibition.start_date.split("-");
  return `${name} - ${SHORT_MONTHS[Number(month) - 1]}/${year.slice(2)}`;
}

// Checkbox dropdown, not a native <select multiple> — a fixed-height
// listbox is a poor fit on small touch screens (dataviz skill's
// filter-composition rule cares about usability, not just correctness).
function ExhibitionMultiSelect({
  exhibitions,
  selectedIds,
  onChange,
}: {
  exhibitions: ExhibitionOut[];
  selectedIds: string[];
  onChange: (ids: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onClickOutside(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, []);

  function toggle(id: string) {
    onChange(selectedIds.includes(id) ? selectedIds.filter((x) => x !== id) : [...selectedIds, id]);
  }

  const singleSelected = exhibitions.find((e) => e.exhibition_id === selectedIds[0]);
  const label =
    selectedIds.length === 0
      ? "All exhibitions"
      : selectedIds.length === 1
      ? singleSelected
        ? formatExhibitionLabel(singleSelected)
        : "1 exhibition"
      : `${selectedIds.length} exhibitions`;

  return (
    <div ref={rootRef} className="relative w-full sm:w-auto">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="w-full sm:w-auto flex items-center justify-between gap-2 border border-black/12 px-3 py-2 text-sm bg-white focus:outline-none focus:border-[#E65527] transition-colors"
      >
        <span className="truncate max-w-[220px]">{label}</span>
        <ChevronDown size={14} className="shrink-0 text-black/40" />
      </button>
      {open && (
        <div
          role="listbox"
          aria-multiselectable="true"
          className="absolute z-20 mt-1 w-full sm:w-72 max-h-64 overflow-auto border border-black/12 bg-white shadow-lg"
        >
          <label className="flex items-center gap-2 px-3 py-2 text-sm border-b border-black/8 cursor-pointer hover:bg-black/[0.02]">
            <input type="checkbox" checked={selectedIds.length === 0} onChange={() => onChange([])} />
            All exhibitions
          </label>
          {exhibitions.map((exhibition) => (
            <label
              key={exhibition.exhibition_id}
              className="flex items-center gap-2 px-3 py-2 text-sm cursor-pointer hover:bg-black/[0.02]"
            >
              <input
                type="checkbox"
                checked={selectedIds.includes(exhibition.exhibition_id)}
                onChange={() => toggle(exhibition.exhibition_id)}
              />
              <span className="truncate">{formatExhibitionLabel(exhibition)}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

// One row, above the charts — every chart re-scopes to the same slice, so
// the numbers always agree (dataviz skill's filter-composition rule).
// Stacks vertically on narrow screens instead of overflowing.
export function DashboardFilterBar({
  exhibitions,
  filters,
  onFiltersChange,
  showUserFilter = false,
  orgMembers = [],
  currentUserId,
}: {
  exhibitions: ExhibitionOut[];
  filters: DashboardFilters;
  onFiltersChange: (filters: DashboardFilters) => void;
  // Admin-only "uploaded by" control — only rendered when true (gated by
  // the caller on isAdmin && orgMembers.length > 1, same as /upload).
  showUserFilter?: boolean;
  orgMembers?: OrgMemberOut[];
  currentUserId?: string;
}) {
  return (
    <div className="flex flex-col sm:flex-row sm:flex-wrap items-stretch sm:items-center gap-3">
      <ExhibitionMultiSelect
        exhibitions={exhibitions}
        selectedIds={filters.exhibitionIds}
        onChange={(exhibitionIds) => onFiltersChange({ ...filters, exhibitionIds })}
      />

      {showUserFilter && (
        <UploadedByFilter
          orgMembers={orgMembers}
          currentUserId={currentUserId}
          value={filters.userId ?? "all"}
          onChange={(userId) => onFiltersChange({ ...filters, userId })}
        />
      )}

      <select
        value={filters.range}
        onChange={(e) => onFiltersChange({ ...filters, range: e.target.value as TimeRangePreset })}
        className="border border-black/12 px-3 py-2 text-sm bg-white focus:outline-none focus:border-[#E65527] transition-colors"
      >
        {RANGE_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>

      {filters.range === "custom" && (
        <div className="flex items-center gap-2 flex-wrap">
          <input
            type="date"
            value={filters.customStart ?? ""}
            onChange={(e) => onFiltersChange({ ...filters, customStart: e.target.value })}
            aria-label="Custom range start date"
            className="border border-black/12 px-3 py-2 text-sm bg-white focus:outline-none focus:border-[#E65527] transition-colors"
          />
          <span className="text-black/30 text-sm">to</span>
          <input
            type="date"
            value={filters.customEnd ?? ""}
            onChange={(e) => onFiltersChange({ ...filters, customEnd: e.target.value })}
            aria-label="Custom range end date"
            className="border border-black/12 px-3 py-2 text-sm bg-white focus:outline-none focus:border-[#E65527] transition-colors"
          />
        </div>
      )}
    </div>
  );
}

// Translates the current filters into {startDate, endDate} (YYYY-MM-DD) for
// getDashboardAnalytics/listCards — kept alongside the filter bar since it's
// the only place range presets/custom dates are defined, not duplicated in
// the page. Only reads the range/custom-date fields, so callers that don't
// have a full DashboardFilters (e.g. /upload, which has no exhibition
// filter) can pass just those.
export function rangeToDates(
  filters: Pick<DashboardFilters, "range" | "customStart" | "customEnd">
): { startDate?: string; endDate?: string } {
  if (filters.range === "custom") {
    return { startDate: filters.customStart || undefined, endDate: filters.customEnd || undefined };
  }
  if (filters.range === "all") return {};
  const days = { "30d": 30, "90d": 90, "1y": 365 }[filters.range];
  const end = new Date();
  const start = new Date();
  start.setDate(start.getDate() - days);
  return { startDate: start.toISOString().slice(0, 10), endDate: end.toISOString().slice(0, 10) };
}
