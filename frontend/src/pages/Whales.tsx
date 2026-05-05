import { useEffect, useState } from "react";
import { EmptyState } from "../components/EmptyState";
import { api, n, type WhaleTrade, type WhaleScore } from "../lib/api";

export function Whales() {
  const [scores, setScores] = useState<WhaleScore[]>([]);
  const [trades, setTrades] = useState<WhaleTrade[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      try {
        const [s, t] = await Promise.all([api.whaleScores(50), api.whaleTrades(100)]);
        if (!cancelled) { setScores(s); setTrades(t); }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    const id = setInterval(load, 60_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const visibleTrades = selected
    ? trades.filter((t) => t.maker_address.toLowerCase() === selected.toLowerCase())
    : trades.slice(0, 30);

  return (
    <div className="space-y-6">
      {/* Leaderboard */}
      <section className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h2 className="text-gray-300 text-sm font-semibold mb-3">
          Top Whale Traders <span className="text-gray-500 font-normal">({scores.length} tracked)</span>
        </h2>
        {loading ? (
          <EmptyState message="Loading whale scores…" />
        ) : scores.length === 0 ? (
          <EmptyState message="No whale scores yet — scores build up after the first WhaleScorer run" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-400 border-b border-gray-800 text-left">
                  <th className="pb-2 pr-3">Rank</th>
                  <th className="pb-2 pr-3">Address</th>
                  <th className="pb-2 pr-3 text-right">Score</th>
                  <th className="pb-2 pr-3 text-right">Win Rate</th>
                  <th className="pb-2 pr-3 text-right">Big Win %</th>
                  <th className="pb-2 pr-3 text-right">Median Gain</th>
                  <th className="pb-2 pr-3 text-right">Markets</th>
                  <th className="pb-2 text-right">Last Trade</th>
                </tr>
              </thead>
              <tbody>
                {scores.map((s, i) => (
                  <tr
                    key={s.id}
                    onClick={() => setSelected(selected === s.address ? null : s.address)}
                    className={`border-b border-gray-800/50 cursor-pointer transition-colors ${
                      selected === s.address ? "bg-brand-500/10" : "hover:bg-gray-800/30"
                    }`}
                  >
                    <td className="py-1.5 pr-3 text-gray-500">#{i + 1}</td>
                    <td className="py-1.5 pr-3 font-mono text-xs text-gray-300">
                      {s.display_name ?? `${s.address.slice(0, 6)}…${s.address.slice(-4)}`}
                    </td>
                    <td className="py-1.5 pr-3 text-right">
                      <ScorePill score={n(s.composite_score)} />
                    </td>
                    <td className="py-1.5 pr-3 text-right font-mono text-xs">
                      {s.win_rate != null ? `${(n(s.win_rate) * 100).toFixed(1)}%` : "—"}
                    </td>
                    <td className="py-1.5 pr-3 text-right font-mono text-xs text-green-400">
                      {s.big_win_rate != null ? `${(n(s.big_win_rate) * 100).toFixed(1)}%` : "—"}
                    </td>
                    <td className="py-1.5 pr-3 text-right font-mono text-xs">
                      {s.median_gain_pct != null ? `${(n(s.median_gain_pct) * 100).toFixed(1)}%` : "—"}
                    </td>
                    <td className="py-1.5 pr-3 text-right text-gray-400">{s.markets_traded ?? "—"}</td>
                    <td className="py-1.5 text-right text-xs text-gray-500">
                      {s.last_trade_at ? fmtDate(s.last_trade_at) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Recent trades */}
      <section className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-gray-300 text-sm font-semibold">
            Recent Whale Trades
            {selected && (
              <span className="text-gray-500 font-normal ml-2">
                — {selected.slice(0, 8)}…
                <button onClick={() => setSelected(null)} className="ml-2 text-xs text-brand-500 hover:underline">
                  clear
                </button>
              </span>
            )}
          </h2>
          <span className="text-gray-500 text-xs">{visibleTrades.length} trades</span>
        </div>
        {visibleTrades.length === 0 ? (
          <EmptyState message="No recent whale trades" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-400 border-b border-gray-800 text-left">
                  <th className="pb-2 pr-3">Time</th>
                  <th className="pb-2 pr-3">Address</th>
                  <th className="pb-2 pr-3">Direction</th>
                  <th className="pb-2 pr-3 text-right">Size</th>
                  <th className="pb-2 pr-3 text-right">Price</th>
                  <th className="pb-2 text-center">Mirrored</th>
                </tr>
              </thead>
              <tbody>
                {visibleTrades.map((t) => (
                  <tr key={t.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                    <td className="py-1.5 pr-3 text-gray-500 text-xs">{fmtDate(t.block_timestamp)}</td>
                    <td className="py-1.5 pr-3 font-mono text-xs text-gray-300">
                      {t.maker_address.slice(0, 6)}…{t.maker_address.slice(-4)}
                    </td>
                    <td className="py-1.5 pr-3">
                      <span className={t.maker_direction === "buy" ? "text-green-400" : "text-red-400"}>
                        {t.maker_direction.toUpperCase()}
                      </span>
                    </td>
                    <td className="py-1.5 pr-3 text-right font-mono">${n(t.usd_amount).toFixed(2)}</td>
                    <td className="py-1.5 pr-3 text-right font-mono text-xs">{n(t.price).toFixed(3)}</td>
                    <td className="py-1.5 text-center text-xs">
                      {t.mirrored ? "✅" : t.mirror_queued_at ? "⏳" : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function ScorePill({ score }: { score: number }) {
  const s = n(score);
  const color = s >= 80 ? "bg-green-900/60 text-green-300" : s >= 60 ? "bg-yellow-900/60 text-yellow-300" : "bg-gray-800 text-gray-400";
  return <span className={`px-2 py-0.5 rounded text-xs font-mono font-semibold ${color}`}>{s.toFixed(0)}</span>;
}

function fmtDate(iso: string) {
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
