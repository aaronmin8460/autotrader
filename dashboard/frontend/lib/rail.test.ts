/**
 * The rail's axis, and the mistake it exists to prevent.
 *
 * "A big number is not a breach." A 9% position under an 11% cap is healthy,
 * and the rail must not draw it as nine tenths red.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { railDomain } from "./rail.ts";

test("a small cap gets a small axis, so the breach band stays a band", () => {
  // The deployed policy's per-symbol figures.
  const max = railDomain(0.0905, 0.09, 0.11);
  assert.ok(max < 0.2, `axis ${max} is far larger than the limit it describes`);
  // The cap sits well inside the axis, leaving a visible past-the-cap region.
  const capAt = 0.11 / max;
  assert.ok(capAt > 0.6 && capAt < 0.9, `cap sits at ${capAt} of the rail`);
  // And the current value is nowhere near the red.
  assert.ok(0.0905 / max < capAt);
});

test("an account-wide cap keeps the full 0-100% axis", () => {
  const max = railDomain(0.9015, 0.9, 0.95);
  assert.equal(max, 1);
});

test("the axis never exceeds 100% of equity", () => {
  assert.equal(railDomain(0.99, 0.95, 0.95), 1);
  assert.equal(railDomain(1, 1, 1), 1);
});

test("a row with no cap is scaled from its target", () => {
  // Cash reserve: target 10%, no hard cap.
  const max = railDomain(0.0985, 0.1, null);
  assert.ok(max > 0.1 && max <= 0.2, `axis ${max}`);
});

test("an unreadable row still yields a usable axis", () => {
  assert.ok(railDomain(null, null, null) > 0);
  assert.equal(railDomain(null, null, 0.11), Math.min(1, 0.11 * 1.3));
});
