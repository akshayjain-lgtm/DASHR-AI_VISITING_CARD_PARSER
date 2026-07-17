"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import type { ExhibitionOut } from "@/lib/api";

export type TimeRangePreset = "30d" | "90d" | "1y" | "all" | "custom";

export type DashboardFilters = {
  exhibitionIds: string[];
  range: TimeRangePreset;
  customStart?: string;
  customEnd?: string;
};

const RANGE_OPTIONS: { value: TimeRangePreset; label: string }[] = [
  { value: "30d", label: "Last 30 days" },
  { value: "90d", label: "Last 90 days" },
  { value: "1y", label: "Last 1 year" },
  { value: "all", label: "All time" },
  { value: "custom", label: "Custom range" },
];

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

  const label =
    selectedIds.length === 0
      ? "All exhibitions"
      : selectedIds.length === 1
      ? exhibitions.find((e) => e.exhibition_id === selectedIds[0])?.name ?? "1 exhibition"
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
              <span className="truncate">{exhibition.name ?? "Unnamed exhibition"}</span>
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
}: {
  exhibitions: ExhibitionOut[];
  filters: DashboardFilters;
  onFiltersChange: (filters: DashboardFilters) => void;
}) {
  return (
    <div className="flex flex-col sm:flex-row sm:flex-wrap items-stretch sm:items-center gap-3">
      <ExhibitionMultiSelect
        exhibitions={exhibitions}
        selectedIds={filters.exhibitionIds}
        onChange={(exhibitionIds) => onFiltersChange({ ...filters, exhibitionIds })}
      />

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
// getDashboardAnalytics — kept alongside the filter bar since it's the only
// place range presets/custom dates are defined, not duplicated in the page.
export function rangeToDates(filters: DashboardFilters): { startDate?: string; endDate?: string } {
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
