import { useEffect, useState } from "react";
import { api, n, type FunnelMetrics as FM } from "../lib/api";

export function FunnelMetrics() {
  const [data, setData] = useState<FM | null>(null);

  useEffect(() => {
    let c = false;
    async function load() {
      try {
        const d = await api.funnel(24);
        if (!c) setData(d);
      } catch {}
    }
    load();
    const id = setInterval(load, 60_000);
    return () => { c = true; clearInterval(id); };
  }, []);

  if (!data) return null;

  const scanned = n(data.markets_scanned);
  const steps = [
    { label: "Markets Scanned", value: scanned, color: "text-gray-300" },
    { label: "Signals Generated", value: n(data.signals_generated), color: "text-yellow-400" },
    { label: "Trades Blocked", value: n(data.trades_blocked), color: "text-red-400" },
    { label: "Trades Executed", value: n(data.trades_executed), color: "text-green-400" },
  ];

  const passRate = scanned > 0
    ? ((n(data.trades_executed) / scanned) * 100).toFixed(2)
    : "0";

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-gray-400 text-xs font-semibold uppercase">Signal Funnel (24h)</h3>
        <span className="text-gray-600 text-xs">{passRate}% pass-through</span>
      </div>
      <div className="grid grid-cols-2 sm:flex sm:items-center sm:justify-between gap-2">
        {steps.map((s, i) => (
          <div key={s.label} className="flex items-center gap-2 sm:flex-1">
            <div className="text-center flex-1">
              <div className={`text-lg sm:text-xl font-bold ${s.color}`}>{s.value.toLocaleString()}</div>
              <div className="text-[10px] text-gray-500 mt-0.5">{s.label}</div>
            </div>
            {i < steps.length - 1 && <span className="text-gray-700 text-lg hidden sm:inline">&rarr;</span>}
          </div>
        ))}
      </div>
    </div>
  );
}
