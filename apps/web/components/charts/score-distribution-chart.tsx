import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { ScoreDistribution } from "@/lib/api";

const BRAND_ORANGE = "#E65527";
// High stays brand orange (matches the app's existing "hot lead, call
// first" identity). Medium/Low get a splash of color from the dataviz
// skill's status palette instead of flat grays; Unscored stays a dashed,
// near-transparent outline so "no data" reads as categorically different
// from "scored low," not just lighter.
const BUCKET_STYLE: Record<string, { fill: string; stroke?: string; dash?: string }> = {
  High: { fill: BRAND_ORANGE },
  Medium: { fill: "#fab219" }, // status "warning" amber
  Low: { fill: "#2a78d6" }, // categorical blue — cool, non-alarming
  Unscored: { fill: "rgba(0,0,0,0.03)", stroke: "rgba(0,0,0,0.25)", dash: "4 3" },
};

export function ScoreDistributionChart({ data }: { data: ScoreDistribution }) {
  const rows = [
    { bucket: "High", count: data.high },
    { bucket: "Medium", count: data.medium },
    { bucket: "Low", count: data.low },
    { bucket: "Unscored", count: data.unscored },
  ];
  const total = data.high + data.medium + data.low + data.unscored;

  return (
    <div className="border border-black/8 bg-white p-5">
      <div className="text-sm font-bold mb-3">Score Distribution</div>
      {total === 0 ? (
        <div className="py-12 text-center text-sm text-black/30">No leads yet.</div>
      ) : (
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={rows} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
            <CartesianGrid vertical={false} stroke="rgba(0,0,0,0.06)" />
            <XAxis
              dataKey="bucket"
              tick={{ fontSize: 11, fill: "rgba(0,0,0,0.35)" }}
              tickLine={false}
              axisLine={false}
            />
            <YAxis
              allowDecimals={false}
              tick={{ fontSize: 11, fill: "rgba(0,0,0,0.35)" }}
              tickLine={false}
              axisLine={false}
              width={30}
            />
            <Tooltip
              contentStyle={{ fontSize: 12, border: "1px solid rgba(0,0,0,0.1)" }}
              labelStyle={{ fontWeight: 700 }}
            />
            <Bar dataKey="count" name="Leads" radius={[4, 4, 0, 0]} maxBarSize={56}>
              {rows.map((row) => {
                const style = BUCKET_STYLE[row.bucket];
                return (
                  <Cell
                    key={row.bucket}
                    fill={style.fill}
                    stroke={style.stroke}
                    strokeDasharray={style.dash}
                  />
                );
              })}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
