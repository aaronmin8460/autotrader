/**
 * Portfolio arithmetic: weights, allocation, contribution, target vs actual.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import type { PaperTargetRow } from "./paper.ts";
import { allocationSlices, pnlContributions, positionWeights, targetVsActual, weightOf } from "./portfolio.ts";
import type { Amount, PositionRow, PositionsPanel, PrimaryMetrics } from "./types.ts";

const of = (value: number): Amount => ({ value, available: true, unavailable_reason: null });

function position(symbol: string, value: number, pnl: number | null = 0, assetClass: "EQUITY" | "CRYPTO" = "EQUITY"): PositionRow {
  return {
    symbol,
    asset_class: assetClass,
    quantity: "2",
    price: value / 2,
    market_value: value,
    average_entry_price: value / 2,
    unrealized_pnl: pnl,
    unrealized_pnl_fraction: pnl === null ? null : pnl / value,
    updated_at: "2026-09-02T17:33:04+00:00",
    source: "BROKER",
  };
}

function panel(rows: PositionRow[]): PositionsPanel {
  return { source: "BROKER", as_of: null, rows, flat_symbols: [], unavailable_reason: null, note: null };
}

const metrics: PrimaryMetrics = {
  equity: of(100000),
  cash: of(10000),
  daily_pnl: of(0),
  daily_pnl_fraction: 0,
  daily_pnl_baseline: of(100000),
  daily_pnl_baseline_date: null,
  exposure: of(90000),
  exposure_fraction: 0.9,
};

function target(symbol: string, overrides: Partial<PaperTargetRow> = {}): PaperTargetRow {
  return {
    symbol,
    in_execution_universe: true,
    bar_timestamp: "2026-09-02T17:15:00+00:00",
    participate: true,
    eda1_signal: "HOLD",
    eda1_stance: 1,
    v3_signal: "HOLD",
    stances_agree: true,
    reference_close: 100,
    actual_quantity: "2",
    last_risk_reason: "APPROVED",
    stance_label: "LONG",
    target_weight: 0.09,
    target_source: "RECORDED_DECISION",
    target_decided_at: "2026-09-02T16:02:00+00:00",
    last_order_side: "BUY",
    action: "HOLD",
    ...overrides,
  };
}

test("weight is market value over equity, and null when either side is unknown", () => {
  assert.equal(weightOf(position("AAPL", 9000), 100000), 0.09);
  assert.equal(weightOf(position("AAPL", 9000), null), null);
  assert.equal(weightOf({ ...position("AAPL", 9000), market_value: null }, 100000), null);
  assert.deepEqual(positionWeights(panel([position("AAPL", 9000), position("MSFT", 4500)]), 100000), { AAPL: 0.09, MSFT: 0.045 });
});

test("the allocation lists positions largest first, then cash, with fractions of equity", () => {
  const slices = allocationSlices(panel([position("AAPL", 8000), position("BTC/USD", 2000, 0, "CRYPTO"), position("NVDA", 9000)]), metrics);
  assert.deepEqual(slices.map((slice) => slice.label), ["NVDA", "AAPL", "BTC/USD", "Cash"]);
  assert.deepEqual(slices.map((slice) => slice.kind), ["EQUITY", "EQUITY", "CRYPTO", "CASH"]);
  assert.equal(slices[3]!.fraction, 0.1);
  assert.equal(slices[0]!.fraction, 0.09);
});

test("too many positions fold into one Other slice so the labels stay readable", () => {
  const rows = Array.from({ length: 20 }, (_, index) => position(`S${index}`, 1000 + index));
  const slices = allocationSlices(panel(rows), metrics);
  assert.equal(slices.length, 15);
  assert.match(slices[13]!.label, /^Other \(7\)$/);
  assert.equal(slices[14]!.label, "Cash");
});

test("contributions are the broker's unrealized P&L, sorted, and skip unknowns", () => {
  const rows = pnlContributions(panel([position("A", 1000, -50), position("B", 1000, 120), position("C", 1000, null)]));
  assert.deepEqual(rows.map((row) => row.symbol), ["B", "A"]);
  assert.equal(rows[0]!.pnl, 120);
});

test("target vs actual joins the recorded decision to the broker position", () => {
  const rows = targetVsActual(
    [target("AAPL"), target("TSLA", { stance_label: "FLAT", target_weight: 0, target_source: "STANCE_FLAT", action: "SELL" }), target("GOOGL", { target_weight: null, target_source: "NOT_RECORDED" })],
    panel([position("AAPL", 9040.29), position("GOOGL", 9000)]),
    metrics,
  );
  const aapl = rows.find((row) => row.symbol === "AAPL")!;
  assert.equal(aapl.target_weight, 0.09);
  assert.equal(aapl.target_value, 9000);
  assert.equal(aapl.actual_value, 9040.29);
  assert.ok(Math.abs(aapl.actual_weight! - 0.0904029) < 1e-6);
  assert.ok(Math.abs(aapl.delta_value! - 40.29) < 1e-6);
  assert.equal(aapl.action, "HOLD");

  const tsla = rows.find((row) => row.symbol === "TSLA")!;
  assert.equal(tsla.stance, "FLAT");
  assert.equal(tsla.target_weight, 0);
  assert.equal(tsla.actual_value, 0);
  assert.equal(tsla.action, "SELL");

  const googl = rows.find((row) => row.symbol === "GOOGL")!;
  assert.equal(googl.target_weight, null);
  assert.equal(googl.target_value, null);
  assert.equal(googl.delta_value, null);
  assert.equal(googl.target_source, "NOT_RECORDED");
});

test("without a broker read the actual side is unknown, never zero", () => {
  const rows = targetVsActual([target("AAPL")], null, null);
  assert.equal(rows[0]!.actual_weight, null);
  assert.equal(rows[0]!.target_value, null);
});
