import { useEffect, useState } from "react";
import { api, n, type Opportunity } from "../lib/api";

export function OpportunitiesQueue() {
  const [items, setItems] = useState<Opportunity[]>([]);

  useEffect(() => {
    let c = false;
    async function load() {
      try {
        const d = await api.opportunities(20);
        if (!c) setItems(d);
      } catch {}
    }
    load();
    const id = setInterval(load, 30_000);
    return () => { c = true; clearInterval(id); };
  }, []);

  if (items.length === 0) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-gray-400 text-xs font-semibold uppercase mb-3">Opportunities Queue</h3>
        <p className="text-gray-600 text-sm py-4 text-center">No opportunities detected</p>
      </div>
    );
  }

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="text-gray-400 text-xs font-semibold uppercase mb-3">
        Opportunities Queue <span className="text-gray-600 font-normal">({items.length})</span>
      </h3>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-gray-500 border-b border-gray-800 text-left text-xs">
              <th className="pb-2 pr-3">Market</th>
              <th className="pb-2 pr-3 hidden sm:table-cell">Exchange</th>
              <th className="pb-2 pr-3">Price</th>
              <th className="pb-2 pr-3">Edge</th>
              <th className="pb-2 pr-3 hidden md:table-cell">Conf.</th>
              <th className="pb-2 pr-3 hidden lg:table-cell">Relevance</th>
              <th className="pb-2 hidden lg:table-cell">Days</th>
            </tr>
          </thead>
          <tbody>
            {items.map((o) => {
              const edge = n(o.edge);
              const edgeColor = edge > 0.1 ? "text-green-400" : edge > 0 ? "text-yellow-400" : "text-gray-500";
              return (
                <tr key={o.market_id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                  <td className="py-1.5 pr-3 text-gray-300 max-w-[200px] truncate" title={o.title}>
                    {o.title}
                  </td>
                  <td className="py-1.5 pr-3 text-gray-500 text-xs hidden sm:table-cell">
                    {o.exchange === "kalshi" ? "KALSHI" : "POLY"}
                  </td>
                  <td className="py-1.5 pr-3">{o.yes_mid ? `${(n(o.yes_mid) * 100).toFixed(0)}¢` : "—"}</td>
                  <td className={`py-1.5 pr-3 font-semibold ${edgeColor}`}>
                    {edge > 0 ? "+" : ""}{(edge * 100).toFixed(1)}%
                  </td>
                  <td className="py-1.5 pr-3 text-gray-400 hidden md:table-cell">{(n(o.confidence) * 100).toFixed(0)}%</td>
                  <td className="py-1.5 pr-3 text-gray-400 hidden lg:table-cell">{(n(o.relevance) * 100).toFixed(0)}%</td>
                  <td className="py-1.5 text-gray-500 hidden lg:table-cell">{o.days_to_close != null ? n(o.days_to_close).toFixed(0) : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
