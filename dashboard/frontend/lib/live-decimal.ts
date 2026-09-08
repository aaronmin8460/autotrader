/**
 * Money on the Live page, formatted without ever becoming a JavaScript number.
 *
 * The frozen accounting contract states the rule in one line: money is an exact
 * decimal **string**, and it is to be parsed "with an exact decimal type, not
 * `Number`". This module is that exact type, kept deliberately small - the page
 * displays these figures and never does arithmetic on them, so what is needed
 * is a parser, a comparison, and a formatter, and nothing else.
 *
 * `lib/format.ts` already formats money for the rest of the application and
 * takes a `number`. It is not used for contract figures and is not changed:
 * routing `"103.37"` through `money()` would work today, on these magnitudes,
 * and would be wrong in the way that only shows up later. A dashboard for an
 * account holding real money should not have a rounding story that begins "at
 * these balances it does not matter yet".
 *
 * The second rule this module enforces is the contract's other one: **`null`
 * means unknown and is never zero.** Every function here returns the house dash
 * for a `null`, an empty string, or anything that is not a decimal numeral. It
 * never returns `"$0.00"` for an absent value, and it never returns `NaN`,
 * `Infinity`, `undefined` or `null` as text.
 */

/**
 * What an unreadable figure looks like. One character, everywhere.
 *
 * Deliberately declared here rather than imported from `lib/format`, which
 * owns the same constant for the rest of the application. This module is
 * dependency-free on purpose — that is what lets `node --test` load it
 * directly, the same arrangement `chart-util` has — and one character is a
 * cheaper duplication than a runtime import. `live-decimal.test.ts` asserts
 * the two are the same character, so they cannot drift.
 */
export const DASH = "—";

/**
 * A decimal number held as text, split at the point.
 *
 * `int` and `frac` are digit strings with no sign and no separator. `neg`
 * carries the sign so that `-0.00` and `0.00` are distinguishable while parsing
 * and identical once rendered - a signed zero on screen reads as a formatting
 * bug, which is the same judgement `signedPercent` in `lib/format.ts` makes.
 */
export interface ExactDecimal {
  neg: boolean;
  int: string;
  frac: string;
}

/** Accepts `-12`, `0.06`, `+3.50`, `.5`, `7.` — and rejects everything else. */
const DECIMAL_PATTERN = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/;

/**
 * A contract decimal string, or `null` when it is not one.
 *
 * Exponent notation is rejected rather than expanded. The contract says money
 * is "never exponent notation", so a payload carrying `1e2` has already
 * departed from the contract, and quietly interpreting it would hide exactly
 * the drift the contract test exists to catch.
 */
export function parseDecimal(text: string | null | undefined): ExactDecimal | null {
  if (typeof text !== "string") return null;
  const trimmed = text.trim();
  if (trimmed === "" || !DECIMAL_PATTERN.test(trimmed)) return null;
  const neg = trimmed.startsWith("-");
  const unsigned = trimmed.replace(/^[+-]/, "");
  const [rawInt = "", rawFrac = ""] = unsigned.split(".");
  const int = rawInt.replace(/^0+(?=\d)/, "") || "0";
  return { neg, int, frac: rawFrac };
}

/** True when every digit is zero, whatever the sign or the scale. */
export function isZero(value: ExactDecimal | null): boolean {
  if (!value) return false;
  return !/[1-9]/.test(value.int + value.frac);
}

/** `-1`, `0` or `+1`. The sign of the value, with negative zero reading as `0`. */
export function signOf(value: ExactDecimal | null): -1 | 0 | 1 {
  if (!value || isZero(value)) return 0;
  return value.neg ? -1 : 1;
}

/**
 * Round to `digits` decimal places, half away from zero, entirely in text.
 *
 * Half away from zero rather than half to even because this is a display
 * rounding of a figure the backend has already decided, and matching what a
 * reader gets from a calculator is worth more here than statistical neutrality
 * over a series nobody is summing.
 */
function roundTo(value: ExactDecimal, digits: number): ExactDecimal {
  if (value.frac.length <= digits) {
    return { ...value, frac: value.frac.padEnd(digits, "0") };
  }
  const keep = value.frac.slice(0, digits);
  const roundUp = Number(value.frac.charAt(digits)) >= 5;
  if (!roundUp) return { ...value, frac: keep };

  // Increment the digit string `int + keep` by one, right to left, so a carry
  // can cross the decimal point without either half becoming a number.
  const digitsOut = (value.int + keep).split("");
  let index = digitsOut.length - 1;
  while (index >= 0) {
    if (digitsOut[index] === "9") {
      digitsOut[index] = "0";
      index -= 1;
    } else {
      digitsOut[index] = String(Number(digitsOut[index]) + 1);
      break;
    }
  }
  if (index < 0) digitsOut.unshift("1");
  const carried = digitsOut.join("");
  const cut = carried.length - digits;
  return {
    neg: value.neg,
    int: (carried.slice(0, cut) || "0").replace(/^0+(?=\d)/, "") || "0",
    frac: carried.slice(cut),
  };
}

