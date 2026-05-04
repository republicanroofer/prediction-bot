import { useEffect, useState } from "react";
import { api, type Position, type PositionHistory } from "../lib/api";

type Props = {
  position: Position;
  onClose: () => void;
};

export function PositionDetailModal({ position, onClose }: Props) {
  const [history, setHistory] = useState<PositionHistory | null>(null);

  useEffect(() => {
    api.positionHistory(position.id).then(setHistory).catch(() => {});
  }, [position.id]);

  const pos = history?.position ?? position;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="bg-gray-900 border border-gray-800 rounded-lg w-full max-w-lg max-h-[80vh] overflow-y-auto p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex justify-between items-start mb-4">
          <div>
            <h2 className="text-gray-200 font-semibold text-sm">
              {history?.market?.title ?? pos.external_market_id ?? "Position Detail"}
            </h2>
            <div className="flex gap-2 mt-1 text-xs">
              <span className="text-gray-500">{String(pos.exchange).toUpperCase()}</span>
              <span className={pos.side === "yes" ? "text-green-400" : "text-red-400"}>
                {String(pos.side).toUpperCase()}
              </span>
              <span className="text-gray-500">{pos.signal_type}</span>
            </div>
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-300 text-lg leading-none">&times;</button>
        </div>

        {/* Key Metrics */}
        <div className="grid grid-cols-2 gap-3 mb-4">
          {[
            ["Entry Price", `$${Number(pos.avg_entry_price).toFixed(4)}`],
            ["Contracts", Number(pos.contracts ?? 0).toFixed(0)],
            ["Cost Basis", `$${Number(pos.cost_basis_usd).toFixed(2)}`],
            ["Current Price", pos.current_price ? `$${Number(pos.current_price).toFixed(4)}` : "—"],
            ["Unrealized P&L", `${Number(pos.unrealized_pnl ?? 0) >= 0 ? "+" : ""}$${Number(pos.unrealized_pnl ?? 0).toFixed(2)}`],
            ["Realized P&L", pos.realized_pnl != null ? `$${Number(pos.realized_pnl).toFixed(2)}` : "—"],
            ["Stop Loss", pos.stop_loss_price ? `$${Number(pos.stop_loss_price).toFixed(4)}` : "—"],
            ["Take Profit", pos.take_profit_price ? `$${Number(pos.take_profit_price).toFixed(4)}` : "—"],
            ["Kelly Used", pos.kelly_fraction_used ? `${(Number(pos.kelly_fraction_used) * 100).toFixed(2)}%` : "—"],
            ["Status", String(pos.status)],
          ].map(([label, value]) => (
            <div key={String(label)}>
              <div className="text-[10px] text-gray-500 uppercase">{label}</div>
              <div className="text-sm text-gray-300">{value}</div>
            </div>
          ))}
        </div>

        {/* Whale Info */}
        {pos.whale_address && (
          <div className="border-t border-gray-800 pt-3 mb-4">
            <h3 className="text-gray-500 text-xs font-semibold uppercase mb-2">Whale Mirror</h3>
            <div className="text-xs text-gray-400 space-y-1">
              <div>Address: <span className="text-gray-300 font-mono">{String(pos.whale_address).slice(0, 10)}...</span></div>
              {pos.whale_score && <div>Score: <span className="text-gray-300">{Number(pos.whale_score).toFixed(0)}</span></div>}
            </div>
          </div>
        )}

        {/* Orders */}
        {history && history.orders.length > 0 && (
          <div className="border-t border-gray-800 pt-3 mb-4">
            <h3 className="text-gray-500 text-xs font-semibold uppercase mb-2">Orders ({history.orders.length})</h3>
            <div className="space-y-1">
              {history.orders.map((o, i) => (
                <div key={i} className="flex justify-between text-xs text-gray-400">
                  <span>{o.is_opening ? "OPEN" : "CLOSE"} {String(o.side).toUpperCase()}</span>
                  <span>{Number(o.requested_contracts).toFixed(0)} @ ${Number(o.requested_price).toFixed(4)}</span>
                  <span className="text-gray-600">{o.status}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Fills */}
        {history && history.fills.length > 0 && (
          <div className="border-t border-gray-800 pt-3 mb-4">
            <h3 className="text-gray-500 text-xs font-semibold uppercase mb-2">Fills ({history.fills.length})</h3>
            <div className="space-y-1">
              {history.fills.map((f, i) => (
                <div key={i} className="flex justify-between text-xs text-gray-400">
                  <span>{Number(f.contracts).toFixed(0)} @ ${Number(f.price).toFixed(4)}</span>
                  <span>{f.fees_usd ? `fee $${Number(f.fees_usd).toFixed(4)}` : ""}</span>
                  <span className="text-gray-600">{new Date(f.filled_at).toLocaleString()}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Timestamps */}
        <div className="border-t border-gray-800 pt-3 text-xs text-gray-500 space-y-1">
          <div>Opened: {new Date(pos.opened_at).toLocaleString()}</div>
          {pos.closed_at && <div>Closed: {new Date(pos.closed_at).toLocaleString()}</div>}
          {pos.close_reason && <div>Reason: {pos.close_reason}</div>}
          {pos.max_hold_until && <div>Max hold: {new Date(pos.max_hold_until).toLocaleString()}</div>}
        </div>
      </div>
    </div>
  );
}
