/**
 * The wire contract, mirroring `autotrader.dashboard.models`.
 *
 * Kept as hand-written types rather than generated ones on purpose: the API is
 * six routes and one payload, and a code-generation step would be more moving
 * parts than the thing it generates. The backend's own tests assert the field
 * names; these types assert the shape the page renders.
 *
 * Note what is *not* here. There is no command, no mutation, no request body,
 * and no action type - the API this describes is GET-only, and there is nothing
 * for the browser to send back.
 */

export type Tone = "NEUTRAL" | "POSITIVE" | "NEGATIVE" | "ATTENTION" | "MUTED";

export type SystemState = "HEALTHY" | "ATTENTION" | "PAUSED";

export type UnavailableReason =
  | "BROKER_NOT_CONFIGURED"
  | "BROKER_UNREADABLE"
  | "DATABASE_UNREADABLE"
  | "NOT_RECORDED";

export type Source = "BROKER" | "LOCAL" | "UNAVAILABLE";

export type AssetClass = "CRYPTO" | "EQUITY";

/** A USD figure, or an honest statement that it could not be read. */
export interface Amount {
  value: number | null;
  available: boolean;
  unavailable_reason: UnavailableReason | null;
}

export interface PrimaryMetrics {
  equity: Amount;
  cash: Amount;
  daily_pnl: Amount;
  daily_pnl_fraction: number | null;
  daily_pnl_baseline: Amount;
  daily_pnl_baseline_date: string | null;
  exposure: Amount;
  exposure_fraction: number | null;
}

export interface PositionRow {
  symbol: string;
  asset_class: AssetClass;
  quantity: string;
  price: number | null;
  market_value: number | null;
  average_entry_price: number | null;
  unrealized_pnl: number | null;
  unrealized_pnl_fraction: number | null;
  updated_at: string;
  source: Source;
}

export interface PositionsPanel {
  source: Source;
  as_of: string | null;
  rows: PositionRow[];
  flat_symbols: string[];
  unavailable_reason: UnavailableReason | null;
  note: string | null;
}

export interface OrderRow {
  client_order_id: string;
  created_at: string;
  symbol: string;
  asset_class: AssetClass;
  side: string;
  quantity: string;
  filled_quantity: string | null;
  average_fill_price: number | null;
  status: string;
  status_tone: Tone;
  status_source: Source;
  needs_attention: boolean;
  risk_reason_code: string;
  broker_order_id: string | null;
  submitted_at: string | null;
  filled_at: string | null;
}

export interface OrdersPanel {
  rows: OrderRow[];
  total: number;
  attention_count: number;
  unavailable_reason: UnavailableReason | null;
}

export interface HealthComponent {
  key: string;
  label: string;
  status: string;
  tone: Tone;
  detail: string | null;
}

export interface ReconciliationPanel {
  available: boolean;
  status: string | null;
  tone: Tone;
  safe_to_trade: boolean | null;
  started_at: string | null;
  completed_at: string | null;
  orders_checked: number | null;
  positions_checked: number | null;
  issues: number | null;
  repairs: number | null;
  unresolved: number | null;
  unavailable_reason: UnavailableReason | null;
}

export interface CheckpointRow {
  symbol: string;
  last_processed_bar: string;
  updated_at: string;
  age_seconds: number;
  stale: boolean;
}

export interface RuntimePanel {
  state: string;
  tone: Tone;
  detail: string | null;
  strategy_name: string | null;
  mode: string | null;
  started_at: string | null;
  ended_at: string | null;
  startup_safety: string;
  startup_safety_tone: Tone;
  startup_safety_detail: string | null;
  paper_execution_enabled: boolean;
  paper_execution_detail: string | null;
  last_cycle_at: string | null;
  next_cycle_at: string | null;
  checkpoints: CheckpointRow[];
  last_error: string | null;
  last_error_at: string | null;
}

export interface RiskLimit {
  key: string;
  label: string;
  limit_fraction: number;
  limit_value: Amount;
  used_value: Amount;
  used_fraction: number | null;
  utilization: number | null;
  breached: boolean;
  subject: string | null;
  detail: string | null;
}

export interface RiskPanel {
  limits: RiskLimit[];
  available: boolean;
  unavailable_reason: UnavailableReason | null;
}

export interface Overview {
  generated_at: string;
  environment: string;
  system_state: SystemState;
  system_state_tone: Tone;
  attention: string[];
  database: HealthComponent | null;
  broker: HealthComponent | null;
  metrics: PrimaryMetrics | null;
  positions: PositionsPanel | null;
  orders: OrdersPanel | null;
  health: HealthComponent[];
  reconciliation: ReconciliationPanel | null;
  runtime: RuntimePanel | null;
  risk: RiskPanel | null;
  notices: string[];
}
