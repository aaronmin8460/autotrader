/**
 * Real fills for a symbol, for the detail chart's markers. Pure.
 *
 * Only `EQUITY PAPER` rows the broker reported `FILLED` qualify, and only
 * rows the read model marks as not simulated - which is every row it emits,
 * asserted here anyway so a future payload cannot draw a shadow action as a
 * fill.
 */

import type { AccountOrdersPanel } from "./orders";

export interface Fill {
  at: string;
  side: "BUY" | "SELL";
  price: number | null;
  quantity: string;
  label: string;
}

export function fillsFor(panel: AccountOrdersPanel | null, symbol: string): Fill[] {
  if (!panel) return [];
  return panel.rows
    .filter(
      (row) =>
        row.symbol === symbol &&
        row.simulated === false &&
        row.source === "EQUITY PAPER" &&
        row.status === "FILLED" &&
        (row.filled_at ?? row.submitted_at) !== null,
    )
    .map((row) => ({
      at: (row.filled_at ?? row.submitted_at) as string,
      side: row.side === "BUY" ? "BUY" : "SELL",
      price: row.average_fill_price,
      quantity: row.filled_quantity ?? row.quantity,
      label: `${row.side} ${row.filled_quantity ?? row.quantity} · EQUITY PAPER fill`,
    }));
}

/** Fills that fall inside a series' time window. */
export function fillsWithin(fills: Fill[], firstAt: string | null, lastAt: string | null): Fill[] {
  if (!firstAt || !lastAt) return [];
  const start = new Date(firstAt).getTime();
  const end = new Date(lastAt).getTime() + 15 * 60 * 1000;
  return fills.filter((fill) => {
    const at = new Date(fill.at).getTime();
    return at >= start && at <= end;
  });
}
