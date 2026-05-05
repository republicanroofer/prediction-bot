import { useEffect, useState } from "react";
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip } from "recharts";
import { api, n, type CategoryExposure } from "../lib/api";

const COLORS = ["#0ea5e9", "#8b5cf6", "#22c55e", "#eab308", "#ef4444", "#f97316", "#ec4899", "#6366f1"];

export function ExposurePieChart() {
  const [data, setData] = useState<CategoryExposure[]>([]);

  useEffect(() => {
    let c = false;
    async function load() {
      try {
        const d = await api.exposure();
        if (!c) setData(d.filter((e) => n(e.exposure_usd) > 0));
      } catch {}
    }
    load();
    const id = setInterval(load, 30_000);
    return () => { c = true; clearInterval(id); };
  }, []);

  if (data.length === 0) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-gray-400 text-xs font-semibold uppercase mb-3">Category Exposure</h3>
        <p className="text-gray-600 text-sm py-4 text-center">No exposure data</p>
      </div>
    );
  }

  const chartData = data.map((d) => ({
    name: d.category || "unknown",
    value: n(d.exposure_usd),
    count: d.positions_count,
  }));

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="text-gray-400 text-xs font-semibold uppercase mb-3">Category Exposure</h3>
      <div className="flex flex-col sm:flex-row items-center gap-4">
        <ResponsiveContainer width="100%" height={160}>
          <PieChart>
            <Pie
              data={chartData}
              cx="50%"
              cy="50%"
              outerRadius={60}
              innerRadius={30}
              dataKey="value"
              stroke="none"
            >
              {chartData.map((_, i) => (
                <Cell key={i} fill={COLORS[i % COLORS.length]} />
              ))}
            </Pie>
            <Tooltip
              contentStyle={{ backgroundColor: "#1f2937", border: "1px solid #374151", borderRadius: 8, fontSize: 12 }}
              formatter={(v: number) => [`$${n(v).toFixed(2)}`, "Exposure"]}
            />
          </PieChart>
        </ResponsiveContainer>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
          {chartData.map((d, i) => (
            <div key={d.name} className="flex items-center gap-1.5">
              <div className="w-2 h-2 rounded-full" style={{ backgroundColor: COLORS[i % COLORS.length] }} />
              <span className="text-gray-400 capitalize">{d.name}</span>
              <span className="text-gray-600">${n(d.value).toFixed(0)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
