/**
 * Fill markers: only real, filled, EQUITY PAPER orders reach a chart.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { fillsFor, fillsWithin } from "./fills.ts";
import type { AccountOrderRow, AccountOrdersPanel } from "./orders.ts";

function order(overrides: Partial<AccountOrderRow>): AccountOrderRow {
  return {
    client_order_id: "autotrader-x",
    broker_order_id: "b",
    source: "EQUITY PAPER",
    simulated: false,
    symbol: "META",
    asset_class: "EQUITY",
    side: "BUY",
    quantity: "0.17",
    filled_quantity: "0.17",
    average_fill_price: 593.15,
    status: "FILLED",
    status_tone: "POSITIVE",
    status_source: "BROKER",
    needs_attention: false,
    risk_reason_code: "APPROVED",
    created_at: "2026-09-02T16:02:00+00:00",
    submitted_at: "2026-09-02T16:03:53+00:00",
    filled_at: "2026-09-02T16:03:53+00:00",
    authoritative_at: "2026-09-02T16:03:53+00:00",
    ...overrides,
  };
}

const panel: AccountOrdersPanel = {
  generated_at: "2026-09-02T17:40:00+00:00",
  rows: [
    order({ client_order_id: "a" }),
    order({ client_order_id: "b", side: "SELL", filled_at: "2026-09-02T14:17:35+00:00", submitted_at: "2026-09-02T14:17:35+00:00" }),
    order({ client_order_id: "c", status: "UNKNOWN", status_tone: "ATTENTION", filled_at: null, submitted_at: null }),
    order({ client_order_id: "d", source: "CRYPTO PAPER", symbol: "META" }),
    order({ client_order_id: "e", simulated: true }),
    order({ client_order_id: "f", symbol: "NVDA" }),
  ],
  total: 6,
  attention_count: 1,
  stores: [],
  duplicates_dropped: 0,
  includes_simulated: false,
  note: "",
  limit: 25,
};

test("only filled EQUITY PAPER rows for the symbol become markers", () => {
  const fills = fillsFor(panel, "META");
  assert.deepEqual(fills.map((fill) => fill.side), ["BUY", "SELL"]);
  assert.match(fills[0]!.label, /EQUITY PAPER fill/);
  assert.equal(fills[0]!.price, 593.15);
  assert.deepEqual(fillsFor(null, "META"), []);
});

test("a simulated row can never become a marker even if the payload carried one", () => {
  const simulatedOnly: AccountOrdersPanel = { ...panel, rows: [order({ simulated: true })] };
  assert.deepEqual(fillsFor(simulatedOnly, "META"), []);
});

test("markers are limited to the chart's own window", () => {
  const fills = fillsFor(panel, "META");
  assert.equal(fillsWithin(fills, "2026-09-02T15:00:00+00:00", "2026-09-02T17:00:00+00:00").length, 1);
  assert.equal(fillsWithin(fills, "2026-09-02T13:30:00+00:00", "2026-09-02T17:00:00+00:00").length, 2);
  assert.equal(fillsWithin(fills, null, null).length, 0);
});
