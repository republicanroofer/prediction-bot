import { useEffect, useRef, useState } from "react";
import { EmptyState } from "../components/EmptyState";
import { api, type ActivityEvent } from "../lib/api";

const EVENT_META: Record<ActivityEvent["event_type"], { icon: string; label: string; color: string }> = {
  position_opened: { icon: "▶", label: "Opened",  color: "text-green-400" },
  position_closed: { icon: "■", label: "Closed",  color: "text-blue-400"  },
  trade_blocked:   { icon: "✗", label: "Blocked", color: "text-red-400"   },
  whale_queued:    { icon: "🐋", label: "Whale",  color: "text-yellow-400"},
};

export function Activity() {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<string>("all");
  const [hours, setHours] = useState(24);
  const prevLen = useRef(0);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const data = await api.activity(hours, 200);
        if (!cancelled) {
          setEvents(data);
          setLoading(false);
        }
      } catch {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    const id = setInterval(load, 5_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [hours]);

  const filtered = filter === "all" ? events : events.filter((e) => e.event_type === filter);

  const counts = {
    position_opened: events.filter((e) => e.event_type === "position_opened").length,
    position_closed: events.filter((e) => e.event_type === "position_closed").length,
    trade_blocked:   events.filter((e) => e.event_type === "trade_blocked").length,
    whale_queued:    events.filter((e) => e.event_type === "whale_queued").length,
  };

  return (
    <div className="space-y-4">
      {/* Filter pills + time range */}
      <div className="flex items-center gap-2 flex-wrap">
        <button
          onClick={() => setFilter("all")}
          className={pill(filter === "all")}
        >
          All ({events.length})
        </button>
        {(Object.keys(EVENT_META) as ActivityEvent["event_type"][]).map((t) => {
          const m = EVENT_META[t];
          return (
            <button key={t} onClick={() => setFilter(t)} className={pill(filter === t)}>
              <span className={m.color}>{m.icon}</span> {m.label} ({counts[t]})
            </button>
          );
        })}
        <select
          value={hours}
          onChange={(e) => setHours(Number(e.target.value))}
          className="ml-auto bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-300 focus:outline-none"
        >
          <option value={1}>Last 1h</option>
          <option value={6}>Last 6h</option>
          <option value={24}>Last 24h</option>
          <option value={72}>Last 3d</option>
        </select>
      </div>

      {/* Event feed */}
      {loading ? (
        <EmptyState message="Loading activity…" />
      ) : filtered.length === 0 ? (
        <EmptyState message="No activity in this time window" />
      ) : (
        <div className="space-y-1">
          {filtered.map((e, i) => (
            <EventRow key={`${e.ts}-${i}`} event={e} isNew={i < filtered.length - prevLen.current} />
          ))}
        </div>
      )}
    </div>
  );
}

function EventRow({ event: e }: { event: ActivityEvent; isNew: boolean }) {
  const meta = EVENT_META[e.event_type];
  return (
    <div className="flex items-start gap-3 bg-gray-900 border border-gray-800 rounded px-3 py-2 text-sm hover:border-gray-700 transition-colors">
      {/* Icon + type */}
      <span className={`font-mono shrink-0 w-4 text-center ${meta.color}`}>{meta.icon}</span>
      <span className={`shrink-0 text-xs font-semibold w-20 ${meta.color}`}>{meta.label}</span>

      {/* Exchange + side */}
      <span className="shrink-0 text-xs text-gray-500 w-20">
        {e.exchange?.toUpperCase()}
        {e.side && <span className={e.side === "yes" || e.side === "buy" ? " text-green-400" : " text-red-400"}> {e.side.toUpperCase()}</span>}
      </span>

      {/* Market title */}
      <span className="flex-1 text-gray-300 min-w-0 truncate" title={e.market_title ?? undefined}>
        {e.market_title ?? e.address ?? "—"}
      </span>

      {/* Amounts / reason */}
      <span className="shrink-0 text-xs text-right min-w-[80px]">
        {e.event_type === "position_opened" && e.size_usd != null && (
          <span className="text-gray-300">${e.size_usd.toFixed(2)}</span>
        )}
        {e.event_type === "position_closed" && e.pnl != null && (
          <span className={e.pnl >= 0 ? "text-green-400 font-semibold" : "text-red-400 font-semibold"}>
            {e.pnl >= 0 ? "+" : ""}${e.pnl.toFixed(2)}
          </span>
        )}
        {e.event_type === "trade_blocked" && e.gate && (
          <span className="text-red-400/80">{e.gate.replace("_", " ")}</span>
        )}
        {e.event_type === "whale_queued" && e.size_usd != null && (
          <span className="text-yellow-400">${e.size_usd.toFixed(0)}</span>
        )}
      </span>

      {/* Signal type */}
      {e.signal_type && (
        <span className="shrink-0 text-xs text-gray-600 w-20 text-right">{e.signal_type.replace("_", " ")}</span>
      )}

      {/* Timestamp */}
      <span className="shrink-0 text-xs text-gray-600 w-28 text-right font-mono">
        {fmtTime(e.ts)}
      </span>
    </div>
  );
}

function pill(active: boolean) {
  return `px-3 py-1 text-xs rounded-full border transition-colors ${
    active
      ? "border-brand-500 text-brand-500 bg-brand-500/10"
      : "border-gray-700 text-gray-400 hover:border-gray-500"
  }`;
}

function fmtTime(iso: string) {
  return new Date(iso).toLocaleString(undefined, {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}
