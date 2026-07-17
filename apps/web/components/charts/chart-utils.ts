// Recharts' YAxis `width` for a category axis is a fixed reservation, not a
// fit-to-content measurement — a hardcoded width wide enough for the
// longest-ever label leaves a dead strip of whitespace on the left whenever
// the actual data's labels are shorter (e.g. "Unclassified"/"Other" next to
// "Industrial Machinery & Equipment"). Estimate from the labels actually
// being rendered instead, clamped to a sane range.
export function estimateCategoryAxisWidth(
  labels: string[],
  { min = 70, max = 170, charWidth = 5.6, padding = 20 }: { min?: number; max?: number; charWidth?: number; padding?: number } = {}
): number {
  const longest = labels.reduce((longestLen, label) => Math.max(longestLen, label.length), 0);
  return Math.min(max, Math.max(min, Math.round(longest * charWidth + padding)));
}

export const UNCLASSIFIED_LABEL = "Unclassified";
export const OTHER_LABEL = "Other";

// A category-count list can have far more distinct values than a chart can
// usefully label (free-text industries/regions, or a growing list of
// exhibitions) — fold everything past the top N into a single "Other" row.
// "Unclassified" (if present) is always kept as its own row regardless of
// rank, never folded into "Other". Shared by industry-mix-chart.tsx,
// region-mix-chart.tsx, and exhibition-performance-chart.tsx, which
// previously each hand-rolled this same ~15-line algorithm. Callers should
// pass `palette.MAX_FOLDED_TOP_CATEGORIES` as `maxCategories`, not an
// independently-chosen number, so the fold never emits more rows than the
// categorical palette has distinct hues for.
export function foldTopNCategories<L extends string, C extends string>(
  data: (Record<L, string> & Record<C, number>)[],
  labelKey: L,
  countKey: C,
  maxCategories: number
): (Record<L, string> & Record<C, number>)[] {
  const unclassified = data.find((row) => row[labelKey] === UNCLASSIFIED_LABEL);
  const rest = data
    .filter((row) => row[labelKey] !== UNCLASSIFIED_LABEL)
    .slice()
    .sort((a, b) => b[countKey] - a[countKey]);
  const top = rest.slice(0, maxCategories);
  const overflow = rest.slice(maxCategories);
  const folded = [...top];
  if (overflow.length > 0) {
    const overflowCount = overflow.reduce((sum, row) => sum + row[countKey], 0);
    folded.push({ [labelKey]: OTHER_LABEL, [countKey]: overflowCount } as Record<L, string> & Record<C, number>);
  }
  if (unclassified) folded.push(unclassified);
  return folded;
}
