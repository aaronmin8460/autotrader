/**
 * The risk view: the deployed policy against the broker's now.
 *
 * The defect pinned here: a ~9% position and a ~90% book rendered red against
 * 5% and 30% lines that belong to a different runtime's constants. Under the
 * fractional policy the panel must show 11% and 95% as hard caps, 9% and 90%
 * as targets, read ON TARGET in green for the healthy allocation, and never
 * carry the stale figures.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { test } from "node:test";

import type { PolicyPanel } from "./paper.ts";
import { buildRiskView, carriesStaleLegacyLimit } from "./risk.ts";
import type { Amount, PositionRow, PositionsPanel, PrimaryMetrics, RiskLimit } from "./types.ts";

const here = dirname(fileURLToPath(import.meta.url));
const source = (relative: string): string => readFileSync(join(here, "..", relative), "utf8");

const of = (value: number): Amount => ({ value, available: true, unavailable_reason: null });
const missing: Amount = { value: null, available: false, unavailable_reason: "BROKER_UNREADABLE" };

/** The deployed policy as the paper API reports it. */
const FRACTIONAL: PolicyPanel = {
  policy_id: "EDA1_FRACTIONAL_RESERVED_90",
  config_hash: "e081e1f6bad9",
  source: "RUNTIME_START_EVENT",
  authoritative: true,
  target_gross: 0.9,
  hard_gross_cap: 0.95,
  hard_symbol_cap: 0.11,
  cash_reserve_target: 0.1,
  target_slot_weight: 0.09,
  universe_size: 10,
  fractional: true,
  daily_loss_halt: 0.02,
  note: "",
};

function position(symbol: string, value: number): PositionRow {
  return {
    symbol,
    asset_class: "EQUITY",
    quantity: "1",
    price: value,
    market_value: value,
    average_entry_price: value,
    unrealized_pnl: 0,
    unrealized_pnl_fraction: 0,
    updated_at: "2026-09-02T17:33:04+00:00",
    source: "BROKER",
  };
}

/** The account at audit time: ten ~9% positions, 89.96% deployed, 10.04% cash. */
function metrics(overrides: Partial<PrimaryMetrics> = {}): PrimaryMetrics {
  return {
    equity: of(99860.74),
    cash: of(10023.37),
    daily_pnl: of(331.49),
    daily_pnl_fraction: 0.0033,
    daily_pnl_baseline: of(99529.25),
    daily_pnl_baseline_date: "2026-09-02",
    exposure: of(89837.37),
    exposure_fraction: 0.8996,
    ...overrides,
  };
}

function positions(values: number[] = [8944.5, 8969.11, 9040.29, 8972.47, 8998.16, 8963.6, 8948.59, 8974.32, 8990.1, 9036.23]): PositionsPanel {
  return {
    source: "BROKER",
    as_of: "2026-09-02T17:33:04+00:00",
    rows: values.map((value, index) => position(`S${index}`, value)),
    flat_symbols: [],
    unavailable_reason: null,
    note: null,
  };
}

const dailyLoss: RiskLimit = {
  key: "daily_loss",
  label: "UTC daily loss halt",
  limit_fraction: 0.02,
  limit_value: of(1990.59),
  used_value: of(0),
  used_fraction: 0,
  utilization: 0,
  breached: false,
  subject: null,
  detail: null,
};

test("the healthy 90% book reads ON TARGET in green against the 95% cap", () => {
  const view = buildRiskView(metrics(), positions(), FRACTIONAL, dailyLoss);
  const total = view.rows.find((row) => row.key === "total")!;
  assert.equal(total.status, "ON TARGET");
  assert.equal(total.tone, "POSITIVE");
  assert.equal(total.target, 0.9);
  assert.equal(total.cap, 0.95);
  assert.ok(Math.abs((total.current ?? 0) - 0.8996) < 0.001);
  assert.equal(view.policyId, "EDA1_FRACTIONAL_RESERVED_90");
  assert.equal(view.authoritative, true);
});

test("a ~9% position reads ON TARGET in green against the 11% symbol cap", () => {
  const view = buildRiskView(metrics(), positions(), FRACTIONAL, dailyLoss);
  const symbol = view.rows.find((row) => row.key === "symbol")!;
  assert.equal(symbol.status, "ON TARGET");
  assert.equal(symbol.tone, "POSITIVE");
  assert.equal(symbol.target, 0.09);
  assert.equal(symbol.cap, 0.11);
  assert.equal(symbol.subject, "S2");
  assert.ok(Math.abs((symbol.current ?? 0) - 9040.29 / 99860.74) < 1e-9);
});

