import type { Position } from "../lib/api";

type Props = { positions: Position[] };

const STRATEGY_COLORS: Record<string, string> = {
  news: "bg-blue-500",
  whale_mirror: "bg-purple-500",
  llm_directional: "bg-cyan-500",
  safe_compounder: "bg-green-500",
  arbitrage: "bg-yellow-500",
  order_book: "bg-orange-500",
  late_money: "bg-red-500",
  historical_pattern: "bg-teal-500",
  social_sentiment: "bg-pink-500",
  manual: "bg-gray-500",
};

export function StrategyBreakdown({ positions }: Props) {
  const counts: Record<string, number> = {};
  for (const p of positions) {
    const st = p.signal_type || "unknown";
    counts[st] = (counts[st] || 0) + 1;
  }

  const total = positions.length;
  if (total === 0) return null;

  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="text-gray-400 text-xs font-semibold uppercase mb-3">Strategy Breakdown</h3>
      <div className="space-y-2">
        {entries.map(([strategy, count]) => {
          const pct = ((count / total) * 100).toFixed(0);
          const color = STRATEGY_COLORS[strategy] ?? "bg-gray-600";
          return (
            <div key={strategy} className="flex items-center gap-3">
              <div className={`w-2.5 h-2.5 rounded-full shrink-0 ${color}`} />
              <span className="text-gray-300 text-sm flex-1 capitalize">{strategy.replace(/_/g, " ")}</span>
              <span className="text-gray-400 text-sm">{count}</span>
              <span className="text-gray-600 text-xs w-10 text-right">{pct}%</span>
            </div>
          );
        })}
      </div>
      {/* mini bar */}
      <div className="flex mt-3 h-2 rounded-full overflow-hidden">
        {entries.map(([strategy, count]) => {
          const color = STRATEGY_COLORS[strategy] ?? "bg-gray-600";
          return <div key={strategy} className={`${color}`} style={{ width: `${(count / total) * 100}%` }} />;
        })}
      </div>
    </div>
  );
}
