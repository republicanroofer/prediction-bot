const BASE = "/api/v1";

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
};
