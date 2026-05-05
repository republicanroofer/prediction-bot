import { PnLChart } from "../components/PnLChart";
import { PositionsTable } from "../components/PositionsTable";
import { StatCard } from "../components/StatCard";
import type { DailyPnL, Position, WsSnapshot } from "../lib/api";

type Props = {
  snapshot: WsSnapshot | null;
  pnlHistory: DailyPnL[];
};

export function Overview({ snapshot, pnlHistory }: Props) {
  const positions: Position[] = snapshot?.positions ?? [];
  const livePnl = snapshot?.pnl;

  const totalRealized = pnlHistory.reduce((s, d) => s + d.realized_pnl, 0);
  const totalWins = pnlHistory.reduce((s, d) => s + d.num_wins, 0);
  const totalTrades = pnlHistory.reduce((s, d) => s + d.num_wins + d.num_losses, 0);
  const winRate = totalTrades > 0 ? totalWins / totalTrades : 0;
  const exposure = positions.reduce((s, p) => s + Number(p.cost_basis_usd), 0);

  return (
    <div className="space-y-6">
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
          sub={`$${exposure.toFixed(2)} exposure`}
        />
      </div>

      <section className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h2 className="text-gray-300 text-sm font-semibold mb-3">Cumulative P&L (30 days)</h2>
        {pnlHistory.length > 0 ? (
          <PnLChart data={pnlHistory} />
        ) : (
          <p className="text-gray-600 text-sm py-8 text-center">No P&L data yet</p>
        )}
      </section>

      <section className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h2 className="text-gray-300 text-sm font-semibold mb-3">
          Open Positions <span className="text-gray-500 font-normal">({positions.length})</span>
        </h2>
        <PositionsTable positions={positions} />
      </section>
    </div>
  );
}
