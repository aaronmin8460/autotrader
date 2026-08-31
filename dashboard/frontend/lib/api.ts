"use client";

/**
 * Polling. Five seconds, one endpoint, and a deliberate refusal to blank out.
 *
 * This system trades on completed 15-minute bars. A dashboard that streamed
 * sub-second updates would be showing motion, not information, so the refresh
 * is a plain interval against one consolidated endpoint - and because that
 * endpoint assembles the whole page from a single database read, two panels
 * can never disagree about the same instant.
 *
 * **A failed poll never clears the screen.** The last good payload stays
 * rendered and the header says the connection dropped and when the data is
 * from. Blanking the page at the moment the backend becomes unreachable would
 * destroy exactly the context an operator needs to diagnose why.
 *
 * Two polls, not one, and they are two because the answers come from two
 * processes. The overview describes the account and the trails in its own
 * store; the service poll below describes which units are up on the host,
 * which that store cannot know - see `lib/services` for why the difference
 * mattered enough to add a second request to a deliberately quiet page.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { SERVICES_ENDPOINT, type ServiceUnitsPanel } from "./services";
import type { Overview } from "./types";

/** How often the page re-reads. See the module docstring for why it is slow. */
export const POLL_INTERVAL_MS = 5_000;

/** How long one poll may take before it is abandoned and retried. */
const REQUEST_TIMEOUT_MS = 8_000;

const ENDPOINT = "/api/dashboard/overview";

export interface OverviewState {
  /** The last payload that arrived, or null before the first one does. */
  data: Overview | null;
  /** True until the first poll settles, successfully or not. */
  loading: boolean;
  /** True while the most recent poll succeeded. */
  connected: boolean;
  /** When the last successful poll landed, as an ISO string. */
  lastSuccessAt: string | null;
  /** Force a poll now. */
  refresh: () => void;
}

export function useOverview(): OverviewState {
  const [data, setData] = useState<Overview | null>(null);
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
      const payload = (await response.json()) as Overview;
      setData(payload);
      setConnected(true);
      setLastSuccessAt(payload.generated_at);
    } catch {
      // Deliberately silent, and deliberately non-destructive: `data` is left
      // exactly as it was so the page keeps showing the last known truth.
      setConnected(false);
    } finally {
      clearTimeout(timer);
      inFlight.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void poll();
    const interval = setInterval(() => void poll(), POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [poll]);

  return { data, loading, connected, lastSuccessAt, refresh: () => void poll() };
}

export interface ServicesState {
  /** The last panel that arrived, or null before one does. */
  services: ServiceUnitsPanel | null;
  /** True while the most recent poll succeeded. */
  connected: boolean;
}

/**
 * Poll the service manager's view of the runtime units.
 *
 * Same contract as the overview poll: a failure keeps the last good answer and
 * never blanks the panel. What it must *not* do is fall back to the trail-derived
 * rows when it fails - those describe a different service, and substituting them
 * is the bug this endpoint exists to fix. `healthRows` renders `null` as four
 * UNKNOWN rows instead.
 */
export function useServiceUnits(): ServicesState {
  const [services, setServices] = useState<ServiceUnitsPanel | null>(null);
  const [connected, setConnected] = useState(false);
  const inFlight = useRef(false);

  const poll = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(SERVICES_ENDPOINT, {
        cache: "no-store",
        signal: controller.signal,
        headers: { accept: "application/json" },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setServices((await response.json()) as ServiceUnitsPanel);
      setConnected(true);
    } catch {
      setConnected(false);
    } finally {
      clearTimeout(timer);
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    void poll();
    const interval = setInterval(() => void poll(), POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [poll]);

  return { services, connected };
}
