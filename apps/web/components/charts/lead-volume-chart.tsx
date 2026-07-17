import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { LeadVolumePoint } from "@/lib/api";

const BRAND_ORANGE = "#E65527";

export function LeadVolumeChart({ data }: { data: LeadVolumePoint[] }) {
  return (
    <div className="border border-black/8 bg-white p-5">
      <div className="text-sm font-bold mb-3">Lead Volume</div>
      {data.length === 0 ? (
        <div className="py-12 text-center text-sm text-black/30">No leads yet.</div>
      ) : (
        <ResponsiveContainer width="100%" height={220}>
          <AreaChart data={data} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
            <defs>
              <linearGradient id="leadVolumeGradient" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor={BRAND_ORANGE} stopOpacity={0.35} />
                <stop offset="95%" stopColor={BRAND_ORANGE} stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid vertical={false} stroke="rgba(0,0,0,0.06)" />
            <XAxis
              dataKey="date"
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
            <Area
              type="monotone"
              dataKey="count"
              name="Leads"
              stroke={BRAND_ORANGE}
              strokeWidth={2}
              fill="url(#leadVolumeGradient)"
            />
          </AreaChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
