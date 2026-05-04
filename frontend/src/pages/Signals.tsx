import { useEffect, useState } from "react";
import { EmptyState } from "../components/EmptyState";
import { api, type NewsSignal, type WhaleTrade } from "../lib/api";

export function Signals() {
  const [tab, setTab] = useState<"news" | "whale">("news");
  const [news, setNews] = useState<NewsSignal[]>([]);
  const [whale, setWhale] = useState<WhaleTrade[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      try {
        const [n, w] = await Promise.all([api.newsSignals(24, 50), api.whaleSignals(24, 50)]);
        if (!cancelled) { setNews(n); setWhale(w); }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    const id = setInterval(load, 15_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  return (
    <div className="space-y-4">
      <div className="flex gap-2">
        {(["news", "whale"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-1.5 text-sm rounded-full border transition-colors ${
              tab === t
                ? "border-brand-500 text-brand-500 bg-brand-500/10"
                : "border-gray-700 text-gray-400 hover:border-gray-500"
            }`}
          >
            {t === "news" ? `News (${news.length})` : `Whale Mirror Queue (${whale.length})`}
          </button>
        ))}
      </div>

      {loading ? (
        <EmptyState message="Loading signals…" />
      ) : tab === "news" ? (
        <NewsPanel signals={news} />
      ) : (
        <WhaleSignalPanel trades={whale} />
      )}
    </div>
  );
}

function NewsPanel({ signals }: { signals: NewsSignal[] }) {
  if (!signals.length) return <EmptyState message="No news signals in the last 24h" />;
  return (
    <div className="space-y-2">
      {signals.map((s) => {
        const sent = Number(s.sentiment_score ?? 0);
        const rel = Number(s.relevance_score ?? 0);
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
              <td className="py-1.5 pr-4 font-mono text-xs text-gray-300">{t.maker_address.slice(0, 8)}…</td>
              <td className="py-1.5 pr-4 text-gray-300 max-w-[200px] truncate">{t.market_title ?? "—"}</td>
              <td className="py-1.5 pr-4">
                <span className={t.maker_direction === "buy" ? "text-green-400" : "text-red-400"}>
                  {t.maker_direction.toUpperCase()}
                </span>
              </td>
              <td className="py-1.5 pr-4 font-mono">${Number(t.usd_amount).toFixed(2)}</td>
              <td className="py-1.5 pr-4 font-mono">{Number(t.price).toFixed(3)}</td>
              <td className="py-1.5">
                {t.whale_score != null ? (
                  <ScorePill score={Number(t.whale_score)} />
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
  const color = score >= 80 ? "bg-green-900/60 text-green-300" : score >= 60 ? "bg-yellow-900/60 text-yellow-300" : "bg-gray-800 text-gray-400";
  return <span className={`px-2 py-0.5 rounded text-xs font-mono font-semibold ${color}`}>{score.toFixed(0)}</span>;
}

function fmtDate(iso: string) {
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
