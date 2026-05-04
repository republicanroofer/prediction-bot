import { useEffect, useState } from "react";
import { Nav } from "./components/Nav";
import { StatusBar } from "./components/StatusBar";
import { Activity } from "./pages/Activity";
import { Markets } from "./pages/Markets";
import { Overview } from "./pages/Overview";
import { Signals } from "./pages/Signals";
import { Whales } from "./pages/Whales";
import { api, type BotStatus, type DailyPnL } from "./lib/api";
import { useWebSocket } from "./lib/useWebSocket";

export default function App() {
  const { snapshot, connected } = useWebSocket();
  const [tab, setTab] = useState("overview");
  const [status, setStatus] = useState<BotStatus | null>(null);
  const [pnlHistory, setPnlHistory] = useState<DailyPnL[]>([]);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [s, p] = await Promise.all([api.status(), api.pnlDaily(30)]);
        if (!cancelled) { setStatus(s); setPnlHistory(p); }
      } catch { /* silent */ }
    }
    load();
    const id = setInterval(load, 30_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  return (
    <div className="min-h-screen flex flex-col">
      <StatusBar status={status} connected={connected} />
      <Nav active={tab} onChange={setTab} />

      <main className="flex-1 p-6 max-w-7xl mx-auto w-full">
        {tab === "overview"  && <Overview snapshot={snapshot} pnlHistory={pnlHistory} />}
        {tab === "signals"   && <Signals />}
        {tab === "markets"   && <Markets />}
        {tab === "whales"    && <Whales />}
        {tab === "activity"  && <Activity />}
      </main>
    </div>
  );
}
