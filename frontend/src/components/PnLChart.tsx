import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { n, type DailyPnL } from "../lib/api";

type Props = { data: DailyPnL[] };

export function PnLChart({ data }: Props) {
  const sorted = [...data].sort((a, b) => a.date.localeCompare(b.date));

  // Cumulative realized P&L
  let cum = 0;
  const chartData = sorted.map((d) => {
    cum += n(d.realized_pnl);
    return {
      date: d.date.slice(5),   // MM-DD
      realized: d.realized_pnl,
      cumulative: parseFloat(cum.toFixed(2)),
    };
  });

  return (
    <ResponsiveContainer width="100%" height={200}>
      <AreaChart data={chartData}>
        <defs>
          <linearGradient id="pnlGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor="#0ea5e9" stopOpacity={0.3} />
            <stop offset="95%" stopColor="#0ea5e9" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis dataKey="date" stroke="#6b7280" tick={{ fontSize: 11 }} />
        <YAxis stroke="#6b7280" tick={{ fontSize: 11 }} tickFormatter={(v) => `$${v}`} />
        <Tooltip
          contentStyle={{ background: "#111827", border: "1px solid #374151" }}
          formatter={(v: number) => [`$${n(v).toFixed(2)}`, "Cumulative P&L"]}
        />
        <Area
          type="monotone"
          dataKey="cumulative"
          stroke="#0ea5e9"
          fill="url(#pnlGrad)"
          strokeWidth={2}
          dot={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
