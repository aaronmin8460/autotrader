/**
 * Chart helpers: batching, labels, and the sparkline path.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { chartUnavailableLabel, chunk, normalizeSymbols } from "./chart-util.ts";
import { sparkPath } from "./spark.ts";

test("a wide symbol set is chunked to the backend's per-request ceiling", () => {
  const symbols = Array.from({ length: 27 }, (_, index) => `S${index}`);
  const groups = chunk(symbols, 12);
  assert.deepEqual(groups.map((group) => group.length), [12, 12, 3]);
  assert.deepEqual(chunk([], 12), []);
});

test("symbols are upper-cased, de-duplicated and sorted so the cache key is stable", () => {
  assert.deepEqual(normalizeSymbols(["nvda", " SPY", "spy", "", "btc/usd"]), ["BTC/USD", "NVDA", "SPY"]);
});

test("every unavailable reason has words", () => {
  assert.equal(chartUnavailableLabel("NO_BARS"), "No bars for this range");
  assert.equal(chartUnavailableLabel("PROVIDER_BUDGET_EXHAUSTED"), "Chart budget exhausted; retry later");
  assert.equal(chartUnavailableLabel("BROKER_NOT_CONFIGURED"), "No market-data credentials");
  assert.equal(chartUnavailableLabel(null), "Chart unavailable");
});

test("the sparkline path spans the width and inverts the price axis", () => {
  const path = sparkPath([1, 2, 3], 96, 26);
  assert.match(path, /^M0\.0 24\.0 L48\.0 13\.0 L96\.0 2\.0$/);
  assert.equal(sparkPath([1], 96, 26), "");
  assert.equal(sparkPath([5, 5], 96, 26), "M0.0 24.0 L96.0 24.0");
});
