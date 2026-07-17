import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { RegionMixPoint } from "@/lib/api";
import { categoricalColor, MAX_FOLDED_TOP_CATEGORIES } from "./palette";
import { estimateCategoryAxisWidth, foldTopNCategories } from "./chart-utils";

// Same top-N + "Other" folding pattern as industry-mix-chart.tsx — up to
// 15 Indian states/metros are possible, more than a chart can label.
export function RegionMixChart({ data }: { data: RegionMixPoint[] }) {
  const folded = foldTopNCategories(data, "region", "count", MAX_FOLDED_TOP_CATEGORIES);
  const yAxisWidth = estimateCategoryAxisWidth(folded.map((row) => row.region));

  return (
    <div className="border border-black/8 bg-white p-5">
      <div className="text-sm font-bold mb-3">Region Mix</div>
      {folded.length === 0 ? (
        <div className="py-12 text-center text-sm text-black/30">No leads yet.</div>
      ) : (
        <ResponsiveContainer width="100%" height={260}>
          <BarChart data={folded} layout="vertical" margin={{ top: 4, right: 16, left: 0, bottom: 0 }}>
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
              dataKey="region"
              width={yAxisWidth}
              tick={{ fontSize: 11, fill: "rgba(0,0,0,0.5)" }}
              tickLine={false}
              axisLine={false}
            />
            <Tooltip
              contentStyle={{ fontSize: 12, border: "1px solid rgba(0,0,0,0.1)" }}
              labelStyle={{ fontWeight: 700 }}
            />
            <Bar dataKey="count" name="Leads" radius={[0, 4, 4, 0]} maxBarSize={20}>
              {folded.map((row, index) => (
                <Cell key={row.region} fill={categoricalColor(index)} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
