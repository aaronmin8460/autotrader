"use client";

/**
 * Chart data: one batched GET per (range, symbol set), cached by the browser
 * for as long as the backend says the range stays fresh.
 *
 * Charts are price series and nothing more. They come from a separate
 * loopback process that talks to the market-data provider and to nothing else,
 * so a chart failing to load leaves every account panel exactly as it was. The
 * hook below asks for every symbol a panel needs in one request, keeps what
 * came back, and re-asks only when the range's TTL has passed or a symbol is
 * missing. It never fires one request per row.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { REQUEST_TIMEOUT_MS, getJson } from "./api";
import { chartUnavailableLabel, chunk, normalizeSymbols } from "./chart-util";
import type { AssetClass } from "./types";

export { chartUnavailableLabel, chunk } from "./chart-util";

export type ChartRange = "1D" | "5D" | "1M" | "3M" | "6M";

export const CHART_RANGES: ReadonlyArray<ChartRange> = ["1D", "5D", "1M", "3M", "6M"];

/** The ranges a sparkline may show: short, so the table stays a table. */
export const SPARKLINE_RANGES: ReadonlyArray<ChartRange> = ["1D", "5D", "1M"];

export const CHARTS_ENDPOINT = "/api/market-charts/bars";

/** The backend refuses more; the hook chunks a wider set into several GETs. */
export const MAX_SYMBOLS_PER_REQUEST = 12;

/** `[timestamp, open, high, low, close, volume]` */
export type ChartPoint = [string, number, number, number, number, number];

export interface ChartSeries {
  symbol: string;
  asset_class: AssetClass;
  range: ChartRange;
  timeframe: string;
  available: boolean;
  points: ChartPoint[];
  first_at: string | null;
  last_at: string | null;
  first_close: number | null;
  last_close: number | null;
  change_fraction: number | null;
  fetched_at: string | null;
  from_cache: boolean;
  unavailable_reason: string | null;
}

export interface ChartBatch {
  generated_at: string;
  range: ChartRange;
  range_label: string;
  ttl_seconds: number;
  series: ChartSeries[];
  provider_calls_made: number;
  cache_hits: number;
  budget_remaining: number;
  note: string;
}

export interface ChartState {
  /** Series by symbol, for the requested range. Missing means not loaded yet. */
  series: Readonly<Record<string, ChartSeries>>;
  /** True while a request is in flight. */
  loading: boolean;
  /** False after the most recent request failed; the last series stay. */
  connected: boolean;
  /** Why a series is missing, in operator words. */
  reason: (symbol: string) => string;
}

interface CacheEntry {
  series: ChartSeries;
  expiresAt: number;
}

/**
 * Module-level so the operations page and a detail drawer share what they
 * fetched, keyed by range and symbol so switching a range back and forth
 * inside the TTL costs nothing.
 */
const cache = new Map<string, CacheEntry>();

function key(range: ChartRange, symbol: string): string {
  return `${range}:${symbol}`;
}

export function useChartBatch(symbols: ReadonlyArray<string>, range: ChartRange): ChartState {
  const wanted = useMemo(() => normalizeSymbols(symbols), [symbols]);
  const signature = wanted.join(",");
  const [series, setSeries] = useState<Record<string, ChartSeries>>({});
  const [loading, setLoading] = useState(false);
  const [connected, setConnected] = useState(true);
  const inFlight = useRef(false);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      if (inFlight.current || wanted.length === 0) return;
      const now = Date.now();
      const fresh: Record<string, ChartSeries> = {};
      const missing: string[] = [];
      for (const symbol of wanted) {
        const entry = cache.get(key(range, symbol));
        if (entry && entry.expiresAt > now) fresh[symbol] = entry.series;
        else missing.push(symbol);
      }
      if (!cancelled) setSeries(fresh);
      if (missing.length === 0) return;

      inFlight.current = true;
      setLoading(true);
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS * 2);
      try {
        for (const group of chunk(missing, MAX_SYMBOLS_PER_REQUEST)) {
          const query = `${CHARTS_ENDPOINT}?symbols=${encodeURIComponent(group.join(","))}&range=${range}`;
          const batch = await getJson<ChartBatch>(query, controller.signal);
          const expiresAt = Date.now() + batch.ttl_seconds * 1000;
          for (const item of batch.series) {
            cache.set(key(range, item.symbol), { series: item, expiresAt });
            fresh[item.symbol] = item;
          }
        }
        if (!cancelled) {
          setSeries({ ...fresh });
          setConnected(true);
        }
      } catch {
        if (!cancelled) setConnected(false);
      } finally {
        clearTimeout(timer);
        inFlight.current = false;
        if (!cancelled) setLoading(false);
      }
    }

    void load();
    // Re-check on the shortest TTL cadence; the cache decides what to refetch.
    const interval = setInterval(() => void load(), 60_000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
    // `signature` stands in for the symbol list so a new array with the same
    // symbols does not refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, range]);

  return {
    series,
    loading,
    connected,
    reason: (symbol: string) => chartUnavailableLabel(series[symbol]?.unavailable_reason),
  };
}
