/**
 * Locale-aware date rendering, over an intentionally locale-INVARIANT clock.
 *
 * The rule this file enforces: **localisation may change how a value is
 * written, never what it means.**
 *
 * * A **clock** stays `HH:MM:SS`, 24-hour, UTC, in both locales. This system's
 *   risk day is a UTC calendar day and its bars are UTC timestamps; rendering
 *   `오후 6:05` where the halt rule reads 18:05 UTC would put a Korean operator
 *   one conversion away from the rule that stops trading. The word `UTC` is
 *   printed beside it in both locales.
 * * A **date** is localised, because a date is unambiguous either way:
 *   `Sep 3, 2026` / `2026. 9. 3.`.
 * * A **currency** is never converted. `$101,995.05` is the account's own
 *   currency and reads identically in both locales; a KRW figure would be a
 *   number this system has never computed.
 */

import type { Locale } from "./locale";

/**
 * The dash, restated rather than imported.
 *
 * `lib/format` is bundler-resolved and this module is also executed directly
 * by `node --test`, which will not resolve an extensionless specifier. The
 * duplication is one character wide and `lib/i18n.test.ts` asserts the two
 * stay equal, so it cannot drift silently.
 */
const DASH = "—";

const INTL_LOCALE: Record<Locale, string> = { en: "en-US", ko: "ko-KR" };

function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? null : at;
}

const dateCache = new Map<string, Intl.DateTimeFormat>();

function formatter(locale: Locale, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${locale}:${JSON.stringify(options)}`;
  let found = dateCache.get(key);
  if (!found) {
    found = new Intl.DateTimeFormat(INTL_LOCALE[locale], { ...options, timeZone: "UTC" });
    dateCache.set(key, found);
  }
  return found;
}

/**
 * The full date, written the way each locale writes one.
 *
 * `Sep 3, 2026` / `2026. 9. 3.` - a numeric month in Korean, because that is
 * the conventional written form there, not because the value differs.
 */
const LONG_DATE: Record<Locale, Intl.DateTimeFormatOptions> = {
  en: { year: "numeric", month: "short", day: "numeric" },
  ko: { year: "numeric", month: "numeric", day: "numeric" },
};

export function dateLong(iso: string | null | undefined, locale: Locale): string {
  const at = parse(iso);
  if (!at) return DASH;
  return formatter(locale, LONG_DATE[locale]).format(at);
}

/** `3 Sep` / `9월 3일` — the compact form used in dense table stamps. */
export function dateShort(iso: string | null | undefined, locale: Locale): string {
  const at = parse(iso);
  if (!at) return DASH;
  return formatter(locale, { month: "short", day: "numeric" }).format(at);
}

/**
 * The full stamp, for a tooltip or a header: localised date, invariant clock.
 * `Sep 3, 2026 18:05:39 UTC` / `2026. 9. 3. 18:05:39 UTC`
 */
export function stampFull(iso: string | null | undefined, locale: Locale): string {
  const at = parse(iso);
  if (!at) return DASH;
  const pad = (value: number) => value.toString().padStart(2, "0");
  const clock = `${pad(at.getUTCHours())}:${pad(at.getUTCMinutes())}:${pad(at.getUTCSeconds())}`;
  return `${dateLong(iso, locale)} ${clock} UTC`;
}

/**
 * A stamp for a table cell: the clock alone on `reference`'s own UTC day, and
 * a localised date in front of it on any other day.
 */
export function stamp(
  iso: string | null | undefined,
  reference: string | null | undefined,
  locale: Locale,
): string {
  const at = parse(iso);
  if (!at) return DASH;
  const pad = (value: number) => value.toString().padStart(2, "0");
  const clock = `${pad(at.getUTCHours())}:${pad(at.getUTCMinutes())}:${pad(at.getUTCSeconds())}`;
  const today = parse(reference);
  const sameDay =
    today !== null &&
    today.getUTCFullYear() === at.getUTCFullYear() &&
    today.getUTCMonth() === at.getUTCMonth() &&
    today.getUTCDate() === at.getUTCDate();
  return sameDay ? clock : `${dateShort(iso, locale)} ${clock}`;
}

/** `4m ago` / `4분 전`. Coarse on purpose: a dashboard is not a stopwatch. */
export function relativeTime(
  iso: string | null | undefined,
  now: string | null | undefined,
  locale: Locale,
): string {
  const at = parse(iso);
  const reference = parse(now);
  if (!at || !reference) return DASH;
  const seconds = Math.round((reference.getTime() - at.getTime()) / 1000);
  const magnitude = Math.abs(seconds);
  const [value, unit]: [number, Intl.RelativeTimeFormatUnit] =
    magnitude < 60
      ? [seconds, "second"]
      : magnitude < 3600
        ? [Math.trunc(seconds / 60), "minute"]
        : magnitude < 86400
          ? [Math.trunc(seconds / 3600), "hour"]
          : [Math.trunc(seconds / 86400), "day"];
  const relative = new Intl.RelativeTimeFormat(INTL_LOCALE[locale], {
    numeric: "always",
    style: "narrow",
  });
  // `seconds` counts elapsed time, so the sign flips for "ago".
  return relative.format(-value, unit);
}
