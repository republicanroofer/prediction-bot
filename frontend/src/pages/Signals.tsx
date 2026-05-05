import { useEffect, useState } from "react";
import { EmptyState } from "../components/EmptyState";
import { api, n, type NewsSignal, type WhaleTrade, type EvalDecision } from "../lib/api";

type TabId = "news" | "whale" | "order_book" | "late_money" | "social" | "historical" | "arbitrage";

const TAB_LABELS: Record<TabId, string> = {
  news: "News",
  whale: "Whale Mirror",
  order_book: "Order Book",
  late_money: "Late Money",
  social: "Social Sentiment",
  historical: "Hist. Pattern",
  arbitrage: "Arbitrage",
};

export function Signals() {
  const [tab, setTab] = useState<TabId>("news");
  const [news, setNews] = useState<NewsSignal[]>([]);
  const [whale, setWhale] = useState<WhaleTrade[]>([]);
  const [firings, setFirings] = useState<EvalDecision[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [ns, wh, fi] = await Promise.all([
          api.newsSignals(24, 50),
          api.whaleSignals(24, 50),
          api.signalFirings(),
        ]);
        if (!cancelled) {
          setNews(ns);
          setWhale(wh);
          setFirings(fi);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    const id = setInterval(load, 15_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const bySig = (type: string) => firings.filter((f) => f.signal_type === type);
  const counts: Record<TabId, number> = {
    news: news.length,
    whale: whale.length,
    order_book: bySig("order_book").length,
    late_money: bySig("late_money").length,
    social: bySig("social_sentiment").length,
    historical: bySig("historical_pattern").length,
    arbitrage: bySig("arbitrage").length,
  };

  return (
    <div className="space-y-4">
      <div className="flex gap-2 flex-wrap">
        {(Object.keys(TAB_LABELS) as TabId[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-3 py-1.5 text-xs rounded-full border transition-colors ${
              tab === t
                ? "border-brand-500 text-brand-500 bg-brand-500/10"
                : "border-gray-700 text-gray-400 hover:border-gray-500"
            }`}
          >
            {TAB_LABELS[t]} ({counts[t]})
          </button>
        ))}
      </div>

      {loading ? (
        <EmptyState message="Loading signals..." />
      ) : tab === "news" ? (
        <NewsPanel signals={news} />
      ) : tab === "whale" ? (
        <WhaleSignalPanel trades={whale} />
      ) : (
        <FiringsPanel
          decisions={
            tab === "order_book" ? bySig("order_book")
            : tab === "late_money" ? bySig("late_money")
            : tab === "social" ? bySig("social_sentiment")
            : tab === "historical" ? bySig("historical_pattern")
            : bySig("arbitrage")
          }
          label={TAB_LABELS[tab]}
        />
      )}
    </div>
  );
}

function FiringsPanel({ decisions, label }: { decisions: EvalDecision[]; label: string }) {
  if (decisions.length === 0) {
    return <EmptyState message={`No ${label} signals fired in the last 24h`} />;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-gray-400 border-b border-gray-800 text-left text-xs">
            <th className="pb-2 pr-3">Time</th>
            <th className="pb-2 pr-3">Exchange</th>
            <th className="pb-2 pr-3">Market</th>
            <th className="pb-2 pr-3">Side</th>
            <th className="pb-2 pr-3 text-right">Price</th>
            <th className="pb-2 pr-3 text-right">Edge</th>
            <th className="pb-2 pr-3 text-right">Size</th>
            <th className="pb-2">Reason</th>
          </tr>
        </thead>
        <tbody>
          {decisions.map((d) => {
            const edge = n(d.edge);
            const edgeColor = edge > 0.1 ? "text-green-400" : edge > 0 ? "text-yellow-400" : "text-gray-500";
            return (
              <tr key={d.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                <td className="py-1.5 pr-3 text-gray-500 text-xs font-mono">
                  {new Date(d.created_at).toLocaleTimeString()}
                </td>
                <td className="py-1.5 pr-3">
                  <span className={`text-xs px-1.5 py-0.5 rounded font-semibold ${
                    d.exchange === "kalshi" ? "bg-blue-900/50 text-blue-300" : "bg-purple-900/50 text-purple-300"
                  }`}>
                    {d.exchange === "kalshi" ? "K" : "P"}
                  </span>
                </td>
                <td className="py-1.5 pr-3 text-gray-300 max-w-[200px] truncate" title={d.market_title ?? ""}>
                  {d.market_title ?? "—"}
                </td>
                <td className="py-1.5 pr-3">
                  {d.side ? (
                    <span className={d.side === "yes" ? "text-green-400" : "text-red-400"}>
                      {d.side.toUpperCase()}
                    </span>
                  ) : "—"}
                </td>
                <td className="py-1.5 pr-3 text-right font-mono text-gray-400">
                  {d.entry_price != null ? `${(n(d.entry_price) * 100).toFixed(0)}¢` : "—"}
                </td>
                <td className={`py-1.5 pr-3 text-right font-mono font-semibold ${edgeColor}`}>
                  {d.edge != null ? `+${(edge * 100).toFixed(1)}%` : "—"}
                </td>
                <td className="py-1.5 pr-3 text-right font-mono text-gray-400">
                  {d.kelly_size_usd != null ? `$${n(d.kelly_size_usd).toFixed(0)}` : "—"}
                </td>
                <td className="py-1.5 text-gray-500 text-xs max-w-[200px] truncate" title={d.reason}>
                  {d.reason}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function NewsPanel({ signals }: { signals: NewsSignal[] }) {
  if (!signals.length) return <EmptyState message="No news signals in the last 24h" />;
  return (
    <div className="space-y-2">
      {signals.map((s) => {
        const sent = n(s.sentiment_score);
        const rel = n(s.relevance_score);
        const sentColor = sent > 0.1 ? "text-green-400" : sent < -0.1 ? "text-red-400" : "text-gray-400";
        const dirBg = s.direction === "yes" ? "bg-green-900/50 text-green-300" : s.direction === "no" ? "bg-red-900/50 text-red-300" : "bg-gray-800 text-gray-400";
        return (
          <div key={s.id} className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="flex items-start justify-between gap-3">
              <div className="flex-1 min-w-0">
                <p className="text-gray-100 text-sm leading-snug mb-1">
                  {s.url ? (
                    <a href={s.url} target="_blank" rel="noreferrer" className="hover:text-brand-500 transition-colors">
                      {s.headline}
                    </a>
                  ) : s.headline}
                </p>
                {s.keywords && s.keywords.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-1.5">
                    {s.keywords.slice(0, 6).map((k) => (
                      <span key={k} className="text-xs bg-gray-800 text-gray-400 px-1.5 py-0.5 rounded">{k}</span>
                    ))}
                  </div>
                )}
              </div>
              <div className="flex flex-col items-end gap-1 shrink-0 text-xs">
                {s.direction && (
                  <span className={`px-2 py-0.5 rounded-full font-semibold ${dirBg}`}>
                    {s.direction.toUpperCase()}
                  </span>
                )}
                <span className={`font-mono font-semibold ${sentColor}`}>
                  {sent >= 0 ? "+" : ""}{sent.toFixed(2)} sent
                </span>
                <span className="text-gray-500">{(rel * 100).toFixed(0)}% rel</span>
              </div>
            </div>
            <div className="flex items-center gap-3 mt-2 text-xs text-gray-500">
              <span className="uppercase tracking-wide">{s.source}</span>
              {s.published_at && <span>{fmtDate(s.published_at)}</span>}
              <SentimentBar value={sent} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function WhaleSignalPanel({ trades }: { trades: WhaleTrade[] }) {
  if (!trades.length) return <EmptyState message="No whale mirror signals in the last 24h" />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-gray-400 border-b border-gray-800 text-left">
            <th className="pb-2 pr-4">Time</th>
            <th className="pb-2 pr-4">Address</th>
            <th className="pb-2 pr-4">Market</th>
            <th className="pb-2 pr-4">Direction</th>
            <th className="pb-2 pr-4">Size</th>
            <th className="pb-2 pr-4">Price</th>
            <th className="pb-2">Score</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => (
            <tr key={t.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
              <td className="py-1.5 pr-4 text-gray-500 text-xs">{fmtDate(t.mirror_queued_at ?? t.block_timestamp)}</td>
              <td className="py-1.5 pr-4 font-mono text-xs text-gray-300">{t.maker_address.slice(0, 8)}...</td>
              <td className="py-1.5 pr-4 text-gray-300 max-w-[200px] truncate">{t.market_title ?? "—"}</td>
              <td className="py-1.5 pr-4">
                <span className={t.maker_direction === "buy" ? "text-green-400" : "text-red-400"}>
                  {t.maker_direction.toUpperCase()}
                </span>
              </td>
              <td className="py-1.5 pr-4 font-mono">${n(t.usd_amount).toFixed(2)}</td>
              <td className="py-1.5 pr-4 font-mono">{n(t.price).toFixed(3)}</td>
              <td className="py-1.5">
                {t.whale_score != null ? (
                  <ScorePill score={n(t.whale_score)} />
                ) : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SentimentBar({ value }: { value: number }) {
  const clamped = Math.max(-1, Math.min(1, value));
  const pct = Math.abs(clamped) * 50;
  const color = clamped > 0 ? "bg-green-500" : "bg-red-500";
  const left = clamped > 0 ? "50%" : `${50 - pct}%`;
  return (
    <div className="relative w-24 h-1.5 bg-gray-700 rounded-full overflow-hidden">
      <div className="absolute inset-y-0 w-px bg-gray-500" style={{ left: "50%" }} />
      <div className={`absolute inset-y-0 ${color}`} style={{ left, width: `${pct}%` }} />
    </div>
  );
}

function ScorePill({ score }: { score: number }) {
  const s = n(score);
  const color = s >= 80 ? "bg-green-900/60 text-green-300" : s >= 60 ? "bg-yellow-900/60 text-yellow-300" : "bg-gray-800 text-gray-400";
  return <span className={`px-2 py-0.5 rounded text-xs font-mono font-semibold ${color}`}>{s.toFixed(0)}</span>;
}

function fmtDate(iso: string) {
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
