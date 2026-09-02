"use client";

/**
 * The A1-B U30 Shadow wire types and its poll.
 *
 * A fourth endpoint against a fourth backend process reading a fourth
 * database - the one identity that can open the A1-B record. Everything on
 * this payload is an observation: weights the frozen A1-B allocation *would*
 * have held, recorded by a process whose observation table refuses an order
 * linkage by constraint. Nothing here is account state.
 */

import { usePoll, type PollState } from "./api";
import type { ShadowStatus } from "./shadow";

/** Cycles land every fifteen minutes during a session; this is plenty. */
export const A1B_POLL_INTERVAL_MS = 15_000;

export const A1B_ENDPOINT = "/api/equity-a1b-shadow/overview";

export interface A1BServicePanel {
  status: ShadowStatus;
  status_reason: string;
  mode: string;
  designation: string;
  universe: string[];
  universe_size: number;
  incumbents: string[];
  alias_scored: string[];
  symbols_recorded_last_cycle: number;
  last_cycle_at: string | null;
  next_expected_cycle_at: string | null;
  seconds_since_last_cycle: number | null;
  cycles_recorded: number;
  observations_recorded: number;
  first_bar: string | null;
  last_bar: string | null;
  code_sha: string | null;
  started_at: string | null;
  session_confirmed_open: boolean;
  within_regular_session: boolean;
  stale_after_seconds: number;
  policy_hash: string | null;
  mark_every_sessions: number | null;
  grid_anchor: string | null;
  mark_index: number | null;
  mark_date: string | null;
  fit_mark: string | null;
  labeled_symbols: number | null;
  broker_mutation: string;
  orders_submitted: number;
  order_intents_in_database: number;
  linked_orders_in_database: number;
  non_simulated_rows: number;
  zero_order_invariant_holds: boolean;
  invariant_note: string;
}

export interface A1BRegimePanel {
  session_date: string | null;
  state: string | null;
  participate: boolean | null;
  reference_symbol: string | null;
  info_close: number | null;
  info_sma: number | null;
  info_drawdown: number | null;
  sessions_observed: number | null;
  sma_sessions: number | null;
  calm_threshold: number | null;
  lag_sessions: number | null;
  computed_at: string | null;
  unavailable_reason: string | null;
}

export interface A1BSymbolRow {
  symbol: string;
  incumbent: boolean;
  alias_scored: boolean | null;
  bar_timestamp: string | null;
  reference_close: number | null;
  v3_signal: string | null;
  v3_stance: number | null;
  stance: number | null;
  stance_updated_at: string | null;
  participate: boolean | null;
  archetype_label: number | null;
  active_weight: number | null;
  reserved_weight: number | null;
  target_weight: number | null;
  designation: string;
}

export interface A1BHypotheticalPanel {
  label: string;
  normalized_start: number;
  steps: number;
  first_bar: string | null;
  last_bar: string | null;
  costs_applied: boolean;
  portfolio_value: number | null;
  cumulative_return: number | null;
  max_drawdown: number | null;
  average_exposure: number | null;
  current_exposure: number | null;
  long_symbols: number;
  weight_changes: number;
  turnover_per_step: number | null;
  benchmark_return: number | null;
  sample_warning: string;
  sample_is_sufficient: boolean;
  capture_unavailable_reason: string | null;
  unavailable_reason: string | null;
}

export interface A1BObservationSummary {
  observations: number;
  bars: number;
  symbols_per_bar: number;
  participate_bars: number;
  defensive_bars: number;
  participate_sessions: number;
  defensive_sessions: number;
  regime_transitions: number;
  buy_signals: number;
  sell_signals: number;
  hold_signals: number;
  alias_scored_observations: number;
  marks_computed: number;
  unavailable_reason: string | null;
}

export interface A1BOverview {
  generated_at: string;
  read_only: boolean;
  observation_only: boolean;
  hypothetical_label: string;
  service: A1BServicePanel;
  regime: A1BRegimePanel;
  symbols: A1BSymbolRow[];
  hypothetical: A1BHypotheticalPanel;
  summary: A1BObservationSummary;
}

export function useA1BOverview(): PollState<A1BOverview> {
  return usePoll<A1BOverview>(A1B_ENDPOINT, A1B_POLL_INTERVAL_MS);
}
