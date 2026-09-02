"use client";

/**
 * Realized trade P&L: the wire types and the poll.
 *
 * A fourth record on the Equity Paper page, and the one with the strictest
 * reading rules, so they are stated here rather than left to each component:
 *
 * **Three P&L numbers, three meanings, no arithmetic between them.**
 * `daily_pnl` on the account record is account equity against the stored
 * UTC-day baseline. `unrealized_pnl` on a position is the broker's own figure.
 * Realized P&L is what confirmed sales released, from a ledger built out of
 * broker-confirmed executions. They are measured over different windows and
 * from different sources, and the payload says so with
 * `components_are_independent`. Nothing here adds them.
 *
 * **Status gates the figures, not the other way round.** When `status` is not
 * `CLEAN`, the numbers may be wrong and the screen has to say so beside them.
 * A component that renders realized P&L without rendering its status is a bug.
 *
 * **Realized *events*, never "trades".** This book trims on drift, so one
 * symbol can produce many sales without ever closing a position. Nothing here
 * counts a round trip, because the system does not define one.
 */

import { usePoll, type PollState } from "./api";

/** Slower than the paper poll: the ledger advances only when a sale fills. */
export const REALIZED_POLL_INTERVAL_MS = 30_000;

export const REALIZED_SUMMARY_ENDPOINT = "/api/equity-paper/realized-pnl/summary";
export const REALIZED_STATUS_ENDPOINT = "/api/equity-paper/realized-pnl/status";

export type AccountingStatus = "CLEAN" | "DEGRADED" | "MISMATCH" | "UNKNOWN";

export interface AccountingStatusPanel {
  status: AccountingStatus;
  tone: string;
  tracking_started_at: string | null;
  tracking_label: string;
  bootstrap_method: string | null;
  historical_completeness: string | null;
  basis_method: string | null;
  execution_granularity: string;
  symbols_checked: number;
  quantity_mismatches: number;
  cost_deviations: number;
  last_reconciled_at: string | null;
  last_sync_at: string | null;
  last_sync_status: string | null;
  message: string | null;
}

export interface SymbolRealized {
  symbol: string;
  realized_today: number;
  realized_since_tracking: number;
  realized_today_exact: string;
  realized_since_tracking_exact: string;
  event_count: number;
  event_count_today: number;
  quantity: string | null;
  average_cost: number | null;
  total_cost_basis: number | null;
  accounting_status: string;
  tone_today: string;
  tone_since: string;
}

export interface RealizedEventRow {
  event_id: number;
  symbol: string;
  realized_at: string;
  realized_date_utc: string;
  side: "SELL";
  quantity: string;
  execution_price: number;
  average_cost_before: number;
  released_cost_basis: number;
  gross_proceeds: number;
  net_realized_pnl: number;
  net_realized_pnl_exact: string;
  fees: number;
  quantity_after: string;
  provenance: string;
  broker_order_id: string;
  broker_execution_id: string | null;
  tone: string;
}

export interface RealizedSummary {
  realized_today: number;
  realized_since_tracking: number;
  realized_today_exact: string;
  realized_since_tracking_exact: string;
  event_count: number;
  event_count_today: number;
  winning_events: number;
  losing_events: number;
  flat_events: number;
  average_winner: number | null;
  average_loser: number | null;
  tone_today: string;
  tone_since: string;
  utc_day: string;
  status: AccountingStatusPanel;
  components_are_independent: boolean;
  fills_imported: number;
  symbols: SymbolRealized[];
}

export interface RealizedPnlPanel {
  generated_at: string;
  available: boolean;
  unavailable_reason: string | null;
  summary: RealizedSummary | null;
  status: AccountingStatusPanel | null;
  components_are_independent: boolean;
}

export interface SymbolRealizedPanel {
  generated_at: string;
  available: boolean;
  unavailable_reason: string | null;
  symbol: string;
  realized: SymbolRealized | null;
  events: RealizedEventRow[];
  status: AccountingStatusPanel | null;
}

/** The account-level realized figures. `stamp` is the ledger's own sync time. */
export function useRealizedPnl(): PollState<RealizedPnlPanel> {
  return usePoll<RealizedPnlPanel>(REALIZED_SUMMARY_ENDPOINT, REALIZED_POLL_INTERVAL_MS);
}

/** One symbol's realized detail, fetched only while its drawer is open. */
export function useSymbolRealized(symbol: string | null): PollState<SymbolRealizedPanel> {
  const endpoint = symbol ? `/api/equity-paper/symbols/${encodeURIComponent(symbol)}/realized-pnl` : null;
  return usePoll<SymbolRealizedPanel>(endpoint, REALIZED_POLL_INTERVAL_MS);
}
