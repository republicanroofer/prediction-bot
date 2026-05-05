import { useEffect, useState } from "react";
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip } from "recharts";
import { api, n, type CategoryExposure, type CategoryPosition } from "../lib/api";

const COLORS = ["#0ea5e9", "#8b5cf6", "#22c55e", "#eab308", "#ef4444", "#f97316", "#ec4899", "#6366f1"];

type MergedCategory = {
  category: string;
  exposure_usd: number;
  unrealized_pnl: number;
  positions_count: number;
};

function mergeByCategory(data: CategoryExposure[]): MergedCategory[] {
  const map = new Map<string, MergedCategory>();
  for (const d of data) {
    const cat = d.category || "unknown";
    const existing = map.get(cat);
    if (existing) {
      existing.exposure_usd += n(d.exposure_usd);
      existing.unrealized_pnl += n(d.unrealized_pnl);
      existing.positions_count += n(d.positions_count);
    } else {
      map.set(cat, {
        category: cat,
        exposure_usd: n(d.exposure_usd),
        unrealized_pnl: n(d.unrealized_pnl),
        positions_count: n(d.positions_count),
      });
    }
  }
  return Array.from(map.values()).sort((a, b) => b.exposure_usd - a.exposure_usd);
}

export function ExposurePieChart() {
  const [raw, setRaw] = useState<CategoryExposure[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [positions, setPositions] = useState<CategoryPosition[]>([]);
  const [loadingPositions, setLoadingPositions] = useState(false);

  useEffect(() => {
    let c = false;
    async function load() {
      try {
        const d = await api.exposure();
        if (!c) setRaw(d);
      } catch {}
    }
    load();
    const id = setInterval(load, 30_000);
    return () => { c = true; clearInterval(id); };
  }, []);

  useEffect(() => {
    if (!selected) { setPositions([]); return; }
    let c = false;
    setLoadingPositions(true);
    api.positionsByCategory(selected)
      .then((d) => { if (!c) setPositions(d); })
      .catch(() => {})
      .finally(() => { if (!c) setLoadingPositions(false); });
    return () => { c = true; };
  }, [selected]);

  const merged = mergeByCategory(raw).filter((d) => d.exposure_usd > 0);

  if (merged.length === 0) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-gray-400 text-xs font-semibold uppercase mb-3">Category Exposure</h3>
        <p className="text-gray-600 text-sm py-4 text-center">No exposure data</p>
      </div>
    );
  }

  const chartData = merged.map((d) => ({
    name: d.category,
    value: d.exposure_usd,
    count: d.positions_count,
    pnl: d.unrealized_pnl,
  }));

  return (
    <>
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
                cursor="pointer"
                onClick={(_, i) => setSelected(chartData[i].name)}
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
              <button
                key={d.name}
                onClick={() => setSelected(d.name)}
                className="flex items-center gap-1.5 hover:bg-gray-800 rounded px-1 py-0.5 transition-colors"
              >
                <div className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: COLORS[i % COLORS.length] }} />
                <span className="text-gray-400 capitalize">{d.name}</span>
                <span className="text-gray-600">${n(d.value).toFixed(0)}</span>
                <span className="text-gray-700">({d.count})</span>
              </button>
            ))}
          </div>
        </div>
      </div>

      {selected && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={() => setSelected(null)}>
          <div
            className="bg-gray-900 border border-gray-800 rounded-lg w-full max-w-2xl max-h-[80vh] overflow-y-auto p-5"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex justify-between items-center mb-4">
              <h2 className="text-gray-200 font-semibold text-sm capitalize">
                {selected} Positions
                <span className="text-gray-500 font-normal ml-2">({positions.length})</span>
              </h2>
              <button onClick={() => setSelected(null)} className="text-gray-500 hover:text-gray-300 text-lg leading-none">&times;</button>
            </div>

            {loadingPositions ? (
              <p className="text-gray-600 text-sm py-8 text-center">Loading...</p>
            ) : positions.length === 0 ? (
              <p className="text-gray-600 text-sm py-8 text-center">No open positions in this category</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-gray-500 border-b border-gray-800 text-left text-xs">
                      <th className="pb-2 pr-3">Market</th>
                      <th className="pb-2 pr-3">Exch</th>
                      <th className="pb-2 pr-3">Side</th>
                      <th className="pb-2 pr-3 text-right">Entry</th>
                      <th className="pb-2 pr-3 text-right">P&L</th>
                      <th className="pb-2 pr-3">Signal</th>
                      <th className="pb-2 text-right">Opened</th>
                    </tr>
                  </thead>
                  <tbody>
                    {positions.map((p) => {
                      const pnl = n(p.unrealized_pnl);
                      const pnlColor = pnl > 0 ? "text-green-400" : pnl < 0 ? "text-red-400" : "text-gray-400";
                      return (
                        <tr key={p.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                          <td className="py-1.5 pr-3 text-gray-300 max-w-[200px] truncate" title={p.market_title}>
                            {p.market_title}
                          </td>
                          <td className="py-1.5 pr-3">
                            <span className={`text-xs px-1.5 py-0.5 rounded font-semibold ${
                              p.exchange === "kalshi" ? "bg-blue-900/50 text-blue-300" : "bg-purple-900/50 text-purple-300"
                            }`}>
                              {p.exchange === "kalshi" ? "K" : "P"}
                            </span>
                          </td>
                          <td className="py-1.5 pr-3">
                            <span className={p.side === "yes" ? "text-green-400" : "text-red-400"}>
                              {p.side.toUpperCase()}
                            </span>
                          </td>
                          <td className="py-1.5 pr-3 text-right font-mono text-gray-300">
                            {(n(p.avg_entry_price) * 100).toFixed(1)}¢
                          </td>
                          <td className={`py-1.5 pr-3 text-right font-mono font-semibold ${pnlColor}`}>
                            {pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}
                          </td>
                          <td className="py-1.5 pr-3 text-gray-500 text-xs">{p.signal_type?.replace("_", " ")}</td>
                          <td className="py-1.5 text-right text-gray-500 text-xs">
                            {new Date(p.opened_at).toLocaleDateString()}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}
