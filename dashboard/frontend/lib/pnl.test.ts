/**
 * Realized P&L helpers: the joins and the tone, and what each refuses to do.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { realizedBySymbol, realizedForOrder, statusTone } from "./pnl.ts";
import type { RealizedEventRow, RealizedPnlPanel, SymbolRealized } from "./realized.ts";

function event(overrides: Partial<RealizedEventRow> = {}): RealizedEventRow {
  return {
    event_id: 1,
    symbol: "NVDA",
    realized_at: "2026-09-02T15:18:52+00:00",
    realized_date_utc: "2026-09-02",
    side: "SELL",
    quantity: "0.620603768",
    execution_price: 226.7,
    average_cost_before: 219.85,
    released_cost_basis: 136.44,
    gross_proceeds: 140.69,
    net_realized_pnl: 4.25,
    net_realized_pnl_exact: "4.2500868816",
    fees: 0,
    quantity_after: "39.729098735",
    provenance: "EQUITY_RUNTIME",
    broker_order_id: "order-a",
    broker_execution_id: "20260902::abc",
    tone: "POSITIVE",
    ...overrides,
  };
}

function symbolRow(overrides: Partial<SymbolRealized> = {}): SymbolRealized {
  return {
    symbol: "NVDA",
    realized_today: 5.69,
    realized_since_tracking: 5.69,
    realized_today_exact: "5.6921400791",
    realized_since_tracking_exact: "5.6921400791",
    event_count: 2,
    event_count_today: 2,
    quantity: "39.729098735",
    average_cost: 219.85,
    total_cost_basis: 8734.2,
    accounting_status: "TRACKING",
    tone_today: "POSITIVE",
    tone_since: "POSITIVE",
    ...overrides,
  };
}

test("a SELL marker carries the realized P&L of its own order", () => {
  const events = [event({ broker_order_id: "order-a" }), event({ event_id: 2, broker_order_id: "order-b", net_realized_pnl: 1.44 })];

  assert.equal(realizedForOrder(events, "order-a", "SELL"), 4.25);
  assert.equal(realizedForOrder(events, "order-b", "SELL"), 1.44);
});

test("several executions of one order sum into one marker figure", () => {
  const events = [
    event({ event_id: 1, broker_order_id: "order-a", net_realized_pnl: 1.5 }),
    event({ event_id: 2, broker_order_id: "order-a", net_realized_pnl: 2.25 }),
  ];

  assert.equal(realizedForOrder(events, "order-a", "SELL"), 3.75);
});

test("a BUY is never labelled with a realized figure", () => {
  const events = [event({ broker_order_id: "order-a" })];

  assert.equal(realizedForOrder(events, "order-a", "BUY"), null);
});

test("an order the ledger has no event for gets no figure, not zero", () => {
  assert.equal(realizedForOrder([event()], "order-unknown", "SELL"), null);
  assert.equal(realizedForOrder([event()], null, "SELL"), null);
  assert.equal(realizedForOrder([], "order-a", "SELL"), null);
});

test("per-symbol rows are keyed by symbol and empty when the ledger is absent", () => {
  const panel: RealizedPnlPanel = {
    generated_at: "2026-09-02T19:30:00+00:00",
    available: true,
    unavailable_reason: null,
    components_are_independent: true,
    status: null,
    summary: {
      realized_today: 7.86,
      realized_since_tracking: 7.41,
      realized_today_exact: "7.8570775665",
      realized_since_tracking_exact: "7.4070775665",
      event_count: 6,
      event_count_today: 5,
      winning_events: 3,
      losing_events: 3,
      flat_events: 0,
      average_winner: 2.65,
      average_loser: -0.18,
      tone_today: "POSITIVE",
      tone_since: "POSITIVE",
      utc_day: "2026-09-02",
      status: null as never,
      components_are_independent: true,
      fills_imported: 62,
      symbols: [symbolRow(), symbolRow({ symbol: "META", realized_today: 2.26 })],
    },
  };

  const rows = realizedBySymbol(panel);
  assert.equal(rows.NVDA?.realized_today, 5.69);
  assert.equal(rows.META?.realized_today, 2.26);
  assert.deepEqual(realizedBySymbol(null), {});
});

test("only CLEAN is green", () => {
  assert.equal(statusTone("CLEAN"), "POSITIVE");
  assert.equal(statusTone("DEGRADED"), "ATTENTION");
  assert.equal(statusTone("MISMATCH"), "NEGATIVE");
  assert.equal(statusTone("UNKNOWN"), "ATTENTION");
  assert.equal(statusTone(null), "MUTED");
});
