import { useEffect, useState } from "react";
import { api, type EvalDecision } from "../lib/api";

export function Decisions() {
  const [data, setData] = useState<EvalDecision[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<"all" | "accepted" | "rejected">("all");
  const [exchange, setExchange] = useState<string>("all");
  const [signalFilter, setSignalFilter] = useState<string>("all");

  useEffect(() => {
    let c = false;
    async function load() {
      try {
        const d = await api.decisionsLive({
          limit: 500,
          exchange: exchange === "all" ? undefined : exchange,
          decision: filter === "all" ? undefined : filter,
          signal_type: signalFilter === "all" ? undefined : signalFilter,
        });
        if (!c) { setData(d); setLoading(false); }
      } catch {
        if (!c) setLoading(false);
      }
    }
    load();
    const id = setInterval(load, 8_000);
    return () => { c = true; clearInterval(id); };
  }, [filter, exchange, signalFilter]);

  const signalTypes = ["all", ...Array.from(new Set(data.map((d) => d.signal_type).filter(Boolean) as string[]))];
  const acceptCount = data.filter((d) => d.decision === "accepted").length;
  const rejectCount = data.filter((d) => d.decision === "rejected").length;

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-gray-300 text-sm font-semibold mr-2">Decision Log</h2>
        <span className="text-gray-600 text-xs">auto-refreshes every 8s</span>
      </div>

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
            {f === "all" ? `All (${data.length})` : f === "accepted" ? `Accepted (${acceptCount})` : `Rejected (${rejectCount})`}
          </button>
        ))}

        <span className="text-gray-700 mx-1">|</span>

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
            {ex === "all" ? "All" : ex.charAt(0).toUpperCase() + ex.slice(1)}
          </button>
        ))}

        {signalTypes.length > 1 && (
          <>
            <span className="text-gray-700 mx-1">|</span>
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
      </div>

      {/* Stats bar */}
      <div className="flex gap-4 text-xs text-gray-500">
        <span>Showing {data.length} decisions (24h)</span>
        {acceptCount > 0 && (
          <span className="text-green-400">{acceptCount} accepted ({data.length > 0 ? ((acceptCount / data.length) * 100).toFixed(1) : 0}%)</span>
        )}
        {rejectCount > 0 && (
          <span className="text-red-400">{rejectCount} rejected</span>
        )}
      </div>

      {/* Table */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        {loading ? (
          <p className="text-gray-600 text-sm py-8 text-center">Loading decisions...</p>
        ) : data.length === 0 ? (
          <p className="text-gray-600 text-sm py-8 text-center">No decisions yet — waiting for next scanner cycle</p>
        ) : (
          <div className="overflow-x-auto">
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
                  <DecisionRow key={d.id} d={d} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function DecisionRow({ d }: { d: EvalDecision }) {
  const accepted = d.decision === "accepted";
  const edgePct = d.edge != null ? (d.edge * 100).toFixed(1) : null;
  const edgeColor = (d.edge ?? 0) > 0.05 ? "text-green-400" : (d.edge ?? 0) > 0 ? "text-yellow-400" : "text-gray-500";

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
        {d.entry_price != null ? `${(d.entry_price * 100).toFixed(0)}¢` : "—"}
      </td>
      <td className={`p-3 text-xs font-mono font-semibold hidden lg:table-cell ${edgeColor}`}>
        {edgePct != null ? `${Number(edgePct) > 0 ? "+" : ""}${edgePct}%` : "—"}
      </td>
      <td className="p-3 text-gray-400 text-xs font-mono hidden lg:table-cell">
        {d.kelly_size_usd != null ? `$${d.kelly_size_usd.toFixed(0)}` : "—"}
      </td>
      <td className="p-3 text-xs max-w-[280px]">
        <span className={accepted ? "text-green-400/80" : "text-red-400/80"} title={d.reason}>
          <span className="line-clamp-1">{d.reason}</span>
        </span>
      </td>
    </tr>
  );
}
