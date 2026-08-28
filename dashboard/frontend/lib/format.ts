/**
 * Display formatting. Pure functions, no React, no fetch, no clock of its own.
 *
 * Two rules run through all of it. Times are rendered in **UTC**, because this
 * system's risk day is a UTC calendar day and a dashboard that quietly showed
 * local time would put an operator one timezone away from the rule that halts
 * trading. And an unavailable figure renders as an em dash - never as `0`,
 * `NaN`, `$0.00`, or a value carried over from the previous poll.
 */

import type { Amount, UnavailableReason } from "./types";

/** What an unreadable figure looks like. One character, everywhere. */
export const DASH = "—";

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const usdCompact = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** `$99,999.95`, or the dash when there is nothing to show. */
export function money(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  return usd.format(value);
}

/** `+$12.34` / `-$0.05`. The sign is the point, so it is always present. */
export function signedMoney(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  return `${sign}${usdCompact.format(Math.abs(value))}`;
}

/** A fraction as a percentage: `0.0234` renders `2.34%`. */
export function percent(fraction: number | null | undefined, digits = 2): string {
  if (fraction === null || fraction === undefined || !Number.isFinite(fraction)) return DASH;
  return `${(fraction * 100).toFixed(digits)}%`;
}

/**
 * A percentage that always carries its sign.
 *
 * A non-zero move too small to survive rounding renders `~0.00%` rather than
 * `-0.00%`: a signed zero reads as a formatting bug, and the reader's question
 * ("did it move at all?") is answered better by "almost nothing" than by a
 * sign attached to a zero.
 */
export function signedPercent(fraction: number | null | undefined, digits = 2): string {
  if (fraction === null || fraction === undefined || !Number.isFinite(fraction)) return DASH;
  const magnitude = (Math.abs(fraction) * 100).toFixed(digits);
  if (fraction !== 0 && Number(magnitude) === 0) return `~${magnitude}%`;
  const sign = fraction > 0 ? "+" : fraction < 0 ? "-" : "";
  return `${sign}${magnitude}%`;
}

/** An `Amount` rendered with `render`, falling back to the dash. */
export function amount(
  value: Amount | null | undefined,
  render: (raw: number) => string = money,
): string {
  if (!value || !value.available || value.value === null) return DASH;
  return render(value.value);
}

/**
 * A quantity, exactly as the backend sent it.
 *
 * Deliberately not parsed into a JavaScript number: `0.000166632` survives the
 * round trip, but the general case does not, and a quantity that has been
 * through a binary float is a different quantity from the one that traded.
 * Trailing zeros are kept - they are the precision the risk engine approved.
 */
export function quantity(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return DASH;
  return value;
}

/** Why a figure is missing, in words an operator can act on. */
export function unavailableLabel(reason: UnavailableReason | null | undefined): string {
  switch (reason) {
    case "BROKER_NOT_CONFIGURED":
      return "No broker credentials";
    case "BROKER_UNREADABLE":
      return "Broker unreadable";
    case "DATABASE_UNREADABLE":
      return "Database unreadable";
    case "NOT_RECORDED":
      return "Not recorded";
    default:
      return "Unavailable";
  }
}

function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function pad(value: number): string {
  return value.toString().padStart(2, "0");
}

/** `17:10:52` in UTC. */
export function clockUtc(iso: string | null | undefined): string {
  const at = parse(iso);
  if (!at) return DASH;
  return `${pad(at.getUTCHours())}:${pad(at.getUTCMinutes())}:${pad(at.getUTCSeconds())}`;
}

/** `28 Aug 16:43:22` in UTC - a date only when it is not `reference`'s date. */
const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
] as const;

export function stampUtc(iso: string | null | undefined, reference?: string | null): string {
  const at = parse(iso);
  if (!at) return DASH;
  const time = clockUtc(iso);
  const today = parse(reference);
  const sameDay =
    today !== null &&
    today.getUTCFullYear() === at.getUTCFullYear() &&
    today.getUTCMonth() === at.getUTCMonth() &&
    today.getUTCDate() === at.getUTCDate();
  if (sameDay) return time;
  return `${pad(at.getUTCDate())} ${MONTHS[at.getUTCMonth()] ?? ""} ${time}`;
}

/** `4m ago`, `2h ago`, `in 12m`. Coarse on purpose: a dashboard is not a stopwatch. */
export function relative(iso: string | null | undefined, now: string | null | undefined): string {
  const at = parse(iso);
  const reference = parse(now);
  if (!at || !reference) return DASH;
  const seconds = Math.round((reference.getTime() - at.getTime()) / 1000);
  const future = seconds < 0;
  const magnitude = Math.abs(seconds);
  const text =
    magnitude < 60
      ? `${magnitude}s`
      : magnitude < 3600
        ? `${Math.floor(magnitude / 60)}m`
        : magnitude < 86400
          ? `${Math.floor(magnitude / 3600)}h`
          : `${Math.floor(magnitude / 86400)}d`;
  return future ? `in ${text}` : `${text} ago`;
}

/** The tone a signed figure should carry. Zero is not a win or a loss. */
export function signTone(value: number | null | undefined): "POSITIVE" | "NEGATIVE" | "NEUTRAL" {
  if (value === null || value === undefined || !Number.isFinite(value) || value === 0) {
    return "NEUTRAL";
  }
  return value > 0 ? "POSITIVE" : "NEGATIVE";
}
