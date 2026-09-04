/**
 * Data freshness: four states that must never look like one another.
 *
 * "No data yet", "the backend is gone", "this number is old" and "this number
 * is current" are four different operational facts, and the whole reason the
 * indicator exists is that V2 showed the last three identically.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { FRESHNESS_INTERVALS, FRESHNESS_TIMEOUT_MS, freshnessOf } from "./freshness.ts";

const NOW = Date.parse("2026-09-03T18:05:39Z");
const INTERVAL = 5_000;

function at(secondsAgo: number): string {
  return new Date(NOW - secondsAgo * 1000).toISOString();
}

test("a payload from this cycle is fresh", () => {
  const result = freshnessOf(at(2), true, INTERVAL, NOW);
  assert.equal(result.state, "FRESH");
  assert.equal(result.ageSeconds, 2);
});

test("one missed poll is not an alarm", () => {
  const withinBudget = (INTERVAL * FRESHNESS_INTERVALS + FRESHNESS_TIMEOUT_MS) / 1000 - 1;
  assert.equal(freshnessOf(at(withinBudget), true, INTERVAL, NOW).state, "FRESH");
});

test("past the derived threshold the payload is stale, not absent", () => {
  const beyond = (INTERVAL * FRESHNESS_INTERVALS + FRESHNESS_TIMEOUT_MS) / 1000 + 1;
  const result = freshnessOf(at(beyond), true, INTERVAL, NOW);
  assert.equal(result.state, "STALE");
  assert.equal(result.ageSeconds, Math.round(beyond));
});

test("a dropped connection reads OFFLINE and keeps the age of what it has", () => {
  const result = freshnessOf(at(3), false, INTERVAL, NOW);
  assert.equal(result.state, "OFFLINE");
  assert.equal(result.ageSeconds, 3);
});

test("before the first payload the state is WAITING, never a zero", () => {
  assert.deepEqual(freshnessOf(null, true, INTERVAL, NOW), { state: "WAITING", ageSeconds: null });
  assert.deepEqual(freshnessOf(undefined, false, INTERVAL, NOW), {
    state: "OFFLINE",
    ageSeconds: null,
  });
});

test("an unparseable stamp is not silently treated as current", () => {
  assert.equal(freshnessOf("not-a-date", true, INTERVAL, NOW).state, "WAITING");
});

test("a slower record gets a proportionally longer budget", () => {
  const paper = 15_000;
  const fortySeconds = at(40);
  assert.equal(freshnessOf(fortySeconds, true, INTERVAL, NOW).state, "STALE");
  assert.equal(freshnessOf(fortySeconds, true, paper, NOW).state, "FRESH");
});