/** `1234567` becomes `1,234,567`. Thousands separators, added textually. */
function group(int: string): string {
  return int.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/**
 * A contract money string as `$50.00`, or the dash when it is unknown.
 *
 * The dash is the answer for `null`, for an empty string, and for anything
 * unparseable. It is never `$0.00`: the contract is explicit that `null` means
 * unknown, and an operator cannot tell an empty account from an unreadable one
 * if both render as zero.
 */
export function contractMoney(text: string | null | undefined, digits = 2): string {
  const parsed = parseDecimal(text);
  if (!parsed) return DASH;
  const rounded = roundTo(parsed, digits);
  const magnitude = `$${group(rounded.int)}${digits > 0 ? `.${rounded.frac}` : ""}`;
  return signOf(rounded) < 0 ? `-${magnitude}` : magnitude;
}

/**
 * A contract money string as `+$3.00` / `-$10.00`, sign always present.
 *
 * Used for figures whose direction is the message - trading P&L, realized and
 * unrealized P&L, the new high-water-mark profit. An exact zero carries no
 * sign, because zero is neither a gain nor a loss.
 */
export function contractSignedMoney(text: string | null | undefined, digits = 2): string {
  const parsed = parseDecimal(text);
  if (!parsed) return DASH;
  const rounded = roundTo(parsed, digits);
  const sign = signOf(rounded);
  const magnitude = `$${group(rounded.int)}${digits > 0 ? `.${rounded.frac}` : ""}`;
  if (sign === 0) return magnitude;
  return `${sign > 0 ? "+" : "-"}${magnitude}`;
}

/**
 * A contract ratio as a percentage: `"0.06"` renders `6.00%`.
 *
 * The multiply by 100 is a decimal-point shift performed on the digit string,
 * not a multiplication, so `"0.0612"` cannot arrive as `6.119999999999999`.
 */
export function contractPercent(text: string | null | undefined, digits = 2): string {
  const shifted = shiftTwo(parseDecimal(text));
  if (!shifted) return DASH;
  const rounded = roundTo(shifted, digits);
  const magnitude = `${group(rounded.int)}${digits > 0 ? `.${rounded.frac}` : ""}%`;
  return signOf(rounded) < 0 ? `-${magnitude}` : magnitude;
}

/**
 * A contract ratio as a signed percentage: `"0.06"` renders `+6.00%`.
 *
 * A non-zero move too small to survive rounding renders `~0.00%` rather than
 * `+0.00%`, matching `signedPercent` in `lib/format.ts`: the reader's question
 * is "did it move at all?", and "almost nothing" answers it better than a sign
 * attached to a zero.
 */
export function contractSignedPercent(text: string | null | undefined, digits = 2): string {
  const shifted = shiftTwo(parseDecimal(text));
  if (!shifted) return DASH;
  const rounded = roundTo(shifted, digits);
  const magnitude = `${group(rounded.int)}${digits > 0 ? `.${rounded.frac}` : ""}%`;
  if (signOf(shifted) !== 0 && isZero(rounded)) return `~${magnitude}`;
  const sign = signOf(rounded);
  if (sign === 0) return magnitude;
  return `${sign > 0 ? "+" : "-"}${magnitude}`;
}

/** Multiply by 100 by moving the point two places right, in text. */
function shiftTwo(value: ExactDecimal | null): ExactDecimal | null {
  if (!value) return null;
  const frac = value.frac.padEnd(2, "0");
  const int = (value.int + frac.slice(0, 2)).replace(/^0+(?=\d)/, "") || "0";
  return { neg: value.neg, int, frac: frac.slice(2) };
}

/**
 * The tone a signed contract figure should carry.
 *
 * `null` is `MUTED`, not `NEUTRAL`: an unknown figure and a figure that is
 * exactly flat are different states and the page colours them differently.
 */
export function contractTone(
  text: string | null | undefined,
): "POSITIVE" | "NEGATIVE" | "NEUTRAL" | "MUTED" {
  const parsed = parseDecimal(text);
  if (!parsed) return "MUTED";
  const sign = signOf(parsed);
  if (sign === 0) return "NEUTRAL";
  return sign > 0 ? "POSITIVE" : "NEGATIVE";
}

/** `-1`, `0`, `+1` comparing two contract decimals, or `null` if either is unknown. */
export function compareDecimals(
  left: string | null | undefined,
  right: string | null | undefined,
): -1 | 0 | 1 | null {
  const a = parseDecimal(left);
  const b = parseDecimal(right);
  if (!a || !b) return null;
  const signA = signOf(a);
  const signB = signOf(b);
  if (signA !== signB) return signA < signB ? -1 : 1;

  const width = Math.max(a.frac.length, b.frac.length);
  const digitsA = a.int + a.frac.padEnd(width, "0");
  const digitsB = b.int + b.frac.padEnd(width, "0");
  const padded = Math.max(digitsA.length, digitsB.length);
  const left0 = digitsA.padStart(padded, "0");
  const right0 = digitsB.padStart(padded, "0");
  if (left0 === right0) return 0;
  const magnitude = left0 < right0 ? -1 : 1;
  // Both negative: the larger magnitude is the smaller number.
  return (signA < 0 ? -magnitude : magnitude) as -1 | 1;
}
