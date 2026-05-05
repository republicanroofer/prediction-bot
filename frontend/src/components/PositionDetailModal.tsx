import { useEffect, useState } from "react";
import { api, n, type Position, type Order } from "../lib/api";

type Props = {
  position: Position;
  onClose: () => void;
};

export function PositionDetailModal({ position, onClose }: Props) {
  const [orders, setOrders] = useState<Order[]>([]);

  useEffect(() => {
    api.orders(position.id).then(setOrders).catch(() => {});
  }, [position.id]);

  const p = position;
  const pnl = n(p.unrealized_pnl);
  const pnlColor = pnl > 0 ? "text-green-400" : pnl < 0 ? "text-red-400" : "text-gray-400";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="bg-gray-900 border border-gray-800 rounded-lg w-full max-w-lg max-h-[80vh] overflow-y-auto p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex justify-between items-start mb-4">
          <div>
            <h2 className="text-gray-200 font-semibold text-sm">Position Detail</h2>
            <div className="flex gap-2 mt-1 text-xs">
              <span className="text-gray-500">{p.exchange.toUpperCase()}</span>
              <span className={p.side === "yes" ? "text-green-400" : "text-red-400"}>
                {p.side.toUpperCase()}
              </span>
              <span className="text-gray-500">{p.signal_type}</span>
            </div>
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-300 text-lg leading-none">&times;</button>
        </div>

        <div className="grid grid-cols-2 gap-3 mb-4">
          {[
            ["Entry Price", `${(n(p.avg_entry_price) * 100).toFixed(1)}¢`],
            ["Contracts", n(p.current_contracts).toFixed(0)],
            ["Cost Basis", `$${n(p.cost_basis_usd).toFixed(2)}`],
            ["Status", p.status],
            ["Unrealized P&L", `${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}`],
            ["Realized P&L", p.realized_pnl != null ? `$${n(p.realized_pnl).toFixed(2)}` : "—"],
          ].map(([label, value]) => (
            <div key={String(label)}>
              <div className="text-[10px] text-gray-500 uppercase">{label}</div>
              <div className={`text-sm ${label === "Unrealized P&L" ? pnlColor : "text-gray-300"}`}>
                {value}
              </div>
            </div>
          ))}
        </div>

        {orders.length > 0 && (
          <div className="border-t border-gray-800 pt-3 mb-4">
            <h3 className="text-gray-500 text-xs font-semibold uppercase mb-2">Orders ({orders.length})</h3>
            <div className="space-y-1">
              {orders.map((o) => (
                <div key={o.id} className="flex justify-between text-xs text-gray-400">
                  <span>{o.is_opening ? "OPEN" : "CLOSE"} {o.side.toUpperCase()}</span>
                  <span>{n(o.requested_contracts).toFixed(0)} @ {(n(o.requested_price) * 100).toFixed(1)}¢</span>
                  <span className="text-gray-600">{o.status}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="border-t border-gray-800 pt-3 text-xs text-gray-500 space-y-1">
          <div>Opened: {new Date(p.opened_at).toLocaleString()}</div>
          {p.close_reason && <div>Close reason: {p.close_reason}</div>}
        </div>
      </div>
    </div>
  );
}
