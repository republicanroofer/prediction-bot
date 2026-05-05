import { useEffect, useMemo, useState } from "react";
import { EmptyState } from "../components/EmptyState";
import { api, n, type Market } from "../lib/api";

export function Markets() {
  const [markets, setMarkets] = useState<Market[]>([]);
  const [loading, setLoading] = useState(true);
  const [exchange, setExchange] = useState<string>("all");
  const [search, setSearch] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      try {
        const data = await api.markets(200);
        if (!cancelled) setMarkets(data);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    const id = setInterval(load, 60_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const filtered = useMemo(() => {
    return markets.filter((m) => {
      if (exchange !== "all" && m.exchange !== exchange) return false;
      if (search && !m.title.toLowerCase().includes(search.toLowerCase())) return false;
      return true;
    });
  }, [markets, exchange, search]);

  const exchanges = ["all", ...Array.from(new Set(markets.map((m) => m.exchange)))];

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="flex gap-3 items-center flex-wrap">
        <div className="flex gap-1">
          {exchanges.map((ex) => (
            <button
              key={ex}
              onClick={() => setExchange(ex)}
              className={`px-3 py-1 text-xs rounded-full border transition-colors ${
                exchange === ex
                  ? "border-brand-500 text-brand-500 bg-brand-500/10"
                  : "border-gray-700 text-gray-400 hover:border-gray-500"
              }`}
            >
              {ex === "all" ? `All (${markets.length})` : `${ex} (${markets.filter(m => m.exchange === ex).length})`}
            </button>
          ))}
        </div>
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search markets…"
          className="flex-1 max-w-xs bg-gray-800 border border-gray-700 rounded px-3 py-1 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-brand-500"
        />
        <span className="text-gray-500 text-xs ml-auto">{filtered.length} markets</span>
      </div>

      {loading ? (
        <EmptyState message="Loading markets…" />
      ) : filtered.length === 0 ? (
        <EmptyState message="No markets match your filters" />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-gray-400 border-b border-gray-800 text-left">
                <th className="pb-2 pr-3">Exchange</th>
                <th className="pb-2 pr-3">Title</th>
                <th className="pb-2 pr-3">Category</th>
                <th className="pb-2 pr-3 text-right">YES Mid</th>
                <th className="pb-2 pr-3 text-right">Vol 24h</th>
                <th className="pb-2 text-right">Closes</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((m) => {
                const yesMid = m.yes_bid != null && m.yes_ask != null
                  ? ((n(m.yes_bid) + n(m.yes_ask)) / 2)
                  : m.last_price != null ? n(m.last_price) : null;
                const closesIn = m.close_time ? daysUntil(m.close_time) : null;
                return (
                  <tr key={m.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                    <td className="py-1.5 pr-3">
                      <span className={`text-xs px-1.5 py-0.5 rounded uppercase font-semibold ${
                        m.exchange === "kalshi" ? "bg-blue-900/50 text-blue-300" : "bg-purple-900/50 text-purple-300"
                      }`}>
                        {m.exchange}
                      </span>
                    </td>
                    <td className="py-1.5 pr-3 text-gray-200 max-w-xs">
                      <span className="line-clamp-1" title={m.title}>{m.title}</span>
                    </td>
                    <td className="py-1.5 pr-3 text-gray-500 text-xs">{m.category ?? "—"}</td>
                    <td className="py-1.5 pr-3 text-right font-mono">
                      {yesMid != null ? (
                        <ProbBar value={yesMid} />
                      ) : "—"}
                    </td>
                    <td className="py-1.5 pr-3 text-right text-gray-300 font-mono text-xs">
                      {m.volume_24h_usd != null ? `$${fmtK(n(m.volume_24h_usd))}` : "—"}
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
      )}
    </div>
  );
}

function ProbBar({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const color = pct >= 70 ? "text-green-400" : pct <= 30 ? "text-red-400" : "text-yellow-400";
  return (
    <span className={`font-semibold ${color}`}>{pct}¢</span>
  );
}

function fmtK(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return n.toFixed(0);
}

function daysUntil(iso: string): number {
  return Math.max(0, Math.round((new Date(iso).getTime() - Date.now()) / 86_400_000));
}
