import { useEffect, useState } from "react";
import { api, type Decision } from "../lib/api";

export function Decisions() {
  const [data, setData] = useState<Decision[]>([]);
  const [hours, setHours] = useState(24);
  const [filter, setFilter] = useState<"all" | "accepted" | "rejected">("all");

  useEffect(() => {
    let c = false;
    async function load() {
      try {
        const d = await api.decisions(hours, 200);
        if (!c) setData(d);
      } catch {}
    }
    load();
    const id = setInterval(load, 15_000);
    return () => { c = true; clearInterval(id); };
  }, [hours]);

  const filtered = filter === "all" ? data : data.filter((d) => d.decision === filter);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-gray-300 text-sm font-semibold mr-4">Trade Decision Log</h2>
        {(["all", "accepted", "rejected"] as const).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`px-3 py-1 text-xs rounded-full border ${
              filter === f
                ? f === "accepted" ? "border-green-500 text-green-400 bg-green-500/10"
                : f === "rejected" ? "border-red-500 text-red-400 bg-red-500/10"
                : "border-brand-500 text-brand-500 bg-brand-500/10"
                : "border-gray-700 text-gray-400 hover:border-gray-600"
            }`}
          >
            {f === "all" ? "All" : f === "accepted" ? "✓ Accepted" : "✗ Rejected"}
          </button>
        ))}
        <div className="ml-auto flex gap-1">
          {[1, 6, 24, 72].map((h) => (
            <button
              key={h}
              onClick={() => setHours(h)}
              className={`px-2 py-1 text-xs rounded ${hours === h ? "bg-gray-700 text-gray-200" : "text-gray-500 hover:text-gray-300"}`}
            >
              {h}h
            </button>
          ))}
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        {filtered.length === 0 ? (
          <p className="text-gray-600 text-sm py-8 text-center">No decisions in this period</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-500 border-b border-gray-800 text-left text-xs">
                  <th className="p-3">Time</th>
                  <th className="p-3">Decision</th>
                  <th className="p-3">Exchange</th>
                  <th className="p-3">Market</th>
                  <th className="p-3 hidden sm:table-cell">Signal</th>
                  <th className="p-3 hidden md:table-cell">Side</th>
                  <th className="p-3 hidden md:table-cell">Size</th>
                  <th className="p-3">Reason</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((d, i) => (
                  <tr key={i} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                    <td className="p-3 text-gray-500 text-xs whitespace-nowrap">
                      {new Date(d.ts).toLocaleTimeString()}
                    </td>
                    <td className="p-3">
                      <span className={`text-xs font-semibold ${d.decision === "accepted" ? "text-green-400" : "text-red-400"}`}>
                        {d.decision === "accepted" ? "✓ ACCEPT" : "✗ REJECT"}
                      </span>
                    </td>
                    <td className="p-3 text-gray-400 text-xs">{d.exchange?.toUpperCase()}</td>
                    <td className="p-3 text-gray-300 max-w-[200px] truncate" title={d.market_title}>
                      {d.market_title}
                    </td>
                    <td className="p-3 text-gray-500 text-xs hidden sm:table-cell">{d.signal_type}</td>
                    <td className="p-3 hidden md:table-cell">
                      <span className={d.side === "yes" ? "text-green-400 text-xs" : "text-red-400 text-xs"}>
                        {d.side?.toUpperCase()}
                      </span>
                    </td>
                    <td className="p-3 text-gray-400 text-xs hidden md:table-cell">
                      {d.size_usd ? `$${Number(d.size_usd).toFixed(2)}` : "—"}
                    </td>
                    <td className="p-3 text-gray-500 text-xs max-w-[200px] truncate" title={d.block_reason ?? ""}>
                      {d.decision === "accepted" ? "Signal met threshold" : (d.block_reason ?? d.gate ?? "—")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
