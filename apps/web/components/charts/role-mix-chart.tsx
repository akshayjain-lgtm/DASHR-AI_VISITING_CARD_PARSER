import { Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import type { RoleMixPoint } from "@/lib/api";
import { categoricalColor } from "./palette";

// Raw VisitingCard.designation_level values have no display-label mapping
// anywhere else in the app (card-detail-drawer.tsx only does a raw
// underscore-to-space substitution) — this is the first proper label map.
const ROLE_LABELS: Record<string, string> = {
  c_level: "C-Level",
  director: "Director",
  manager: "Manager",
  individual_contributor: "Individual Contributor",
  Unclassified: "Unclassified",
};

function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role;
}

// Donut, not bar: role mix is a small (≤6), fixed, part-to-whole
// breakdown — the one case the dataviz skill sanctions a donut for
// ("part-to-whole at a glance only, ≤ 6 segments").
export function RoleMixChart({ data }: { data: RoleMixPoint[] }) {
  const rows = data.map((row) => ({ name: roleLabel(row.role), count: row.count }));
  const total = rows.reduce((sum, row) => sum + row.count, 0);

  return (
    <div className="border border-black/8 bg-white p-5">
      <div className="text-sm font-bold mb-3">Role Mix</div>
      {total === 0 ? (
        <div className="py-12 text-center text-sm text-black/30">No leads yet.</div>
      ) : (
        <ResponsiveContainer width="100%" height={260}>
          <PieChart>
            <Pie data={rows} dataKey="count" nameKey="name" innerRadius={55} outerRadius={90} paddingAngle={2}>
              {rows.map((row, index) => (
                <Cell key={row.name} fill={categoricalColor(index)} />
              ))}
            </Pie>
            <Tooltip contentStyle={{ fontSize: 12, border: "1px solid rgba(0,0,0,0.1)" }} />
            <Legend wrapperStyle={{ fontSize: 11 }} />
          </PieChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
