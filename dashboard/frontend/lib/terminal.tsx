"use client";

/** Shared polling state for the real-money terminal workspaces. GET only. */

import { createContext, useContext, useMemo, type ReactNode } from "react";

import { usePoll, type PollState } from "./api";
import type { LiveAccountingSummary } from "./live-contract";
import type { LiveSafetyPanel } from "./live-safety";

export const LIVE_TERMINAL_ENDPOINT = "/api/live-safety/terminal";
export const LIVE_HISTORY_ENDPOINT = "/api/live-accounting/history";
export const TERMINAL_POLL_MS = 5_000;
export const HISTORY_POLL_MS = 30_000;

export interface TerminalPosition {
  symbol: string;
  quantity: string | null;
  average_cost: string | null;
  price: string | null;
  market_value: string | null;
  actual_weight: string | null;
  target_weight: string | null;
  drift: string | null;
  unrealized_pnl: string | null;
  unrealized_pnl_fraction: string | null;
  day_pnl: string | null;
  day_change_fraction: string | null;
  stance: string | null;
  last_decision_at: string | null;
  last_fill_at: string | null;
}

export interface TerminalOrder {
  client_order_id: string;
  broker_order_id: string;
  symbol: string;
  side: string;
  quantity: string | null;
  filled_quantity: string | null;
  expected_price: string | null;
  fill_price: string | null;
  status: string;
  intent_status: string | null;
  risk_reason_code: string | null;
  intent_created_at: string | null;
  submitted_at: string | null;
  filled_at: string | null;
  updated_at: string | null;
}

export interface TerminalTarget {
  symbol: string;
  side: string;
  target_weight: string | null;
  target_notional: string | null;
  target_quantity: string | null;
  approved_quantity: string | null;
  reference_price: string | null;
  risk_reason_code: string | null;
  bar_timestamp: string | null;
  decided_at: string | null;
  rollout_stage: string | null;
  sizing_policy: string | null;
  sizing_config_hash: string | null;
  client_order_id: string | null;
}

export interface TerminalRegime {
  state: string;
  participate: boolean;
  session_date: string;
  reference_symbol: string;
  reference_close: string | null;
  sma200: string | null;
  drawdown_from_peak: string | null;
  sessions_observed: number;
  sma_sessions: number;
  computed_at: string | null;
}

export interface TerminalStrategy {
  name: string;
  universe: string[];
  regime: TerminalRegime | null;
  last_run: {
    status: string;
    mode: string;
    started_at: string | null;
    ended_at: string | null;
  } | null;
  next_cycle_at: string | null;
  rollout_stage: string | null;
  sizing_policy: string | null;
}

export interface ExecutionMetrics {
  order_count: number;
  fill_count: number;
  reject_count: number;
  partial_fill_count: number;
  unknown_count: number;
  fill_rate: string | null;
  reject_rate: string | null;
  partial_fill_rate: string | null;
  average_slippage_bps: string | null;
  median_slippage_bps: string | null;
  p95_slippage_bps: string | null;
  slippage_sample_size: number;
  average_fill_latency_ms: string | null;
  latency_sample_size: number;
  duplicate_client_order_ids: number;
  at_most_once_status: string;
}

export interface TerminalEvent {
  timestamp: string;
  category: string;
  type: string;
  symbol: string | null;
  status: string | null;
  detail: string | null;
}

export interface LiveTerminalSnapshot {
  generated_at: string;
  environment: "LIVE";
  read_only: true;
  operational_status: string;
  positions: {
    status: string;
    as_of: string | null;
    largest_position: TerminalPosition | null;
    rows: TerminalPosition[];
  };
  strategy: TerminalStrategy | null;
  targets: TerminalTarget[];
  orders: TerminalOrder[];
  execution: ExecutionMetrics;
  events: TerminalEvent[];
}

export interface HistoryPoint {
  checkpoint_id: number;
  taken_at: string;
  utc_date: string;
  kind: string;
  broker_equity: string | null;
  broker_cash: string | null;
  unrealized_pnl: string | null;
  position_count: number | null;
  flow_adjusted_equity: string | null;
  trading_pnl: string | null;
  time_weighted_return: string | null;
  adjusted_equity_hwm: string | null;
  drawdown: string | null;
}

export interface CapitalFlow {
  settle_at: string;
  activity_type: string;
  classification: string;
  confirmation: string;
  amount: string | null;
  currency: string;
  relation: string;
}

export interface LiveHistory {
  generated_at: string;
  environment: "LIVE";
  read_only: true;
  status: string;
  sample_size: number;
  today_trading_pnl: string | null;
  points: HistoryPoint[];
  flows: CapitalFlow[];
}

export interface TerminalData {
  safety: PollState<LiveSafetyPanel>;
  accounting: PollState<LiveAccountingSummary>;
  terminal: PollState<LiveTerminalSnapshot>;
  history: PollState<LiveHistory>;
  connected: boolean;
  lastGoodAt: string | null;
}

const EMPTY = {
  data: null,
  loading: true,
  connected: false,
  lastSuccessAt: null,
  refresh: () => {},
};

const FALLBACK: TerminalData = {
  safety: EMPTY as PollState<LiveSafetyPanel>,
  accounting: EMPTY as PollState<LiveAccountingSummary>,
  terminal: EMPTY as PollState<LiveTerminalSnapshot>,
  history: EMPTY as PollState<LiveHistory>,
  connected: false,
  lastGoodAt: null,
};

const TerminalContext = createContext<TerminalData>(FALLBACK);

export function TerminalProvider({ children }: { children: ReactNode }) {
  const safety = usePoll<LiveSafetyPanel>("/api/live-safety/summary", TERMINAL_POLL_MS);
  const accounting = usePoll<LiveAccountingSummary>(
    "/api/live-accounting/summary",
    TERMINAL_POLL_MS * 2,
  );
  const terminal = usePoll<LiveTerminalSnapshot>(LIVE_TERMINAL_ENDPOINT, TERMINAL_POLL_MS);
  const history = usePoll<LiveHistory>(LIVE_HISTORY_ENDPOINT, HISTORY_POLL_MS);
  const value = useMemo<TerminalData>(() => {
    const stamps = [
      safety.lastSuccessAt,
      accounting.lastSuccessAt,
      terminal.lastSuccessAt,
      history.lastSuccessAt,
    ].filter((stamp): stamp is string => Boolean(stamp));
    return {
      safety,
      accounting,
      terminal,
      history,
      connected: safety.connected && accounting.connected,
      lastGoodAt: stamps.sort().at(-1) ?? null,
    };
  }, [safety, accounting, terminal, history]);
  return <TerminalContext.Provider value={value}>{children}</TerminalContext.Provider>;
}

export function useTerminal(): TerminalData {
  return useContext(TerminalContext);
}
