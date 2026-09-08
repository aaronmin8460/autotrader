/**
 * Money that never becomes a JavaScript number.
 *
 * The frozen contract requires exact decimal strings parsed with an exact
 * decimal type. These tests pin the two properties that matter: the formatter
 * agrees with a calculator on ordinary figures, and it does **not** agree with
 * `Number` on the figures where `Number` is wrong.
 *
 * The second half is the point. `0.1 + 0.2` arithmetic is the reason the
 * contract states the rule, and a formatter that routed `"1.005"` through a
 * float would round it down while every human reading the contract expects it
 * to round up.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { DASH as FORMAT_DASH } from "./format.ts";
import {
  DASH,
  compareDecimals,
  contractMoney,
  contractPercent,
  contractSignedMoney,
  contractSignedPercent,
  contractTone,
  isZero,
  parseDecimal,
  signOf,
} from "./live-decimal.ts";

const here = dirname(fileURLToPath(import.meta.url));

test("the Live dash is the same character the rest of the application uses", () => {
  // `live-decimal` declares its own so it can stay dependency-free and load
  // under `node --test`. This is what stops the two from drifting apart.
  assert.equal(DASH, FORMAT_DASH);
});

test("ordinary money renders the way an operator expects", () => {
  assert.equal(contractMoney("50.00"), "$50.00");
  assert.equal(contractMoney("0.00"), "$0.00");
  assert.equal(contractMoney("103.00"), "$103.00");
  assert.equal(contractMoney("1234567.89"), "$1,234,567.89");
  assert.equal(contractMoney("-10.00"), "-$10.00");
  assert.equal(contractMoney("5.5"), "$5.50");
  assert.equal(contractMoney("11"), "$11.00");
});

test("an unknown figure is the dash, and is never zero", () => {
  // The contract: "null means unknown, and must be displayed as unknown. It
  // never means zero. A dash is correct; $0.00 is a lie."
  assert.equal(contractMoney(null), DASH);
  assert.equal(contractMoney(undefined), DASH);
  assert.equal(contractMoney(""), DASH);
  assert.equal(contractMoney("   "), DASH);
  assert.notEqual(contractMoney(null), "$0.00");
});

test("nothing ever renders as NaN, Infinity, undefined or null", () => {
  const hostile = ["NaN", "Infinity", "-Infinity", "undefined", "null", "abc", "1e2", "0x10", "--1", "1.2.3"];
  for (const value of hostile) {
    for (const render of [contractMoney, contractSignedMoney, contractPercent, contractSignedPercent]) {
      const text = render(value);
      assert.equal(text, DASH, `${value} via ${render.name}`);
      for (const forbidden of ["NaN", "Infinity", "undefined", "null"]) {
        assert.ok(!text.includes(forbidden), `${render.name}(${value}) leaked ${forbidden}`);
      }
    }
  }
});

test("exponent notation is refused rather than expanded", () => {
  // The contract says money is "never exponent notation", so a payload
  // carrying `1e2` has already departed from it. Interpreting it would hide
  // the drift the contract test exists to catch.
  assert.equal(contractMoney("1e2"), DASH);
  assert.equal(contractMoney("1E2"), DASH);
});

test("a signed figure always carries its sign, and zero carries none", () => {
  assert.equal(contractSignedMoney("3.00"), "+$3.00");
  assert.equal(contractSignedMoney("-10.00"), "-$10.00");
  assert.equal(contractSignedMoney("0.00"), "$0.00");
  assert.equal(contractSignedMoney("-0.00"), "$0.00", "a signed zero reads as a formatting bug");
});

test("a ratio becomes a percentage by moving the point, not by multiplying", () => {
  assert.equal(contractPercent("0.06"), "6.00%");
  assert.equal(contractPercent("0.02"), "2.00%");
  assert.equal(contractPercent("0.20"), "20.00%");
  assert.equal(contractPercent("0.20", 0), "20%");
  assert.equal(contractPercent("0.00"), "0.00%");
  assert.equal(contractPercent("1.00"), "100.00%");
  assert.equal(contractPercent("-0.086207"), "-8.62%");
  assert.equal(contractSignedPercent("0.06"), "+6.00%");
  assert.equal(contractSignedPercent("-0.086207"), "-8.62%");
  assert.equal(contractSignedPercent("0.00"), "0.00%");
});

test("the point shift is exact where a float multiply is not", () => {
  // Real divergences, not illustrative ones. `0.0007 * 100` is
  // 0.06999999999999999 in binary floating point and `0.0035 * 100` is
  // 0.35000000000000003. Both round to the right answer at two decimal places,
  // which is exactly why this defect is easy to ship: it hides at display
  // precision and surfaces deeper.
  assert.equal((0.0007 * 100).toFixed(17), "0.06999999999999999");
  assert.equal((0.0035 * 100).toFixed(17), "0.35000000000000003");

  // The digit shift carries neither error, at any precision.
  assert.equal(contractPercent("0.0007", 17), "0.07000000000000000%");
  assert.equal(contractPercent("0.0035", 17), "0.35000000000000000%");
  assert.equal(contractPercent("0.0007"), "0.07%");
  assert.equal(contractPercent("0.0035"), "0.35%");
});

test("rounding is half away from zero, computed on the digits", () => {
  // The float route gets these wrong: 1.005 and 2.675 are not representable,
  // so `Number(...).toFixed(2)` rounds both DOWN, against every reader's
  // expectation and against the backend's own value.
  assert.equal(Number("1.005").toFixed(2), "1.00", "the float route rounds down");
  assert.equal(Number("2.675").toFixed(2), "2.67", "and again");
  assert.equal(contractMoney("1.005"), "$1.01", "the digit route rounds up, as a reader expects");
  assert.equal(contractMoney("2.675"), "$2.68");
  assert.equal(contractMoney("0.994"), "$0.99");
  assert.equal(contractMoney("0.995"), "$1.00", "a carry crosses the point");
  assert.equal(contractMoney("9.999"), "$10.00", "a carry extends the integer part");
  assert.equal(contractMoney("99.999"), "$100.00");
  assert.equal(contractMoney("-1.005"), "-$1.01");
});

test("a large exact figure survives that a float would not", () => {
  assert.equal(contractMoney("9007199254740993.01"), "$9,007,199,254,740,993.01");
});

test("a move too small to survive rounding reads as almost nothing, not as zero", () => {
  assert.equal(contractSignedPercent("0.000001"), "~0.00%");
  assert.equal(contractSignedPercent("-0.000001"), "~0.00%");
});

test("parsing, zero and sign agree on the awkward cases", () => {
  assert.equal(signOf(parseDecimal("0.00")), 0);
  assert.equal(signOf(parseDecimal("-0.00")), 0, "negative zero is flat, not a loss");
  assert.equal(signOf(parseDecimal("-0.01")), -1);
  assert.equal(signOf(parseDecimal("0.01")), 1);
  assert.ok(isZero(parseDecimal("0")));
  assert.ok(isZero(parseDecimal("-0.0000")));
  assert.ok(!isZero(parseDecimal("0.0001")));
  assert.equal(parseDecimal("007.50")?.int, "7");
  assert.equal(parseDecimal(".5")?.frac, "5");
});

test("tone follows the value, and unknown is muted rather than neutral", () => {
  assert.equal(contractTone("3.00"), "POSITIVE");
  assert.equal(contractTone("-3.00"), "NEGATIVE");
  assert.equal(contractTone("0.00"), "NEUTRAL");
  assert.equal(contractTone(null), "MUTED", "unknown and flat are different states");
});

test("comparison orders decimals without converting them", () => {
  assert.equal(compareDecimals("53.00", "58.00"), -1);
  assert.equal(compareDecimals("58.00", "53.00"), 1);
  assert.equal(compareDecimals("53.00", "53.0"), 0);
  assert.equal(compareDecimals("-10.00", "-2.00"), -1, "a bigger negative magnitude is smaller");
  assert.equal(compareDecimals("-0.00", "0.00"), 0);
  assert.equal(compareDecimals("100.00", null), null);
});

test("the module never converts a contract figure through Number", () => {
  // The rule is structural, so it is checked structurally: with prose stripped,
  // no float-parsing call appears in the executable code of this module.
  const source = readFileSync(join(here, "live-decimal.ts"), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/.*$/gm, "");
  // `Number(...)` appears twice, on a SINGLE character each time, to compare
  // and increment one digit — a conversion that is exact for 0-9 and is not a
  // parse of a money string. What must never appear is a float parse of a
  // whole figure.
  for (const forbidden of ["parseFloat", "parseInt", "toFixed", "Number(text", "Number(trimmed", "valueOf", "toLocaleString"]) {
    assert.ok(!source.includes(forbidden), `live-decimal uses ${forbidden}`);
  }
  const wholeValueConversions = source.match(/Number\((?!value\.frac\.charAt|digitsOut\[)/g) ?? [];
  assert.deepEqual(wholeValueConversions, [], "a whole contract figure was converted through Number");
});
