/**
 * Focused tests for the formatting rules that would silently misreport.
 *
 * Run with `npm test`, which uses Node's own test runner and type stripping -
 * no test framework is installed for this. The rest of the frontend is covered
 * by `npm run lint`, `npm run typecheck`, and `npm run build`.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DASH,
  amount,
  clockUtc,
  money,
  percent,
  quantity,
  relative,
  signTone,
  signedMoney,
  stampUtc,
  unavailableLabel,
} from "./format.ts";

test("an unavailable figure never renders as a number", () => {
  assert.equal(money(null), DASH);
  assert.equal(signedMoney(undefined), DASH);
  assert.equal(percent(null), DASH);
  assert.equal(money(Number.NaN), DASH);
  assert.equal(amount({ value: null, available: false, unavailable_reason: "NOT_RECORDED" }), DASH);
});

test("an available amount renders its value", () => {
  assert.equal(amount({ value: 99999.95, available: true, unavailable_reason: null }), "$99,999.95");
});

test("a signed figure always carries its sign, and zero carries none", () => {
  assert.equal(signedMoney(12.3), "+$12.30");
  assert.equal(signedMoney(-0.05), "-$0.05");
  assert.equal(signedMoney(0), "$0.00");
});

test("zero is neither a gain nor a loss", () => {
  assert.equal(signTone(0), "NEUTRAL");
  assert.equal(signTone(1), "POSITIVE");
  assert.equal(signTone(-1), "NEGATIVE");
  assert.equal(signTone(null), "NEUTRAL");
});

test("an exact quantity is passed through, trailing zeros and all", () => {
  assert.equal(quantity("0.000167050"), "0.000167050");
  assert.equal(quantity(null), DASH);
});

test("times render in UTC, not the viewer's timezone", () => {
  assert.equal(clockUtc("2026-08-28T16:43:22.987681+00:00"), "16:43:22");
  assert.equal(clockUtc("2026-08-28T18:43:22+02:00"), "16:43:22");
  assert.equal(clockUtc("not a date"), DASH);
});

test("a stamp drops the date only when it matches the reference day", () => {
  const today = "2026-08-28T17:00:00+00:00";
  assert.equal(stampUtc("2026-08-28T16:43:22+00:00", today), "16:43:22");
  assert.equal(stampUtc("2026-08-27T22:00:24+00:00", today), "27 Aug 22:00:24");
});

test("relative ages read forwards and backwards", () => {
  const now = "2026-08-28T17:00:00+00:00";
  assert.equal(relative("2026-08-28T16:58:00+00:00", now), "2m ago");
  assert.equal(relative("2026-08-28T17:15:00+00:00", now), "in 15m");
  assert.equal(relative(null, now), DASH);
});

test("every unavailable reason has words, including an unknown one", () => {
  assert.equal(unavailableLabel("BROKER_NOT_CONFIGURED"), "No broker credentials");
  assert.equal(unavailableLabel("DATABASE_UNREADABLE"), "Database unreadable");
  assert.equal(unavailableLabel(null), "Unavailable");
});
