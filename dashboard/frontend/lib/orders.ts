"use client";

/**
 * Account-wide recent orders: the crypto and equity paper stores, merged.
 *
 * Served by the equity paper API because that is the one process that can
 * open both order stores read-only. Every row carries the store it came from
 * as `source`, and `simulated` is `false` on every row by construction - the
 * read model takes no shadow record as input, and the panel says so.
 */

import { POLL_INTERVAL_MS, usePoll, type PollState } from "./api";
import type { AssetClass, Source, Tone } from "./types";

export const ACCOUNT_ORDERS_LIMIT = 25;
export const ACCOUNT_ORDERS_ENDPOINT = `/api/equity-paper/account-orders?limit=${ACCOUNT_ORDERS_LIMIT}`;

export type OrderSource = "CRYPTO PAPER" | "EQUITY PAPER";

export interface AccountOrderRow {
  client_order_id: string;
  broker_order_id: string | null;
  source: OrderSource;
  simulated: boolean;
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
  created_at: string;
  submitted_at: string | null;
  filled_at: string | null;
  authoritative_at: string;
}

export interface StoreSummary {
  source: OrderSource;
  available: boolean;
  rows_read: number;
  total: number;
  attention_count: number;
  unavailable_reason: string | null;
}

export interface AccountOrdersPanel {
  generated_at: string;
  rows: AccountOrderRow[];
  total: number;
  attention_count: number;
  stores: StoreSummary[];
  duplicates_dropped: number;
  includes_simulated: boolean;
  note: string;
  limit: number;
}

export function useAccountOrders(): PollState<AccountOrdersPanel> {
  return usePoll<AccountOrdersPanel>(ACCOUNT_ORDERS_ENDPOINT, POLL_INTERVAL_MS);
}
