import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { ExhibitionPerformance } from "@/lib/api";
import { categoricalColor, MAX_FOLDED_TOP_CATEGORIES } from "./palette";
import { foldTopNCategories } from "./chart-utils";

const MAX_TICK_CHARS = 14;

function truncateForTick(name: string): string {
  return name.length > MAX_TICK_CHARS ? `${name.slice(0, MAX_TICK_CHARS - 1)}…` : name;
}

// Custom tick, not just an angled built-in one — a fixed rotation still
// clips long exhibition names against the chart's bottom edge on narrow
// (phone-width) containers. Truncating what's drawn (the underlying data
// point/tooltip still carries the full name) keeps every label fully
// visible regardless of container width.
function AngledExhibitionTick({ x, y, payload }: { x: number; y: number; payload: { value: string } }) {
  return (
    <text
      x={x}
      y={y}
      dy={10}
      textAnchor="end"
      transform={`rotate(-35, ${x}, ${y})`}
      fontSize={10}
      fill="rgba(0,0,0,0.5)"
    >
      {truncateForTick(payload.value)}
    </text>
  );
}

// avg_score is intentionally not shown for the time being (see
// .claude/specs/16-dashboard-analytics.md) — lead count per exhibition
// only, each bar its own hue from the shared categorical palette so
// individual exhibitions are visually distinguishable at a glance. Folded
// to the top N (+ "Other") past MAX_FOLDED_TOP_CATEGORIES exhibitions, same
// as industry/region mix — otherwise a seller running many exhibitions
// would see bar colors repeat (categoricalColor cycles the 8-hue palette).
export function ExhibitionPerformanceChart({ data }: { data: ExhibitionPerformance[] }) {
  const mapped = data.map((row) => ({
    name: row.exhibition_name ?? "Unnamed exhibition",
    lead_count: row.lead_count,
  }));
  const rows = foldTopNCategories(mapped, "name", "lead_count", MAX_FOLDED_TOP_CATEGORIES);

  return (
    <div className="border border-black/8 bg-white p-5">
      <div className="text-sm font-bold mb-3">Exhibition Performance</div>
      {rows.length === 0 ? (
        <div className="py-12 text-center text-sm text-black/30">No exhibitions yet.</div>
      ) : (
        <ResponsiveContainer width="100%" height={260}>
          <BarChart data={rows} margin={{ top: 4, right: 8, left: 8, bottom: 24 }}>
            <CartesianGrid vertical={false} stroke="rgba(0,0,0,0.06)" />
            <XAxis
              dataKey="name"
              tick={<AngledExhibitionTick x={0} y={0} payload={{ value: "" }} />}
              tickLine={false}
              axisLine={false}
              interval={0}
              height={64}
            />
            <YAxis
              allowDecimals={false}
              tick={{ fontSize: 11, fill: "rgba(0,0,0,0.35)" }}
              tickLine={false}
              axisLine={false}
              width={30}
            />
            <Tooltip contentStyle={{ fontSize: 12, border: "1px solid rgba(0,0,0,0.1)" }} />
            <Bar dataKey="lead_count" name="Leads" radius={[4, 4, 0, 0]} maxBarSize={48}>
              {rows.map((row, index) => (
                <Cell key={row.name} fill={categoricalColor(index)} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
