"use client";

/**
 * The three records every page needs, polled once for the whole application.
 *
 * Before this provider each page mounted its own polls, so navigating from
 * Operations to Equity Paper tore down four intervals and started four more.
 * With eight routes that arrangement would have multiplied the request rate by
 * the number of pages, which is exactly the "more live-feeling visual" trade
 * this dashboard must not make. The cadence here is **unchanged** from the
 * Operations page at Dashboard V2:
 *
 *   /api/dashboard/overview          5 s   the broker account
 *   /api/equity-paper/services       5 s   the service manager's view
 *   /api/equity-paper/overview      15 s   the deployed policy and regime
 *
 * Orders and chart bars stay page-local: only two pages need the order stream
 * and only the pages with a table need bars, so mounting those globally would
 * add requests rather than remove them.
 *
 * Everything here is a GET. There is no mutation in this file and no endpoint
 * behind one.
 */

import { createContext, useContext, useMemo, type ReactNode } from "react";

import { POLL_INTERVAL_MS, useOverview, useServiceUnits, type OverviewState } from "./api";
import { PAPER_POLL_INTERVAL_MS, usePaperOverview, type PaperState } from "./paper";
import type { ServiceUnitsPanel } from "./services";
import { freshnessOf, type Freshness } from "@/components/ui";

export interface DashboardData {
  account: OverviewState;
  paper: PaperState;
  services: ServiceUnitsPanel | null;
  servicesConnected: boolean;
  /** How current the account record is, and by how much it missed. */
  freshness: { state: Freshness; ageSeconds: number | null };
  accountIntervalMs: number;
  paperIntervalMs: number;
}

const EMPTY_POLL = {
  data: null,
  loading: true,
  connected: false,
  lastSuccessAt: null,
  refresh: () => {},
};

const FALLBACK: DashboardData = {
  account: EMPTY_POLL as OverviewState,
  paper: EMPTY_POLL as PaperState,
  services: null,
  servicesConnected: false,
  freshness: { state: "WAITING", ageSeconds: null },
  accountIntervalMs: POLL_INTERVAL_MS,
  paperIntervalMs: PAPER_POLL_INTERVAL_MS,
};

const DashboardContext = createContext<DashboardData>(FALLBACK);

export function DashboardProvider({ children }: { children: ReactNode }) {
  const account = useOverview();
  const paper = usePaperOverview();
  const { services, connected: servicesConnected } = useServiceUnits();

  const value = useMemo<DashboardData>(
    () => ({
      account,
      paper,
      services,
      servicesConnected,
      freshness: freshnessOf(
        account.data?.generated_at ?? null,
        account.connected,
        POLL_INTERVAL_MS,
      ),
      accountIntervalMs: POLL_INTERVAL_MS,
      paperIntervalMs: PAPER_POLL_INTERVAL_MS,
    }),
    [account, paper, services, servicesConnected],
  );

  return <DashboardContext.Provider value={value}>{children}</DashboardContext.Provider>;
}

export function useDashboard(): DashboardData {
  return useContext(DashboardContext);
}
