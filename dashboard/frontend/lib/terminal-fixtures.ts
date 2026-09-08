/** Deterministic Terminal V4 states for tests and visual QA only. */

import { FIXTURE_A_BASELINE, FIXTURE_E_DRAWDOWN, FIXTURE_F_STALE, FIXTURE_G_UNKNOWN_FLOW, FIXTURE_H_ARMED, FIXTURE_K_WITHDRAWAL, type LiveFixture } from "./live-fixtures.ts";
import type { LiveHistory, LiveTerminalSnapshot } from "./terminal.tsx";

const AT = "2026-09-08T14:05:00+00:00";

export const TERMINAL_POSITION_HEAVY: LiveTerminalSnapshot = {
  generated_at: AT,
  environment: "LIVE",
  read_only: true,
  operational_status: "CLEAN",
  positions: {
    status: "OK",
    as_of: AT,
    largest_position: null,
    rows: ([
      ["AAPL", "0.025", "223.00", "224.20", "5.605", "0.1121", "0.11", "0.0021", "0.03", "0.00538", "0.01", "0.00179", "LONG"],
      ["MSFT", "0.011", "505.00", "503.20", "5.5352", "0.110704", "0.11", "0.000704", "-0.02", "-0.00356", "-0.01", "-0.0018", "LONG"],
      ["NVDA", "0.031", "178.00", "181.10", "5.6141", "0.112282", "0.11", "0.002282", "0.09", "0.01741", "0.04", "0.00717", "LONG"],
      ["AMZN", "0.024", "234.00", "232.80", "5.5872", "0.111744", "0.11", "0.001744", "-0.03", "-0.00513", "-0.02", "-0.00358", "LONG"],
      ["META", "0.007", "805.00", "801.00", "5.607", "0.11214", "0.11", "0.00214", "-0.03", "-0.00497", "0.01", "0.00178", "LONG"],
    ] as const).map(([symbol, quantity, average_cost, price, market_value, actual_weight, target_weight, drift, unrealized_pnl, unrealized_pnl_fraction, day_pnl, day_change_fraction, stance]) => ({ symbol, quantity, average_cost, price, market_value, actual_weight, target_weight, drift, unrealized_pnl, unrealized_pnl_fraction, day_pnl, day_change_fraction, stance, last_decision_at: "2026-09-08T13:45:01+00:00", last_fill_at: "2026-09-08T13:45:04+00:00" })),
  },
  strategy: {
    name: "EDA-1",
    universe: ["AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "QQQ", "SPY", "TSLA", "VTI"],
    regime: { state: "PARTICIPATE", participate: true, session_date: "2026-09-08", reference_symbol: "SPY", reference_close: "652.40", sma200: "598.20", drawdown_from_peak: "-0.012", sessions_observed: 420, sma_sessions: 200, computed_at: "2026-09-08T13:45:00+00:00" },
    last_run: { status: "COMPLETED", mode: "LIVE", started_at: "2026-09-08T13:45:00+00:00", ended_at: "2026-09-08T13:45:05+00:00" },
    next_cycle_at: "2026-09-08T14:00:00+00:00",
    rollout_stage: "LIVE_VALIDATION",
    sizing_policy: "LIVE_VALIDATION_100",
  },
  targets: ["AAPL", "MSFT", "NVDA", "AMZN", "META"].map((symbol) => ({ symbol, side: "BUY", target_weight: "0.11", target_notional: "5.50", target_quantity: "0.025", approved_quantity: "0.025", reference_price: "220.00", risk_reason_code: "PASS", bar_timestamp: "2026-09-08T13:45:00+00:00", decided_at: "2026-09-08T13:45:01+00:00", rollout_stage: "LIVE_VALIDATION", sizing_policy: "LIVE_VALIDATION_100", sizing_config_hash: "ecf003eb9a86", client_order_id: `live-${symbol.toLowerCase()}-001` })),
  orders: [
    { client_order_id: "live-nvda-001", broker_order_id: "broker-nvda-001", symbol: "NVDA", side: "BUY", quantity: "0.031", filled_quantity: "0.031", expected_price: "181.00", fill_price: "181.10", status: "FILLED", intent_status: "LINKED", risk_reason_code: "PASS", intent_created_at: "2026-09-08T13:45:02+00:00", submitted_at: "2026-09-08T13:45:03+00:00", filled_at: "2026-09-08T13:45:04+00:00", updated_at: "2026-09-08T13:45:04+00:00" },
    { client_order_id: "live-msft-002", broker_order_id: "broker-msft-002", symbol: "MSFT", side: "BUY", quantity: "0.011", filled_quantity: "0.005", expected_price: "503.00", fill_price: "503.10", status: "PARTIALLY_FILLED", intent_status: "LINKED", risk_reason_code: "PASS", intent_created_at: "2026-09-08T13:46:02+00:00", submitted_at: "2026-09-08T13:46:03+00:00", filled_at: null, updated_at: "2026-09-08T13:46:05+00:00" },
    { client_order_id: "live-tsla-003", broker_order_id: "broker-tsla-003", symbol: "TSLA", side: "BUY", quantity: "0.012", filled_quantity: "0", expected_price: "448.00", fill_price: null, status: "REJECTED", intent_status: "LINKED", risk_reason_code: "BROKER_REJECTED", intent_created_at: "2026-09-08T13:47:02+00:00", submitted_at: "2026-09-08T13:47:03+00:00", filled_at: null, updated_at: "2026-09-08T13:47:05+00:00" },
  ],
  execution: { order_count: 3, fill_count: 1, reject_count: 1, partial_fill_count: 1, unknown_count: 0, fill_rate: "0.333333", reject_rate: "0.333333", partial_fill_rate: "0.333333", average_slippage_bps: "5.524", median_slippage_bps: "5.524", p95_slippage_bps: "5.524", slippage_sample_size: 1, average_fill_latency_ms: "1000", latency_sample_size: 1, duplicate_client_order_ids: 0, at_most_once_status: "PASS" },
  events: [
    { timestamp: "2026-09-08T13:45:04+00:00", category: "ORDER", type: "FILLED", symbol: "NVDA", status: "FILLED", detail: "live-nvda-001" },
    { timestamp: "2026-09-08T13:45:03+00:00", category: "ORDER", type: "BROKER_ACK", symbol: "NVDA", status: "PASS", detail: "live-nvda-001" },
    { timestamp: "2026-09-08T13:45:02+00:00", category: "RISK", type: "RISK_PASS", symbol: "NVDA", status: "PASS", detail: null },
    { timestamp: "2026-09-08T13:45:01+00:00", category: "STRATEGY", type: "TARGET", symbol: "NVDA", status: "PARTICIPATE", detail: "target 11%" },
    { timestamp: "2026-09-08T13:45:00+00:00", category: "SYSTEM", type: "CYCLE_START", symbol: null, status: "PASS", detail: null },
  ],
};

TERMINAL_POSITION_HEAVY.positions.largest_position = TERMINAL_POSITION_HEAVY.positions.rows.find((row) => row.symbol === "NVDA") ?? null;

export const TERMINAL_NO_POSITIONS: LiveTerminalSnapshot = { ...TERMINAL_POSITION_HEAVY, positions: { status: "OK", as_of: AT, largest_position: null, rows: [] }, orders: [], execution: { ...TERMINAL_POSITION_HEAVY.execution, order_count: 0, fill_count: 0, reject_count: 0, partial_fill_count: 0, fill_rate: null, reject_rate: null, partial_fill_rate: null, average_slippage_bps: null, median_slippage_bps: null, p95_slippage_bps: null, slippage_sample_size: 0, average_fill_latency_ms: null, latency_sample_size: 0 }, events: [] };

export const TERMINAL_DEFENSIVE: LiveTerminalSnapshot = { ...TERMINAL_NO_POSITIONS, strategy: { ...TERMINAL_POSITION_HEAVY.strategy!, regime: { ...TERMINAL_POSITION_HEAVY.strategy!.regime!, state: "DEFENSIVE", participate: false } } };

export const TERMINAL_HISTORY: LiveHistory = {
  generated_at: AT, environment: "LIVE", read_only: true, status: "CLEAN", sample_size: 6, today_trading_pnl: "0.44",
  points: [
    [1, "2026-09-01T20:00:00+00:00", "50.00", "50.00", "0.00", "0.00"],
    [2, "2026-09-02T20:00:00+00:00", "50.60", "50.60", "0.60", "0.00"],
    [3, "2026-09-03T20:00:00+00:00", "100.60", "50.60", "0.60", "0.00"],
    [4, "2026-09-04T20:00:00+00:00", "101.10", "51.10", "1.10", "0.00"],
    [5, "2026-09-05T20:00:00+00:00", "100.70", "50.70", "0.70", "-0.00783"],
    [6, "2026-09-08T14:00:00+00:00", "101.14", "51.14", "1.14", "-0.00000"],
  ].map(([checkpoint_id, taken_at, broker_equity, flow_adjusted_equity, trading_pnl, drawdown]) => ({ checkpoint_id: Number(checkpoint_id), taken_at: String(taken_at), utc_date: String(taken_at).slice(0, 10), kind: "PERIODIC", broker_equity: String(broker_equity), broker_cash: "72.00", unrealized_pnl: "0.07", position_count: 5, flow_adjusted_equity: String(flow_adjusted_equity), trading_pnl: String(trading_pnl), time_weighted_return: "0.0228", adjusted_equity_hwm: "51.14", drawdown: String(drawdown) })),
  flows: [{ settle_at: "2026-09-03T14:00:00+00:00", activity_type: "CSD", classification: "DEPOSIT", confirmation: "CONFIRMED", amount: "50.00", currency: "USD", relation: "EXTERNAL" }],
};

export const TERMINAL_EDGE_FIXTURES: ReadonlyArray<{ key: string; fixture: LiveFixture; terminal: LiveTerminalSnapshot; history: LiveHistory }> = [
  { key: "baseline-50-disarmed-no-positions", fixture: FIXTURE_A_BASELINE, terminal: TERMINAL_NO_POSITIONS, history: { ...TERMINAL_HISTORY, points: TERMINAL_HISTORY.points.slice(0, 1), sample_size: 1, flows: [], today_trading_pnl: "0.00" } },
  { key: "armed-participate-position-heavy-positive", fixture: FIXTURE_H_ARMED, terminal: TERMINAL_POSITION_HEAVY, history: TERMINAL_HISTORY },
  { key: "defensive-negative-drawdown", fixture: FIXTURE_E_DRAWDOWN, terminal: TERMINAL_DEFENSIVE, history: { ...TERMINAL_HISTORY, today_trading_pnl: "-0.40" } },
  { key: "stale-accounting", fixture: FIXTURE_F_STALE, terminal: TERMINAL_POSITION_HEAVY, history: { ...TERMINAL_HISTORY, status: "STALE" } },
  { key: "unknown-external-flow", fixture: FIXTURE_G_UNKNOWN_FLOW, terminal: TERMINAL_NO_POSITIONS, history: { ...TERMINAL_HISTORY, status: "UNKNOWN_EXTERNAL_FLOW", points: [], flows: [], sample_size: 0, today_trading_pnl: null } },
  { key: "withdrawal", fixture: FIXTURE_K_WITHDRAWAL, terminal: TERMINAL_POSITION_HEAVY, history: { ...TERMINAL_HISTORY, flows: [{ settle_at: "2026-09-08T14:00:00+00:00", activity_type: "CSW", classification: "WITHDRAWAL", confirmation: "CONFIRMED", amount: "10.00", currency: "USD", relation: "EXTERNAL" }] } },
  { key: "broker-unavailable", fixture: { ...FIXTURE_A_BASELINE, safety: { ...FIXTURE_A_BASELINE.safety, account: { ...FIXTURE_A_BASELINE.safety.account, status: "UNREADABLE", equity: null, cash: null }, live_ready: null } }, terminal: { ...TERMINAL_NO_POSITIONS, positions: { status: "BROKER_UNAVAILABLE", as_of: null, largest_position: null, rows: [] } }, history: TERMINAL_HISTORY },
  { key: "reconciliation-failure", fixture: { ...FIXTURE_A_BASELINE, safety: { ...FIXTURE_A_BASELINE.safety, reconciliation: { ...FIXTURE_A_BASELINE.safety.reconciliation, status: "FAILED", safe_to_trade: false, issues: 1, unresolved: 1 } } }, terminal: TERMINAL_NO_POSITIONS, history: TERMINAL_HISTORY },
];
