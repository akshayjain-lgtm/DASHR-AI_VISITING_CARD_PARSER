import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { IndustryMixPoint } from "@/lib/api";
import { categoricalColor, MAX_FOLDED_TOP_CATEGORIES } from "./palette";
import { estimateCategoryAxisWidth, foldTopNCategories } from "./chart-utils";

// Company.industry is free text, so there can be far more categories than a
// chart can label — fold everything past the top N into "Other". This is
// presentational only; the backend does no folding.
export function IndustryMixChart({ data }: { data: IndustryMixPoint[] }) {
  const folded = foldTopNCategories(data, "industry", "count", MAX_FOLDED_TOP_CATEGORIES);
  const yAxisWidth = estimateCategoryAxisWidth(folded.map((row) => row.industry));

  return (
    <div className="border border-black/8 bg-white p-5">
      <div className="text-sm font-bold mb-3">Industry Mix</div>
      {folded.length === 0 ? (
        <div className="py-12 text-center text-sm text-black/30">No enriched leads yet.</div>
      ) : (
        <ResponsiveContainer width="100%" height={260}>
          <BarChart
            data={folded}
            layout="vertical"
            margin={{ top: 4, right: 16, left: 0, bottom: 0 }}
          >
            <CartesianGrid horizontal={false} stroke="rgba(0,0,0,0.06)" />
            <XAxis
              type="number"
              allowDecimals={false}
              tick={{ fontSize: 11, fill: "rgba(0,0,0,0.35)" }}
              tickLine={false}
              axisLine={false}
            />
            <YAxis
              type="category"
              dataKey="industry"
              width={yAxisWidth}
              tick={{ fontSize: 10, fill: "rgba(0,0,0,0.5)" }}
              tickLine={false}
              axisLine={false}
            />
            <Tooltip
              contentStyle={{ fontSize: 12, border: "1px solid rgba(0,0,0,0.1)" }}
              labelStyle={{ fontWeight: 700 }}
            />
            <Bar dataKey="count" name="Leads" radius={[0, 4, 4, 0]} maxBarSize={20}>
              {folded.map((row, index) => (
                <Cell key={row.industry} fill={categoricalColor(index)} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
