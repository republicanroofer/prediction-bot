import { useEffect, useRef, useState } from "react";
import { api, n, type Market, type Opportunity } from "../lib/api";

export function LiveTicker() {
  const [markets, setMarkets] = useState<Market[]>([]);
  const [opps, setOpps] = useState<Opportunity[]>([]);
  const prevPrices = useRef<Record<string, number>>({});

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [m, o] = await Promise.all([api.markets(50), api.opportunities(15)]);
        if (!cancelled) {
          const next: Record<string, number> = {};
          for (const mk of m) {
            const mid = mk.yes_bid != null && mk.yes_ask != null
              ? (n(mk.yes_bid) + n(mk.yes_ask)) / 2
              : mk.last_price != null ? n(mk.last_price) : 0;
            next[mk.id] = mid;
          }
          prevPrices.current = next;
          setMarkets(m.filter((mk) => mk.is_active && n(mk.volume_24h_usd) > 0).slice(0, 30));
          setOpps(o);
        }
      } catch {}
    }
    load();
    const id = setInterval(load, 15_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (markets.length === 0 && opps.length === 0) return null;

  const items: { label: string; exchange: string; price: number | null; delta: number | null; edge: number | null }[] = [];

  for (const o of opps.slice(0, 10)) {
    items.push({
      label: o.title,
      exchange: o.exchange,
      price: o.yes_mid ? n(o.yes_mid) * 100 : null,
      delta: null,
      edge: n(o.edge),
    });
  }

  for (const m of markets) {
    if (items.some((it) => it.label === m.title)) continue;
    const mid = m.yes_bid != null && m.yes_ask != null
      ? (n(m.yes_bid) + n(m.yes_ask)) / 2
      : m.last_price != null ? n(m.last_price) : null;
    const prev = prevPrices.current[m.id];
    const delta = mid != null && prev != null && prev > 0 ? (mid - prev) * 100 : null;
    items.push({
      label: m.title,
      exchange: m.exchange,
      price: mid != null ? mid * 100 : null,
      delta,
      edge: null,
    });
    if (items.length >= 30) break;
  }

  const doubled = [...items, ...items];

  return (
    <div className="overflow-hidden bg-gray-900 border-b border-gray-800 py-1.5 px-2">
      <div className="flex animate-scroll gap-8 whitespace-nowrap">
        {doubled.map((it, i) => {
          const edgeVal = n(it.edge);
          const edgeColor = edgeVal > 0.05 ? "text-green-400" : edgeVal > 0 ? "text-yellow-400" : "text-gray-500";
          const deltaVal = n(it.delta);
          const deltaColor = deltaVal > 0 ? "text-green-400" : deltaVal < 0 ? "text-red-400" : "text-gray-600";
          return (
            <span key={i} className="text-xs flex gap-1.5 items-center shrink-0">
              <span className="text-gray-600 font-semibold">{it.exchange === "kalshi" ? "K" : "P"}</span>
              <span className="text-gray-300 max-w-[200px] truncate">{it.label}</span>
              {it.price != null && (
                <span className="text-gray-400 font-mono">{n(it.price).toFixed(0)}¢</span>
              )}
              {it.edge != null && edgeVal !== 0 && (
                <span className={`font-mono font-semibold ${edgeColor}`}>
                  {edgeVal > 0 ? "+" : ""}{(edgeVal * 100).toFixed(1)}%
                </span>
              )}
              {it.delta != null && deltaVal !== 0 && (
                <span className={`font-mono ${deltaColor}`}>
                  {deltaVal > 0 ? "+" : ""}{deltaVal.toFixed(1)}
                </span>
              )}
            </span>
          );
        })}
      </div>
    </div>
  );
}
