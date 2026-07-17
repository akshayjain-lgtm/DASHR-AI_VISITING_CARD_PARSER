// Validated categorical palette (dataviz skill's documented default
// instance) — fixed hue order, never cycled/reshuffled per render. Passes
// the CVD/contrast gate as an ordered set; slots 1-4 additionally pass
// under --pairs all (scatter/small-multiples), so anything past 4 series
// should fold into "Other" rather than keep assigning further slots.
export const CATEGORICAL_PALETTE = [
  "#2a78d6", // 1 blue
  "#008300", // 2 green
  "#e87ba4", // 3 magenta
  "#eda100", // 4 yellow
  "#1baf7a", // 5 aqua
  "#eb6834", // 6 orange
  "#4a3aa7", // 7 violet
  "#e34948", // 8 red
] as const;

export function categoricalColor(index: number): string {
  return CATEGORICAL_PALETTE[index % CATEGORICAL_PALETTE.length];
}

// A chart that folds long-tail categories into "Other" (plus, for mix
// charts, a separate always-kept "Unclassified" row) must not fold in more
// top categories than leaves room for those extra rows within the palette
// — otherwise categoricalColor's modulo wraps and two bars silently share a
// hue. Derived from the palette's own length so the two can never drift out
// of sync again (e.g. an "Other" row plus a fixed "Unclassified" row = 2
// reserved slots).
export const MAX_FOLDED_TOP_CATEGORIES = CATEGORICAL_PALETTE.length - 2;