test("the cash reserve reads ON TARGET at ~10%", () => {
  const cash = buildRiskView(metrics(), positions(), FRACTIONAL, dailyLoss).rows.find((row) => row.key === "cash")!;
  assert.equal(cash.status, "ON TARGET");
  assert.equal(cash.target, 0.1);
  assert.equal(cash.cap, null);
});

test("the stale 5% and 30% limits never appear under the fractional policy", () => {
  const view = buildRiskView(metrics(), positions(), FRACTIONAL, dailyLoss);
  for (const row of view.rows) {
    assert.notEqual(row.cap, 0.05, row.key);
    assert.notEqual(row.cap, 0.3, row.key);
    assert.notEqual(row.target, 0.05, row.key);
    assert.notEqual(row.target, 0.3, row.key);
  }
  assert.equal(carriesStaleLegacyLimit(view), false);
  assert.equal(carriesStaleLegacyLimit({ ...view, rows: [{ ...view.rows[1]!, cap: 0.3 }] }), true);
});

test("only an actual hard-cap breach is red", () => {
  const over = buildRiskView(metrics({ exposure: of(96000), exposure_fraction: 0.9613 }), positions(), FRACTIONAL, dailyLoss);
  assert.equal(over.rows.find((row) => row.key === "total")!.status, "OVER CAP");
  assert.equal(over.rows.find((row) => row.key === "total")!.tone, "NEGATIVE");

  const near = buildRiskView(metrics({ exposure: of(94500), exposure_fraction: 0.9463 }), positions(), FRACTIONAL, dailyLoss);
  assert.equal(near.rows.find((row) => row.key === "total")!.status, "NEAR CAP");
  assert.equal(near.rows.find((row) => row.key === "total")!.tone, "ATTENTION");

  const symbolOver = buildRiskView(metrics(), positions([12000, 8900, 8900]), FRACTIONAL, dailyLoss);
  assert.equal(symbolOver.rows.find((row) => row.key === "symbol")!.status, "OVER CAP");
  assert.equal(symbolOver.rows.find((row) => row.key === "symbol")!.tone, "NEGATIVE");
});

test("below target is neutral, not a fault", () => {
  const defensive = buildRiskView(metrics({ exposure: of(45000), exposure_fraction: 0.4506, cash: of(54000) }), positions([9000, 9000, 9000, 9000, 9000]), FRACTIONAL, dailyLoss);
  const total = defensive.rows.find((row) => row.key === "total")!;
  assert.equal(total.status, "BELOW TARGET");
  assert.equal(total.tone, "NEUTRAL");
  assert.equal(defensive.rows.find((row) => row.key === "cash")!.status, "ABOVE TARGET");
});

test("without a policy no limit is shown and the reason is stated", () => {
  const view = buildRiskView(metrics(), positions(), null, dailyLoss);
  assert.equal(view.rows.length, 0);
  assert.equal(view.available, false);
  assert.match(view.note, /could not be read/);
  assert.equal(view.dailyLoss, dailyLoss);
});

test("a fallback policy is shown but flagged as not authoritative", () => {
  const view = buildRiskView(metrics(), positions(), { ...FRACTIONAL, authoritative: false, source: "FALLBACK_REGISTRY_DEFAULT" }, dailyLoss);
  assert.equal(view.authoritative, false);
  assert.match(view.note, /not the runtime's own/);
});

test("an unreadable broker leaves every row UNAVAILABLE rather than zero", () => {
  const view = buildRiskView(metrics({ equity: missing, cash: missing, exposure: missing, exposure_fraction: null }), null, FRACTIONAL, dailyLoss);
  for (const row of view.rows) {
    assert.equal(row.status, "UNAVAILABLE");
    assert.equal(row.current, null);
    assert.equal(row.rail, null);
  }
  assert.equal(view.available, false);
});

test("the risk, metrics and paper components carry no stale limit text", () => {
  // Dashboard V3 split Metrics.tsx into AccountSummary.tsx and added the
  // market-state panel; both render policy figures and both are guarded.
  for (const file of [
    "components/Risk.tsx",
    "components/AccountSummary.tsx",
    "components/MarketState.tsx",
    "components/Portfolio.tsx",
    "components/EquityPaper.tsx",
    "components/charts/ExposureRail.tsx",
    "lib/risk.ts",
    "lib/rail.ts",
    "app/risk/page.tsx",
  ]) {
    const text = source(file);
    assert.ok(!/(^|[^0-9.])30%/.test(text), `${file} names a 30% limit`);
    assert.ok(!/(^|[^0-9.])5%/.test(text), `${file} names a 5% limit`);
    assert.ok(!text.includes("V0.2"), `${file} names the V0.2 policy`);
    assert.ok(!text.includes("One account, one 30% cap"), file);
    assert.ok(!text.includes("30% limit"), file);
  }
});
