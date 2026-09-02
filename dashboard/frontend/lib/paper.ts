"use client";

/**
 * The Equity Paper wire types and its poll.
 *
 * A third endpoint, against a third backend process, reading a third database
 * - and the separation is the point rather than an accident of how it grew.
 *
 *   /api/dashboard/*            what the crypto book actually did, and the
 *                               whole broker account
 *   /api/equity-shadow/*        what two engines WOULD have done, recorded by a
 *                               process that has no way to act
 *   /api/equity-a1b-shadow/*    what a third allocation WOULD have held, likewise
 *   /api/equity-paper/*         what the equity book actually did, on the same
 *                               broker account as the crypto book - and the
 *                               deployed sizing policy it did it under
 *
 * The middle two are hypothetical and the outer two are real. Adding the paper
 * figures to a shadow's compounded curve would produce a number that is
 * neither, and doing it on one payload would be the first step towards doing
 * it on one screen.
 */

import { usePoll, type PollState } from "./api";

/** Cycles land every fifteen minutes during a session; this is plenty. */
export const PAPER_POLL_INTERVAL_MS = 15_000;

export const PAPER_ENDPOINT = "/api/equity-paper/overview";

export interface PaperServicePanel {
  mode: string;
  environment: string;
  running: boolean;
  stale: boolean;
  stage: string | null;
  execution_universe: string[];
  decision_universe: string[];
  sizing_policy: string | null;
  sizing_config_hash: string | null;
  started_at: string | null;
  stopped_at: string | null;
  last_cycle_at: string | null;
  unresolved_intents: number;
  unavailable_reason: string | null;
}

export interface PaperRegimePanel {
  session_date: string | null;
  participate: boolean | null;
  reference_symbol: string | null;
  info_close: number | null;
  info_sma: number | null;
  info_drawdown: number | null;
  sessions_observed: number | null;
  spec: Record<string, number>;
}

export interface PaperExposurePanel {
  account_equity: number | null;
  crypto_positions: string[];
  equity_positions: string[];
  equity_positions_as_of: string | null;
  equity_exposure_note: string;
  per_symbol_cap: string;
  total_account_cap: string;
  target_account_gross: string;
  cash_reserve_target: string;
  fractional_mode: boolean;
  daily_loss_halt: string;
}

/**
 * The deployed sizing policy's figures, as numbers.
 *
 * Read from the running paper process's own start event and resolved in the
 * allocation registry; `authoritative` is true only when the runtime named
 * the policy itself. These are the only source of any target or cap on the
 * operations page.
 */
export interface PolicyPanel {
  policy_id: string;
  config_hash: string | null;
  source: string;
  authoritative: boolean;
  target_gross: number;
  hard_gross_cap: number;
  hard_symbol_cap: number;
  cash_reserve_target: number;
  target_slot_weight: number;
  universe_size: number;
  fractional: boolean;
  daily_loss_halt: number;
  note: string;
}

export interface PaperTargetRow {
  symbol: string;
  in_execution_universe: boolean;
  bar_timestamp: string | null;
  participate: boolean | null;
  eda1_signal: string | null;
  eda1_stance: number | null;
  v3_signal: string | null;
  stances_agree: boolean | null;
  reference_close: number | null;
  actual_quantity: string;
  last_risk_reason: string | null;
  stance_label?: string | null;
  target_weight?: number | null;
  target_source?: string;
  target_notional?: number | null;
  target_quantity?: string | null;
  target_bar_timestamp?: string | null;
  target_decided_at?: string | null;
  target_external_exposure?: number | null;
  last_order_side?: string | null;
  last_order_client_order_id?: string | null;
  action?: string;
}

export interface PaperOrderRow {
  client_order_id: string;
  symbol: string;
  side: string;
  requested_quantity: string;
  approved_quantity: string;
  status: string;
  risk_reason_code: string;
  created_at: string | null;
  broker_status: string | null;
  filled_quantity: string | null;
  filled_average_price: number | null;
}

export interface PaperSafetyPanel {
  account_safety: string | null;
  account_safety_reason: string | null;
  reconciliation_status: string | null;
  reconciliation_at: string | null;
  reconciliation_unresolved: number | null;
  parity_mismatches: number;
  risk_blocked_recent: string[];
}

export interface PaperOverview {
  generated_at: string;
  mode: string;
  read_only: boolean;
  service: PaperServicePanel;
  regime: PaperRegimePanel;
  exposure: PaperExposurePanel;
  targets: PaperTargetRow[];
  orders: PaperOrderRow[];
  safety: PaperSafetyPanel;
  policy?: PolicyPanel | null;
}

export type PaperState = PollState<PaperOverview>;

export function usePaperOverview(): PaperState {
  return usePoll<PaperOverview>(PAPER_ENDPOINT, PAPER_POLL_INTERVAL_MS);
}
