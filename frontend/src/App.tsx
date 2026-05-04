import { useEffect, useState } from "react";
import { PnLChart } from "./components/PnLChart";
import { PositionsTable } from "./components/PositionsTable";
import { StatCard } from "./components/StatCard";
import { StatusBar } from "./components/StatusBar";
import { fetchJSON } from "./lib/api";
import type { BotStatus, DailyPnL } from "./lib/api";
import { useWebSocket } from "./lib/useWebSocket";

export default function App() {
  const { snapshot, connected } = useWebSocket();
  const [status, setStatus] = useState<BotStatus | null>(null);
  const [pnlHistory, setPnlHistory] = useState<DailyPnL[]>([]);

  // Poll REST endpoints every 30 seconds
  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const [s, p] = await Promise.all([
          fetchJSON<BotStatus>("/control/status"),
          fetchJSON<DailyPnL[]>("/pnl/daily?days=30"),
        ]);
        if (!cancelled) {
          setStatus(s);
          setPnlHistory(p);
        }
      } catch {
        // silently retry
      }
    }

    load();
    const id = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const positions = snapshot?.positions ?? [];
  const livePnl = snapshot?.pnl;

  const totalRealized = pnlHistory.reduce((sum, d) => sum + d.realized_pnl, 0);
  const totalWins = pnlHistory.reduce((sum, d) => sum + d.num_wins, 0);
  const totalTrades = pnlHistory.reduce((sum, d) => sum + d.num_wins + d.num_losses, 0);
  const winRate = totalTrades > 0 ? totalWins / totalTrades : 0;

  return (
    <div className="min-h-screen flex flex-col">
      <StatusBar status={status} connected={connected} />

      <main className="flex-1 p-6 space-y-6 max-w-7xl mx-auto w-full">
        {/* Stats row */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <StatCard
            label="Realized P&L (30d)"
            value={`${totalRealized >= 0 ? "+" : ""}$${totalRealized.toFixed(2)}`}
            positive={totalRealized > 0}
            negative={totalRealized < 0}
          />
          <StatCard
            label="Unrealized P&L"
            value={`${(livePnl?.unrealized ?? 0) >= 0 ? "+" : ""}$${(livePnl?.unrealized ?? 0).toFixed(2)}`}
            positive={(livePnl?.unrealized ?? 0) > 0}
            negative={(livePnl?.unrealized ?? 0) < 0}
          />
          <StatCard
            label="Win Rate (30d)"
            value={`${(winRate * 100).toFixed(1)}%`}
            sub={`${totalWins}W / ${totalTrades - totalWins}L`}
            positive={winRate > 0.5}
          />
          <StatCard
            label="Open Positions"
            value={positions.length}
            sub={`$${positions.reduce((s, p) => s + Number(p.cost_basis_usd), 0).toFixed(2)} exposure`}
          />
        </div>

        {/* P&L chart */}
        <section className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h2 className="text-gray-300 text-sm font-semibold mb-3">Cumulative P&L (30 days)</h2>
          {pnlHistory.length > 0 ? (
            <PnLChart data={pnlHistory} />
          ) : (
            <p className="text-gray-600 text-sm py-8 text-center">No P&L data yet</p>
          )}
        </section>

        {/* Positions table */}
        <section className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h2 className="text-gray-300 text-sm font-semibold mb-3">
            Open Positions{" "}
            <span className="text-gray-500 font-normal">({positions.length})</span>
          </h2>
          <PositionsTable positions={positions} />
        </section>
      </main>
    </div>
  );
}
