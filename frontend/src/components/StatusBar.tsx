import { n, type BotStatus } from "../lib/api";

type Props = {
  status: BotStatus | null;
  connected: boolean;
};

export function StatusBar({ status, connected }: Props) {
  return (
    <div className="flex items-center gap-6 px-4 py-2 bg-gray-900 border-b border-gray-800 text-sm">
      <span className="font-bold text-brand-500 text-base">Prediction Bot</span>

      <Dot active={connected} label={connected ? "live" : "disconnected"} />

      {status && (
        <>
          <Tag label="mode" value={status.mode.toUpperCase()} colored={status.mode === "live"} />
          <Tag label="exchange" value={status.exchange} />
          <Tag label="positions" value={String(status.open_positions)} />
          <Tag label="exposure" value={`$${n(status.total_exposure_usd).toFixed(0)}`} />
          <Tag label="kelly" value={`${(n(status.kelly_fraction) * 100).toFixed(0)}%`} />
          <PaperBalance
            balance={n(status.paper_balance)}
            returnPct={n(status.paper_return_pct)}
          />
        </>
      )}
    </div>
  );
}

function Dot({ active, label }: { active: boolean; label: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={`inline-block w-2 h-2 rounded-full ${active ? "bg-green-400" : "bg-red-500"}`} />
      <span className="text-gray-400">{label}</span>
    </span>
  );
}

function Tag({ label, value, colored }: { label: string; value: string; colored?: boolean }) {
  return (
    <span className="text-gray-500">
      {label}:{" "}
      <span className={colored ? "text-yellow-400 font-semibold" : "text-gray-200"}>{value}</span>
    </span>
  );
}

function PaperBalance({ balance, returnPct }: { balance: number; returnPct: number }) {
  const positive = returnPct > 0;
  const negative = returnPct < 0;
  const sign = positive ? "+" : "";
  const color = positive ? "text-green-400" : negative ? "text-red-400" : "text-gray-200";
  return (
    <span className="text-gray-500 border-l border-gray-700 pl-4 ml-2">
      paper:{" "}
      <span className="text-gray-200">${balance.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
      {" "}
      <span className={`${color} font-semibold`}>({sign}{returnPct.toFixed(2)}%)</span>
    </span>
  );
}
