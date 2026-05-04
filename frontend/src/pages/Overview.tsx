import { useState } from "react";
import { ExposurePieChart } from "../components/ExposurePieChart";
import { FunnelMetrics } from "../components/FunnelMetrics";
import { OpportunitiesQueue } from "../components/OpportunitiesQueue";
import { PnLChart } from "../components/PnLChart";
import { PositionDetailModal } from "../components/PositionDetailModal";
import { PositionsTable } from "../components/PositionsTable";
import { StatCard } from "../components/StatCard";
import { StrategyBreakdown } from "../components/StrategyBreakdown";
import type { DailyPnL, Position, WsSnapshot } from "../lib/api";

type Props = {
  snapshot: WsSnapshot | null;
  pnlHistory: DailyPnL[];
};

export function Overview({ snapshot, pnlHistory }: Props) {
  const [selectedPos, setSelectedPos] = useState<Position | null>(null);
  const positions: Position[] = snapshot?.positions ?? [];
  const livePnl = snapshot?.pnl;

  const totalRealized = pnlHistory.reduce((s, d) => s + d.realized_pnl, 0);
  const totalWins = pnlHistory.reduce((s, d) => s + d.num_wins, 0);
  const totalTrades = pnlHistory.reduce((s, d) => s + d.num_wins + d.num_losses, 0);
  const winRate = totalTrades > 0 ? totalWins / totalTrades : 0;
  const exposure = positions.reduce((s, p) => s + Number(p.cost_basis_usd), 0);

  return (
    <div className="space-y-4">
      {/* Stat cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard
          label="Realized P&L"
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
          label="Win Rate"
          value={`${(winRate * 100).toFixed(1)}%`}
          sub={`${totalWins}W / ${totalTrades - totalWins}L`}
          positive={winRate > 0.5}
        />
        <StatCard
          label="Exposure"
          value={`$${exposure.toFixed(2)}`}
          sub={`${positions.length} position${positions.length !== 1 ? "s" : ""}`}
        />
      </div>

      {/* Funnel */}
      <FunnelMetrics />

      {/* Middle row: PnL chart + strategy + exposure */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="text-gray-400 text-xs font-semibold uppercase mb-3">Cumulative P&L (30 days)</h3>
          {pnlHistory.length > 0 ? (
            <PnLChart data={pnlHistory} />
          ) : (
            <p className="text-gray-600 text-sm py-8 text-center">No P&L data yet</p>
          )}
        </div>
        <div className="space-y-4">
          <StrategyBreakdown positions={positions} />
          <ExposurePieChart />
        </div>
      </div>

      {/* Positions */}
      <section className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-gray-400 text-xs font-semibold uppercase mb-3">
          Open Positions <span className="text-gray-600 font-normal">({positions.length})</span>
        </h3>
        <PositionsTable positions={positions} onSelect={setSelectedPos} />
      </section>

      {/* Opportunities */}
      <OpportunitiesQueue />

      {/* Position detail modal */}
      {selectedPos && (
        <PositionDetailModal position={selectedPos} onClose={() => setSelectedPos(null)} />
      )}
    </div>
  );
}
