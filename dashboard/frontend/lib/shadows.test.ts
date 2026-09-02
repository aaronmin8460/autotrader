/**
 * The Shadows comparison: raw counts always, performance figures only when
 * each observer's own sample threshold says they mean something.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import type { A1BOverview } from "./a1b.ts";
import type { ShadowOverview } from "./shadow.ts";
import { INSUFFICIENT, compareShadows } from "./shadows.ts";

function eda1(sufficient: boolean): ShadowOverview {
  return {
    generated_at: "2026-09-02T17:40:00+00:00",
    read_only: true,
    observation_only: true,
    hypothetical_label: "SIMULATED / SHADOW - NO REAL ORDERS",
    service: { universe: Array.from({ length: 10 }, (_, index) => `S${index}`), status: "RUNNING", cycles_recorded: 61 } as ShadowOverview["service"],
    regime: { state: "PARTICIPATE" } as ShadowOverview["regime"],
    symbols: [],
    hypothetical: {
      label: "SIMULATED / SHADOW - NO REAL ORDERS",
      normalized_start: 100,
      steps: 60,
      first_bar: "2026-08-31T13:30:00+00:00",
      last_bar: "2026-09-02T17:15:00+00:00",
      costs_applied: false,
      v3: null,
      eda1: {
        engine: "eda1",
        portfolio_value: 101.2,
        cumulative_return: 0.012,
        max_drawdown: -0.004,
        long_exposure_fraction: 1,
        stance_changes: 3,
        turnover_per_step: 0.05,
        current_long_symbols: Array.from({ length: 10 }, (_, index) => `S${index}`),
        current_stance_summary: "LONG 10/10",
      },
      benchmark_return: 0.011,
      unavailable_reason: null,
    },
    comparison: { bars_compared: 610, steps: 60, stance_disagreement_count: 4, sample_is_sufficient: sufficient, sample_warning: "too small" } as ShadowOverview["comparison"],
  };
}

function a1b(sufficient: boolean): A1BOverview {
  return {
    generated_at: "2026-09-02T17:40:00+00:00",
    read_only: true,
    observation_only: true,
    hypothetical_label: "SIMULATED / SHADOW - NO REAL ORDERS",
    service: { universe_size: 26, status: "RUNNING", observations_recorded: 416 } as A1BOverview["service"],
    regime: { state: "PARTICIPATE" } as A1BOverview["regime"],
    symbols: [],
    hypothetical: {
      label: "SIMULATED / SHADOW - NO REAL ORDERS",
      normalized_start: 100,
      steps: 15,
      first_bar: "2026-09-02T13:30:00+00:00",
      last_bar: "2026-09-02T17:15:00+00:00",
      costs_applied: false,
      portfolio_value: 100.21,
      cumulative_return: 0.0021,
      max_drawdown: -0.0029,
      average_exposure: 1,
      current_exposure: 1,
      long_symbols: 26,
      weight_changes: 0,
      turnover_per_step: 0,
      benchmark_return: 0.0019,
      sample_warning: "A1-B sample too small",
      sample_is_sufficient: sufficient,
      capture_unavailable_reason: "SAMPLE_TOO_SMALL",
      unavailable_reason: null,
    },
    summary: { observations: 416, bars: 16 } as A1BOverview["summary"],
  };
}

const row = (comparison: ReturnType<typeof compareShadows>, key: string) => comparison.rows.find((candidate) => candidate.key === key)!;

test("performance rows read INSUFFICIENT while either sample is too small, with the raw figure beside", () => {
  const comparison = compareShadows(eda1(false), a1b(false));
  assert.equal(comparison.insufficient, true);
  for (const key of ["return", "drawdown", "benchmark"]) {
    const item = row(comparison, key);
    assert.equal(item.performance, true);
    assert.equal(item.eda1.text, INSUFFICIENT);
    assert.equal(item.a1b.text, INSUFFICIENT);
    assert.equal(item.eda1.conclusive, false);
    assert.ok(item.a1b.raw, "raw figure missing");
  }
  assert.equal(row(comparison, "return").a1b.raw, "+0.21%");
  assert.equal(comparison.warning, "A1-B sample too small");
});

test("raw observations are always shown", () => {
  const comparison = compareShadows(eda1(false), a1b(false));
  assert.equal(row(comparison, "steps").eda1.text, "60");
  assert.equal(row(comparison, "steps").a1b.text, "15");
  assert.equal(row(comparison, "decisions").a1b.text, "416");
  assert.equal(row(comparison, "universe").eda1.text, "10 symbols");
  assert.equal(row(comparison, "universe").a1b.text, "26 symbols");
  assert.equal(row(comparison, "exposure").a1b.text, "+100.0%");
  assert.equal(row(comparison, "disagreements").eda1.text, "4");
  assert.equal(row(comparison, "disagreements").a1b.text, "N/A · no counterpart");
  assert.equal(row(comparison, "regime").a1b.text, "PARTICIPATE");
  assert.match(row(comparison, "current").a1b.text, /long 26\/26/);
});

test("once both samples suffice the performance rows are numbers", () => {
  const comparison = compareShadows(eda1(true), a1b(true));
  assert.equal(comparison.insufficient, false);
  assert.equal(row(comparison, "return").eda1.text, "+1.20%");
  assert.equal(row(comparison, "return").a1b.text, "+0.21%");
  assert.equal(row(comparison, "drawdown").eda1.text, "-0.40%");
  assert.equal(row(comparison, "return").eda1.conclusive, true);
});

test("a missing observer renders dashes, never zeros", () => {
  const comparison = compareShadows(null, null);
  assert.equal(row(comparison, "steps").eda1.text, "—");
  assert.equal(row(comparison, "period").a1b.text, "—");
  assert.equal(row(comparison, "return").a1b.text, INSUFFICIENT);
  assert.equal(row(comparison, "return").a1b.raw, "—");
});
