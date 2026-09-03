/**
 * The pure P&L derivations: joins, keying and tone. React-free on purpose.
 *
 * Every function here is a total function over payload data with no fetching,
 * no state and no clock, so the suite runs it directly rather than through a
 * component. The wire types and the polls live in `realized.ts`; nothing in
 * this file can reach the network.
 *
 * **Nothing here adds a realized figure to a daily or unrealized one.** They
 * are three separate measurements over different windows and scopes, and an
 * arithmetic helper that combined them would be the first step towards a
 * screen that claims they sum.
 */

import type { AccountingStatus, RealizedEventRow, RealizedPnlPanel, SymbolRealized } from "./realized";

/** Per-symbol realized totals, keyed by symbol, for joining into a table. */
export function realizedBySymbol(panel: RealizedPnlPanel | null): Readonly<Record<string, SymbolRealized>> {
  const rows: Record<string, SymbolRealized> = {};
  for (const row of panel?.summary?.symbols ?? []) rows[row.symbol] = row;
  return rows;
}

/**
 * The realized P&L a SELL marker should carry, or null.
 *
 * Matched on the broker order id, which is the only identifier a fill marker
 * and a realized event provably share. Matching on time-and-symbol would
 * attach the wrong figure to one of several sales in the same minute, and a
 * BUY is never given one: a purchase realizes nothing.
 */
export function realizedForOrder(events: RealizedEventRow[], orderId: string | null, side: string): number | null {
  if (side !== "SELL" || !orderId) return null;
  const matched = events.filter((event) => event.broker_order_id === orderId);
  if (matched.length === 0) return null;
  return matched.reduce((total, event) => total + event.net_realized_pnl, 0);
}

/** The tone for a status word. Never green unless the ledger reconciled clean. */
export function statusTone(status: AccountingStatus | null | undefined): "POSITIVE" | "ATTENTION" | "NEGATIVE" | "MUTED" {
  switch (status) {
    case "CLEAN":
      return "POSITIVE";
    case "DEGRADED":
      return "ATTENTION";
    case "MISMATCH":
      return "NEGATIVE";
    case "UNKNOWN":
      return "ATTENTION";
    default:
      return "MUTED";
  }
}
