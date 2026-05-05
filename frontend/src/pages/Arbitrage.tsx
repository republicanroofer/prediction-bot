import { useEffect, useState } from "react";
import { EmptyState } from "../components/EmptyState";
import { api, n, type ArbitrageOpp } from "../lib/api";

export function Arbitrage() {
  const [data, setData] = useState<ArbitrageOpp[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let c = false;
    async function load() {
      try {
        const d = await api.arbitrage();
        if (!c) setData(d);
      } finally {
        if (!c) setLoading(false);
      }
    }
    load();
    const id = setInterval(load, 30_000);
    return () => { c = true; clearInterval(id); };
  }, []);

  const actionable = data.filter((d) => n(d.gap_pct) >= 5);
  const watching = data.filter((d) => n(d.gap_pct) < 5);

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <h2 className="text-gray-300 text-sm font-semibold">Cross-Market Arbitrage</h2>
        <span className="text-gray-600 text-xs">Same event, different prices on Kalshi vs Polymarket</span>
        <span className="text-gray-500 text-xs ml-auto">{data.length} pairs tracked</span>
      </div>

      {loading ? (
        <EmptyState message="Scanning for arbitrage opportunities..." />
      ) : data.length === 0 ? (
        <EmptyState message="No cross-exchange price gaps found" />
      ) : (
        <>
          {actionable.length > 0 && (
            <section className="bg-gray-900 border border-green-800/50 rounded-lg p-4">
              <h3 className="text-green-400 text-xs font-semibold uppercase mb-3">
                Actionable ({">"}5% gap) <span className="text-gray-500 font-normal">- {actionable.length} opportunities</span>
              </h3>
              <ArbTable rows={actionable} />
            </section>
          )}

          {watching.length > 0 && (
            <section className="bg-gray-900 border border-gray-800 rounded-lg p-4">
              <h3 className="text-gray-400 text-xs font-semibold uppercase mb-3">
                Watching (3-5% gap) <span className="text-gray-500 font-normal">- {watching.length} pairs</span>
              </h3>
              <ArbTable rows={watching} />
            </section>
          )}
        </>
      )}
    </div>
  );
}

function ArbTable({ rows }: { rows: ArbitrageOpp[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-gray-500 border-b border-gray-800 text-left text-xs">
            <th className="pb-2 pr-3">Market</th>
            <th className="pb-2 pr-3">Category</th>
            <th className="pb-2 pr-3 text-right">Kalshi</th>
            <th className="pb-2 pr-3 text-right">Polymarket</th>
            <th className="pb-2 pr-3 text-right">Gap</th>
            <th className="pb-2 pr-3">Buy</th>
            <th className="pb-2 text-right">Closes</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const gap = n(r.gap_pct);
            const gapColor = gap >= 10 ? "text-green-400" : gap >= 5 ? "text-yellow-400" : "text-gray-400";
            const kMid = n(r.kalshi_mid);
            const pMid = n(r.poly_mid);
            const closesIn = r.close_time ? daysUntil(r.close_time) : null;
            return (
              <tr key={r.title} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                <td className="py-1.5 pr-3 text-gray-300 max-w-[250px] truncate" title={r.title}>
                  {r.title}
                </td>
                <td className="py-1.5 pr-3 text-gray-500 text-xs capitalize">{r.category ?? "—"}</td>
                <td className={`py-1.5 pr-3 text-right font-mono ${r.cheap_exchange === "kalshi" ? "text-green-400 font-semibold" : "text-gray-400"}`}>
                  {(kMid * 100).toFixed(1)}¢
                </td>
                <td className={`py-1.5 pr-3 text-right font-mono ${r.cheap_exchange === "polymarket" ? "text-green-400 font-semibold" : "text-gray-400"}`}>
                  {(pMid * 100).toFixed(1)}¢
                </td>
                <td className={`py-1.5 pr-3 text-right font-mono font-semibold ${gapColor}`}>
                  {gap.toFixed(1)}%
                </td>
                <td className="py-1.5 pr-3">
                  <span className={`text-xs px-1.5 py-0.5 rounded font-semibold ${
                    r.cheap_exchange === "kalshi" ? "bg-blue-900/50 text-blue-300" : "bg-purple-900/50 text-purple-300"
                  }`}>
                    {r.cheap_exchange === "kalshi" ? "KALSHI" : "POLY"}
                  </span>
                </td>
                <td className="py-1.5 text-right text-xs text-gray-500">
                  {closesIn != null ? (closesIn < 1 ? "<1d" : `${closesIn}d`) : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function daysUntil(iso: string): number {
  return Math.max(0, Math.round((new Date(iso).getTime() - Date.now()) / 86_400_000));
}
