"use client";

/**
 * The Equity Shadow wire types and its poll.
 *
 * A separate endpoint from the operational dashboard's, against a separate
 * backend process, reading a separate database - because the two records mean
 * different things. `/api/dashboard/*` is what the account actually did.
 * `/api/equity-shadow/*` is what two engines *would have* done, recorded by a
 * process that has no way to act. Mixing them on one payload would be the
 * first step towards mixing them on one screen.
 */

import { usePoll, type PollState } from "./api";
import type { Tone } from "./types";

/** How often the page re-reads. Cycles are 15 minutes apart; this is plenty. */
export const SHADOW_POLL_INTERVAL_MS = 15_000;

export const SHADOW_ENDPOINT = "/api/equity-shadow/overview";

export type ShadowStatus = "RUNNING" | "IDLE" | "STALE" | "STOPPED" | "UNAVAILABLE";

export interface ServicePanel {
  status: ShadowStatus;
  status_reason: string;
  mode: string;
  universe: string[];
  symbols_recorded_last_cycle: number;
  last_cycle_at: string | null;
  next_expected_cycle_at: string | null;
  seconds_since_last_cycle: number | null;
  cycles_recorded: number;
  code_sha: string | null;
  started_at: string | null;
  last_error: string | null;
  session_confirmed_open: boolean;
  within_regular_session: boolean;
  stale_after_seconds: number;
  broker_mutation: string;
  orders_submitted: number;
  order_intents_in_database: number;
  linked_orders_in_database: number;
  zero_order_invariant_holds: boolean;
  released_candidates: number;
  released_candidates_meaning: string;
  startup_safety_applicable: boolean;
  startup_safety_note: string;
}

export interface RegimePanel {
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

export interface SymbolRow {
  symbol: string;
  bar_timestamp: string | null;
  reference_close: number | null;
  v3_signal: string | null;
  v3_score: number | null;
  v3_confidence: number | null;
  v3_regime: string | null;
  v3_reasons: string[];
  v3_stance: number | null;
  eda1_signal: string | null;
  eda1_regime: string | null;
  eda1_reasons: string[];
  eda1_stance: number | null;
  eda1_score_source: string;
  signals_agree: boolean | null;
  stances_agree: boolean | null;
  participate: boolean | null;
}

export interface EngineHypothetical {
  engine: string;
  portfolio_value: number | null;
  cumulative_return: number | null;
  max_drawdown: number | null;
  long_exposure_fraction: number | null;
  stance_changes: number;
  turnover_per_step: number | null;
  current_long_symbols: string[];
  current_stance_summary: string;
}

export interface HypotheticalPanel {
  label: string;
  normalized_start: number;
  steps: number;
  first_bar: string | null;
  last_bar: string | null;
  costs_applied: boolean;
  v3: EngineHypothetical | null;
  eda1: EngineHypothetical | null;
  benchmark_return: number | null;
  unavailable_reason: string | null;
}

export interface ComparisonPanel {
  bars_compared: number;
  steps: number;
  agreement_count: number;
  disagreement_count: number;
  agreement_fraction: number | null;
  stance_disagreement_count: number;
  participate_bars: number;
  defensive_bars: number;
  participate_sessions: number;
  defensive_sessions: number;
  regime_transitions: number;
  up_capture: number | null;
  down_capture: number | null;
  capture_unavailable_reason: string | null;
  sample_warning: string;
  sample_is_sufficient: boolean;
  unavailable_reason: string | null;
}

export interface ShadowOverview {
  generated_at: string;
  read_only: boolean;
  observation_only: boolean;
  hypothetical_label: string;
  service: ServicePanel;
  regime: RegimePanel;
  symbols: SymbolRow[];
  hypothetical: HypotheticalPanel;
  comparison: ComparisonPanel;
}

export type ShadowState = PollState<ShadowOverview>;

export function useShadowOverview(): ShadowState {
  return usePoll<ShadowOverview>(SHADOW_ENDPOINT, SHADOW_POLL_INTERVAL_MS);
}

/**
 * How a shadow status should read. `RUNNING` is the observation colour, never
 * green: an observer that is up is observing, not trading. `IDLE` is not a
 * fault and is not amber.
 */
export function shadowTone(status: ShadowStatus): Tone {
  switch (status) {
    case "RUNNING":
      return "SHADOW";
    case "IDLE":
      return "MUTED";
    case "STALE":
      return "ATTENTION";
    default:
      return "NEGATIVE";
  }
}

/** The word for a shadow status on screen: a running observer is OBSERVING. */
export function shadowStatusLabel(status: ShadowStatus): string {
  return status === "RUNNING" ? "OBSERVING" : status;
}

/** Plain English for the machine reason on the status strip. */
export const STATUS_REASON_LABELS: Record<string, string> = {
  CYCLE_WITHIN_EXPECTED_INTERVAL: "A cycle landed when one was due.",
  OFF_SESSION_NO_BARS_EXPECTED:
    "Outside US regular hours. No bars exist to observe, so silence is correct.",
  NO_CYCLE_DURING_CONFIRMED_OPEN_SESSION:
    "The broker calendar says the market is open and cycles have stopped arriving.",
  NO_CYCLE_RECORDED_YET: "Started; the first cycle has not landed yet.",
  CLEAN_SHUTDOWN_RECORDED: "The observer recorded a clean shutdown.",
  ZERO_ORDER_INVARIANT_VIOLATED: "An order intent or linked order exists. This must be zero.",
  DATABASE_UNREADABLE: "The shadow database could not be read.",
};
