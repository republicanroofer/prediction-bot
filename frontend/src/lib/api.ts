const BASE = "/api/v1";

export function n(v: unknown, fallback = 0): number {
  if (v == null) return fallback;
  const x = typeof v === "number" ? v : Number(v);
  return Number.isFinite(x) ? x : fallback;
}

export async function fetchJSON<T>(path: string): Promise<T> {
  const res = await fetch(BASE + path);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

// ── Core types ────────────────────────────────────────────────────────────────

export type Position = {
  id: string;
  exchange: string;
  side: string;
  status: string;
  avg_entry_price: number;
  current_contracts: number;
  cost_basis_usd: number;
  unrealized_pnl?: number;
  realized_pnl?: number;
  signal_type: string;
  opened_at: string;
  close_reason?: string;
};

export type Order = {
  id: string;
  exchange: string;
  side: string;
  status: string;
  is_opening: boolean;
  requested_price: number;
  requested_contracts: number;
  created_at: string;
};

export type DailyPnL = {
  date: string;
  exchange: string;
  mode: string;
  realized_pnl: number;
  unrealized_pnl: number;
  num_positions: number;
  num_wins: number;
  num_losses: number;
};

export type BotStatus = {
  mode: string;
  exchange: string;
  open_positions: number;
  pending_orders: number;
  total_exposure_usd: number;
  stop_loss_pct: number;
  take_profit_pct: number;
  max_position_pct: number;
  kelly_fraction: number;
  paper_starting_balance: number;
  paper_balance: number;
  paper_return_pct: number;
};

export type Market = {
  id: string;
  exchange: string;
  external_id: string;
  title: string;
  category?: string;
  yes_bid?: number;
  yes_ask?: number;
  last_price?: number;
  volume_24h_usd?: number;
  volume_total_usd?: number;
  close_time?: string;
  is_active: boolean;
  is_resolved: boolean;
};

export type WhaleScore = {
  id: string;
  address: string;
  display_name?: string;
  composite_score?: number;
  win_rate?: number;
  big_win_rate?: number;
  median_gain_pct?: number;
  median_loss_pct?: number;
  markets_traded?: number;
  total_volume_usd?: number;
  total_pnl_usd?: number;
  is_active: boolean;
  last_trade_at?: string;
  scored_at: string;
};

export type WhaleTrade = {
  id: string;
  tx_hash: string;
  block_timestamp: string;
  maker_address: string;
  maker_direction: string;
  price: number;
  usd_amount: number;
  mirrored: boolean;
  mirror_queued_at?: string;
  market_title?: string;
  whale_score?: number;
};

export type NewsSignal = {
  id: string;
  market_id?: string;
  external_market_id?: string;
  source: string;
  headline: string;
  url?: string;
  published_at?: string;
  sentiment_score?: number;
  relevance_score?: number;
  direction?: string;
  keywords?: string[];
  created_at: string;
};

export type ActivityEvent = {
  event_type: "position_opened" | "position_closed" | "trade_blocked" | "whale_queued";
  ts: string;
  exchange: string;
  side?: string;
  signal_type?: string;
  market_title?: string;
  size_usd?: number;
  pnl?: number;
  reason?: string;
  gate?: string;
  address?: string;
};

export type WsSnapshot = {
  type: "snapshot";
  positions: Position[];
  orders: Order[];
  pnl: { realized: number; unrealized: number };
  ts: string;
};

export type EvalDecision = {
  id: string;
  market_id?: string;
  external_market_id?: string;
  market_title?: string;
  exchange: string;
  signal_type?: string;
  side?: string;
  entry_price?: number;
  edge?: number;
  confidence?: number;
  kelly_size_usd?: number;
  decision: string;
  reason: string;
  created_at: string;
};

export type DecisionSummary = {
  total: number;
  buckets: { reason: string; exchange: string; decision: string; count: number }[];
};

export type CategoryExposure = {
  category: string;
  exchange: string;
  positions_count: number;
  exposure_usd: number;
  unrealized_pnl: number;
};

export type CategoryPosition = {
  id: string;
  exchange: string;
  side: string;
  signal_type: string;
  avg_entry_price: number;
  cost_basis_usd: number;
  unrealized_pnl: number;
  opened_at: string;
  market_title: string;
};

export type FunnelMetrics = {
  markets_scanned: number;
  signals_generated: number;
  trades_blocked: number;
  trades_executed: number;
  period_hours: number;
};

export type Opportunity = {
  market_id: string;
  external_id: string;
  title: string;
  exchange: string;
  category?: string;
  yes_mid?: number;
  confidence: number;
  edge: number;
  signal_type?: string;
  signal_headline?: string;
  relevance: number;
  sentiment?: number;
  volume_24h?: number;
  days_to_close?: number;
};

export type ArbitrageOpp = {
  title: string;
  category?: string;
  kalshi_mid: number;
  poly_mid: number;
  gap_pct: number;
  cheap_exchange: string;
  kalshi_vol: number;
  poly_vol: number;
  close_time?: string;
};

// ── Fetch helpers ─────────────────────────────────────────────────────────────

export const api = {
  status: () => fetchJSON<BotStatus>("/control/status"),
  pnlDaily: (days = 30) => fetchJSON<DailyPnL[]>(`/pnl/daily?days=${days}`),
  markets: (limit = 200) => fetchJSON<Market[]>(`/markets/?limit=${limit}`),
  whaleScores: (limit = 50) => fetchJSON<WhaleScore[]>(`/whales/scores?limit=${limit}&min_score=40`),
  whaleTrades: (limit = 50) => fetchJSON<WhaleTrade[]>(`/whales/trades?limit=${limit}`),
  newsSignals: (hours = 24, limit = 50) => fetchJSON<NewsSignal[]>(`/signals/news?hours=${hours}&limit=${limit}`),
  whaleSignals: (hours = 24, limit = 50) => fetchJSON<WhaleTrade[]>(`/signals/whale?hours=${hours}&limit=${limit}`),
  activity: (hours = 24, limit = 100) => fetchJSON<ActivityEvent[]>(`/activity/?hours=${hours}&limit=${limit}`),
  exposure: () => fetchJSON<CategoryExposure[]>("/analytics/exposure"),
  arbitrage: () => fetchJSON<ArbitrageOpp[]>("/analytics/arbitrage"),
  positionsByCategory: (category: string) =>
    fetchJSON<CategoryPosition[]>(`/analytics/positions-by-category?category=${encodeURIComponent(category)}`),
  funnel: (hours = 24) => fetchJSON<FunnelMetrics>(`/analytics/funnel?hours=${hours}`),
  opportunities: (limit = 20) => fetchJSON<Opportunity[]>(`/analytics/opportunities?limit=${limit}`),
  decisionsSummary: () => fetchJSON<DecisionSummary>("/analytics/decisions/summary"),
  decisionsLive: (params: {
    limit?: number;
    exchange?: string;
    decision?: string;
    signal_type?: string;
    reason?: string;
  } = {}) => {
    const q = new URLSearchParams();
    if (params.limit) q.set("limit", String(params.limit));
    if (params.exchange) q.set("exchange", params.exchange);
    if (params.decision) q.set("decision", params.decision);
    if (params.signal_type) q.set("signal_type", params.signal_type);
    if (params.reason) q.set("reason", params.reason);
    const qs = q.toString();
    return fetchJSON<EvalDecision[]>(`/analytics/decisions/live${qs ? "?" + qs : ""}`);
  },
  orders: (positionId?: string, limit = 100) => {
    const q = new URLSearchParams({ limit: String(limit) });
    if (positionId) q.set("position_id", positionId);
    return fetchJSON<Order[]>(`/orders/?${q}`);
  },
};
