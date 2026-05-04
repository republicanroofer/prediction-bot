const BASE = "/api/v1";

export async function fetchJSON<T>(path: string): Promise<T> {
  const res = await fetch(BASE + path);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

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
};

export type WsSnapshot = {
  type: "snapshot";
  positions: Position[];
  orders: Order[];
  pnl: { realized: number; unrealized: number };
  ts: string;
};
