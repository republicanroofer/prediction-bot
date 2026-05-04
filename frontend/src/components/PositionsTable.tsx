import type { Position } from "../lib/api";

type Props = {
  positions: Position[];
  onSelect?: (p: Position) => void;
};

export function PositionsTable({ positions, onSelect }: Props) {
  if (positions.length === 0) {
    return <div className="text-gray-500 text-sm p-4">No open positions.</div>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-gray-400 border-b border-gray-800 text-left">
            <th className="pb-2 pr-4">Exchange</th>
            <th className="pb-2 pr-4">Side</th>
            <th className="pb-2 pr-4">Entry</th>
            <th className="pb-2 pr-4 hidden sm:table-cell">Contracts</th>
            <th className="pb-2 pr-4">Cost</th>
            <th className="pb-2 pr-4">P&L</th>
            <th className="pb-2 pr-4 hidden md:table-cell">Signal</th>
            <th className="pb-2 hidden lg:table-cell">Opened</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => {
            const pnl = Number(p.unrealized_pnl ?? 0);
            const contracts = Number(p.contracts ?? (p as any).current_contracts ?? 0);
            const pnlColor = pnl > 0 ? "text-green-400" : pnl < 0 ? "text-red-400" : "text-gray-400";
            return (
              <tr
                key={p.id}
                className={`border-b border-gray-800/50 ${onSelect ? "cursor-pointer hover:bg-gray-800/50" : "hover:bg-gray-800/30"}`}
                onClick={() => onSelect?.(p)}
              >
                <td className="py-1.5 pr-4 text-gray-300">{p.exchange?.toUpperCase()}</td>
                <td className="py-1.5 pr-4">
                  <span className={p.side === "yes" ? "text-green-400" : "text-red-400"}>
                    {p.side?.toUpperCase()}
                  </span>
                </td>
                <td className="py-1.5 pr-4">{Number(p.avg_entry_price).toFixed(3)}</td>
                <td className="py-1.5 pr-4 hidden sm:table-cell">{contracts.toFixed(0)}</td>
                <td className="py-1.5 pr-4">${Number(p.cost_basis_usd).toFixed(2)}</td>
                <td className={`py-1.5 pr-4 font-semibold ${pnlColor}`}>
                  {pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}
                </td>
                <td className="py-1.5 pr-4 text-gray-500 text-xs hidden md:table-cell">{p.signal_type}</td>
                <td className="py-1.5 text-gray-500 text-xs hidden lg:table-cell">
                  {new Date(p.opened_at).toLocaleString()}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
