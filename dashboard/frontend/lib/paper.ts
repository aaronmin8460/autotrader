"use client";

/**
 * The Equity Paper wire types and its poll.
 *
 * A third endpoint, against a third backend process, reading a third database
 * - and the separation is the point rather than an accident of how it grew.
 *
 *   /api/dashboard/*      what the crypto book actually did
 *   /api/equity-shadow/*  what two engines WOULD have done, recorded by a
 *                         process that has no way to act
 *   /api/equity-paper/*   what the equity book actually did, on the same
 *                         broker account as the crypto book
 *
 * The middle one is hypothetical and the outer two are real. Adding the paper
 * figures to the shadow's compounded curve would produce a number that is
 * neither, and doing it on one payload would be the first step towards doing
 * it on one screen.
 */

import { useCallback, useEffect, useRef, useState } from "react";

/** Cycles land every fifteen minutes during a session; this is plenty. */
export const PAPER_POLL_INTERVAL_MS = 15_000;

const REQUEST_TIMEOUT_MS = 8_000;

const ENDPOINT = "/api/equity-paper/overview";

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
  equity_exposure_note: string;
  per_symbol_cap: string;
  total_account_cap: string;
  daily_loss_halt: string;
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
}

export interface PaperState {
  data: PaperOverview | null;
  loading: boolean;
  connected: boolean;
  lastSuccessAt: string | null;
}

export function usePaperOverview(): PaperState {
  const [data, setData] = useState<PaperOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [connected, setConnected] = useState(false);
  const [lastSuccessAt, setLastSuccessAt] = useState<string | null>(null);
  const inFlight = useRef(false);

  const poll = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(ENDPOINT, {
        cache: "no-store",
        signal: controller.signal,
        headers: { accept: "application/json" },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as PaperOverview;
      setData(payload);
      setConnected(true);
      setLastSuccessAt(payload.generated_at);
    } catch {
      // Non-destructive: the last known record stays on screen and the header
      // says the poll stopped landing.
      setConnected(false);
    } finally {
      clearTimeout(timer);
      inFlight.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void poll();
    const interval = setInterval(() => void poll(), PAPER_POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [poll]);

  return { data, loading, connected, lastSuccessAt };
}
