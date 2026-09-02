"use client";

/**
 * Polling. One generic hook, a few named endpoints, and a deliberate refusal
 * to blank out.
 *
 * This system trades on completed 15-minute bars. A dashboard that streamed
 * sub-second updates would be showing motion, not information, so every read
 * is a plain interval against one consolidated endpoint per record - and each
 * of those endpoints assembles its payload from a single database read, so
 * two panels from the same record can never disagree about the same instant.
 *
 * **A failed poll never clears the screen.** The last good payload stays
 * rendered and the header says the connection dropped and when the data is
 * from. Blanking the page at the moment a backend becomes unreachable would
 * destroy exactly the context an operator needs to diagnose why.
 *
 * Every request here is a GET. There is no other method in this file, and
 * the tests assert it against the source.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { SERVICES_ENDPOINT, type ServiceUnitsPanel } from "./services";
import type { Overview } from "./types";

/** How often the account record re-reads. See the module docstring. */
export const POLL_INTERVAL_MS = 5_000;

/** How long one poll may take before it is abandoned and retried. */
export const REQUEST_TIMEOUT_MS = 8_000;

export const OVERVIEW_ENDPOINT = "/api/dashboard/overview";

export interface PollState<T> {
  /** The last payload that arrived, or null before the first one does. */
  data: T | null;
  /** True until the first poll settles, successfully or not. */
  loading: boolean;
  /** True while the most recent poll succeeded. */
  connected: boolean;
  /** When the last successful poll landed, as an ISO string. */
  lastSuccessAt: string | null;
  /** Force a poll now. */
  refresh: () => void;
}

/** A GET, with a timeout, that resolves to the parsed JSON or throws. */
export async function getJson<T>(endpoint: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(endpoint, {
    cache: "no-store",
    signal,
    headers: { accept: "application/json" },
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as T;
}

/**
 * Poll one GET endpoint on an interval, keeping the last good answer.
 *
 * `stamp` names the payload field that says when the record was read; it is
 * what the header shows as "last sync" so the time on screen is the backend's
 * time, not the browser's.
 */
export function usePoll<T extends object>(
  endpoint: string | null,
  intervalMs: number,
  stamp: keyof T & string = "generated_at" as keyof T & string,
): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [connected, setConnected] = useState(false);
  const [lastSuccessAt, setLastSuccessAt] = useState<string | null>(null);
  const inFlight = useRef(false);

  const poll = useCallback(async () => {
    if (!endpoint || inFlight.current) return;
    inFlight.current = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const payload = await getJson<T>(endpoint, controller.signal);
      setData(payload);
      setConnected(true);
      const at = payload[stamp];
      setLastSuccessAt(typeof at === "string" ? at : new Date().toISOString());
    } catch {
      // Deliberately silent, and deliberately non-destructive: `data` is left
      // exactly as it was so the page keeps showing the last known truth.
      setConnected(false);
    } finally {
      clearTimeout(timer);
      inFlight.current = false;
      setLoading(false);
    }
  }, [endpoint, stamp]);

  useEffect(() => {
    void poll();
    const interval = setInterval(() => void poll(), intervalMs);
    return () => clearInterval(interval);
  }, [poll, intervalMs]);

  return { data, loading, connected, lastSuccessAt, refresh: () => void poll() };
}

export type OverviewState = PollState<Overview>;

/** The account record: broker account, positions, and the crypto store's trail. */
export function useOverview(): OverviewState {
  return usePoll<Overview>(OVERVIEW_ENDPOINT, POLL_INTERVAL_MS);
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
 * What it must *not* do is fall back to the trail-derived rows when it fails
 * - those describe a different service, and substituting them is the bug
 * this endpoint exists to fix. `healthRows` renders `null` as UNKNOWN rows.
 */
export function useServiceUnits(): ServicesState {
  const { data, connected } = usePoll<ServiceUnitsPanel>(SERVICES_ENDPOINT, POLL_INTERVAL_MS);
  return { services: data, connected };
}
