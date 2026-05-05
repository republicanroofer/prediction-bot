import { useEffect, useState } from "react";
import { api, n, type DecisionSummary, type EvalDecision } from "../lib/api";

const REASON_COLORS: Record<string, string> = {
  "volume too low": "bg-gray-700",
  "no signal": "bg-yellow-800",
  "expires too soon": "bg-orange-800",
  "expires too far": "bg-orange-900",
  "category blocked": "bg-red-900",
  "no close date": "bg-gray-800",
  "already positioned": "bg-blue-900",
  "price too extreme": "bg-purple-900",
  "edge too low": "bg-red-800",
  "kelly size too small": "bg-red-700",
  "risk gate": "bg-red-600",
  "signal fired": "bg-green-800",
};

function bucketColor(reason: string): string {
  for (const [key, color] of Object.entries(REASON_COLORS)) {
    if (reason.startsWith(key)) return color;
  }
  return "bg-gray-700";
}

function bucketLabel(reason: string): string {
  return reason.replace(/_/g, " ");
}

export function Decisions() {
  const [summary, setSummary] = useState<DecisionSummary | null>(null);
  const [data, setData] = useState<EvalDecision[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<"all" | "accepted" | "rejected">("all");
  const [exchange, setExchange] = useState<string>("all");
  const [signalFilter, setSignalFilter] = useState<string>("all");
  const [reasonFilter, setReasonFilter] = useState<string | null>(null);

  useEffect(() => {
    let c = false;
    async function loadSummary() {
      try {
        const s = await api.decisionsSummary();
        if (!c) setSummary(s);
      } catch {}
    }
    loadSummary();
    const id = setInterval(loadSummary, 15_000);
    return () => { c = true; clearInterval(id); };
  }, []);

  useEffect(() => {
    let c = false;
    async function load() {
      try {
        const d = await api.decisionsLive({
          limit: 500,
          exchange: exchange === "all" ? undefined : exchange,
          decision: filter === "all" ? undefined : filter,
          signal_type: signalFilter === "all" ? undefined : signalFilter,
          reason: reasonFilter ?? undefined,
        });
        if (!c) { setData(d); setLoading(false); }
      } catch {
        if (!c) setLoading(false);
      }
    }
    load();
    const id = setInterval(load, 8_000);
    return () => { c = true; clearInterval(id); };
  }, [filter, exchange, signalFilter, reasonFilter]);

  const aggregated = aggregateBuckets(summary);
  const signalTypes = ["all", ...Array.from(new Set(data.map((d) => d.signal_type).filter(Boolean) as string[]))];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-gray-300 text-sm font-semibold mr-2">Decision Log</h2>
        <span className="text-gray-600 text-xs">24h &middot; auto-refreshes</span>
        {summary && <span className="text-gray-500 text-xs ml-auto">{summary.total.toLocaleString()} total evaluations</span>}
      </div>

      {/* Funnel breakdown */}
      {aggregated.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="text-gray-400 text-xs font-semibold uppercase mb-3">Evaluation Funnel (24h)</h3>
          {/* Stacked bar */}
          <div className="flex h-6 rounded-full overflow-hidden mb-3">
            {aggregated.map((b) => (
              <div
                key={b.reason}
                className={`${bucketColor(b.reason)} cursor-pointer hover:brightness-125 transition-all ${reasonFilter === b.reason ? "ring-2 ring-white/40" : ""}`}
                style={{ width: `${b.pct}%`, minWidth: b.pct > 0.5 ? "4px" : "0" }}
                title={`${bucketLabel(b.reason)}: ${b.count.toLocaleString()} (${b.pct.toFixed(1)}%)`}
                onClick={() => setReasonFilter(reasonFilter === b.reason ? null : b.reason)}
              />
            ))}
          </div>
          {/* Legend */}
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-x-4 gap-y-1.5">
            {aggregated.map((b) => (
              <button
                key={b.reason}
                onClick={() => setReasonFilter(reasonFilter === b.reason ? null : b.reason)}
                className={`flex items-center gap-2 text-left rounded px-1.5 py-0.5 transition-colors ${
                  reasonFilter === b.reason ? "bg-gray-800 ring-1 ring-gray-600" : "hover:bg-gray-800/50"
                }`}
              >
                <div className={`w-2.5 h-2.5 rounded-sm shrink-0 ${bucketColor(b.reason)}`} />
                <span className="text-gray-400 text-xs truncate flex-1">{bucketLabel(b.reason)}</span>
                <span className="text-gray-500 text-xs font-mono shrink-0">{fmtCount(b.count)}</span>
                <span className="text-gray-600 text-[10px] font-mono shrink-0 w-10 text-right">{b.pct.toFixed(1)}%</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2">
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
            {f === "all" ? "All" : f === "accepted" ? "Accepted" : "Rejected"}
          </button>
        ))}

        <span className="text-gray-700 mx-0.5">|</span>

        {["all", "kalshi", "polymarket"].map((ex) => (
          <button
            key={ex}
            onClick={() => setExchange(ex)}
            className={`px-2 py-1 text-xs rounded border ${
              exchange === ex
                ? "border-brand-500 text-brand-500 bg-brand-500/10"
                : "border-gray-700 text-gray-400 hover:border-gray-600"
            }`}
          >
            {ex === "all" ? "All" : ex === "kalshi" ? "Kalshi" : "Poly"}
          </button>
        ))}

        {signalTypes.length > 1 && (
          <>
            <span className="text-gray-700 mx-0.5">|</span>
            <select
              value={signalFilter}
              onChange={(e) => setSignalFilter(e.target.value)}
              className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-300"
            >
              {signalTypes.map((s) => (
                <option key={s} value={s}>{s === "all" ? "All signals" : s.replace("_", " ")}</option>
              ))}
            </select>
          </>
        )}

        {reasonFilter && (
          <button
            onClick={() => setReasonFilter(null)}
            className="px-2 py-1 text-xs rounded border border-yellow-700 text-yellow-400 bg-yellow-900/20"
          >
            reason: {reasonFilter} &times;
          </button>
        )}
      </div>

      {/* Table */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        {loading ? (
          <p className="text-gray-600 text-sm py-8 text-center">Loading decisions...</p>
        ) : data.length === 0 ? (
          <p className="text-gray-600 text-sm py-8 text-center">No decisions match these filters</p>
        ) : (
          <div className="overflow-x-auto">
            <div className="text-gray-500 text-xs px-3 py-2 border-b border-gray-800">
              Showing {data.length} most recent{reasonFilter ? ` "${reasonFilter}"` : ""} decisions
            </div>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-500 border-b border-gray-800 text-left text-xs">
                  <th className="p-3">Time</th>
                  <th className="p-3">Decision</th>
                  <th className="p-3 hidden sm:table-cell">Exch</th>
                  <th className="p-3">Market</th>
                  <th className="p-3 hidden md:table-cell">Signal</th>
                  <th className="p-3 hidden md:table-cell">Price</th>
                  <th className="p-3 hidden lg:table-cell">Edge</th>
                  <th className="p-3 hidden lg:table-cell">Kelly</th>
                  <th className="p-3">Reason</th>
                </tr>
              </thead>
              <tbody>
                {data.map((d) => (
                  <DecisionRow key={d.id} d={d} onReasonClick={setReasonFilter} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function DecisionRow({ d, onReasonClick }: { d: EvalDecision; onReasonClick: (r: string) => void }) {
  const accepted = d.decision === "accepted";
  const edge = d.edge != null ? n(d.edge) : null;
  const edgePct = edge != null ? (edge * 100).toFixed(1) : null;
  const edgeColor = (edge ?? 0) > 0.05 ? "text-green-400" : (edge ?? 0) > 0 ? "text-yellow-400" : "text-gray-500";
  const reasonPrefix = d.reason.split(":")[0];
  const entryPrice = d.entry_price != null ? n(d.entry_price) : null;
  const kelly = d.kelly_size_usd != null ? n(d.kelly_size_usd) : null;

  return (
    <tr className={`border-b border-gray-800/50 hover:bg-gray-800/30 ${accepted ? "bg-green-950/10" : ""}`}>
      <td className="p-3 text-gray-500 text-xs whitespace-nowrap font-mono">
        {new Date(d.created_at).toLocaleTimeString()}
      </td>
      <td className="p-3">
        <span className={`text-xs font-bold px-1.5 py-0.5 rounded ${
          accepted ? "text-green-400 bg-green-900/40" : "text-red-400 bg-red-900/40"
        }`}>
          {accepted ? "ACCEPT" : "REJECT"}
        </span>
      </td>
      <td className="p-3 text-gray-400 text-xs hidden sm:table-cell">
        <span className={`px-1.5 py-0.5 rounded text-xs font-semibold ${
          d.exchange === "kalshi" ? "bg-blue-900/50 text-blue-300" : "bg-purple-900/50 text-purple-300"
        }`}>
          {d.exchange === "kalshi" ? "K" : "P"}
        </span>
      </td>
      <td className="p-3 text-gray-300 max-w-[200px] truncate" title={d.market_title ?? ""}>
        {d.market_title ?? d.external_market_id ?? "—"}
      </td>
      <td className="p-3 text-gray-500 text-xs hidden md:table-cell">
        {d.signal_type ? d.signal_type.replace("_", " ") : "—"}
      </td>
      <td className="p-3 text-gray-400 text-xs font-mono hidden md:table-cell">
        {entryPrice != null ? `${(entryPrice * 100).toFixed(0)}¢` : "—"}
      </td>
      <td className={`p-3 text-xs font-mono font-semibold hidden lg:table-cell ${edgeColor}`}>
        {edgePct != null ? `${Number(edgePct) > 0 ? "+" : ""}${edgePct}%` : "—"}
      </td>
      <td className="p-3 text-gray-400 text-xs font-mono hidden lg:table-cell">
        {kelly != null ? `$${kelly.toFixed(0)}` : "—"}
      </td>
      <td className="p-3 text-xs max-w-[280px]">
        <button
          onClick={() => onReasonClick(reasonPrefix)}
          className={`text-left ${accepted ? "text-green-400/80" : "text-red-400/80"} hover:underline`}
          title={d.reason}
        >
          <span className="line-clamp-1">{d.reason}</span>
        </button>
      </td>
    </tr>
  );
}

type AggBucket = { reason: string; count: number; pct: number };

function aggregateBuckets(summary: DecisionSummary | null): AggBucket[] {
  if (!summary || summary.total === 0) return [];
  const byReason: Record<string, number> = {};
  for (const b of summary.buckets) {
    byReason[b.reason] = (byReason[b.reason] || 0) + b.count;
  }
  return Object.entries(byReason)
    .map(([reason, count]) => ({ reason, count, pct: (count / summary.total) * 100 }))
    .sort((a, b) => b.count - a.count);
}

function fmtCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}
