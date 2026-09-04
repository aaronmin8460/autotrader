"use client";

/**
 * One hook for every formatted value a component renders.
 *
 * Money and percentages are **locale-invariant on purpose**: `$101,995.05` is
 * the account's own currency and is written the same way in both locales. This
 * dashboard never converts a figure, and a KRW rendering of a USD account
 * would be a number this system has never computed.
 *
 * Dates are localised; the clock is not. See `datetime.ts` for why a UTC risk
 * day must not be rendered in a 12-hour local form.
 */

import { useMemo } from "react";

import { money, percent, quantity, signedMoney, signedPercent } from "../format";
import { dateLong, dateShort, relativeTime, stamp, stampFull } from "./datetime";
import { useI18n } from "./index";

export function useFormat() {
  const { locale } = useI18n();
  return useMemo(
    () => ({
      /** Locale-invariant. */
      money,
      signedMoney,
      percent,
      signedPercent,
      quantity,
      /** Localised date, invariant UTC clock. */
      stamp: (iso: string | null | undefined, reference?: string | null) =>
        stamp(iso, reference ?? null, locale),
      stampFull: (iso: string | null | undefined) => stampFull(iso, locale),
      dateLong: (iso: string | null | undefined) => dateLong(iso, locale),
      dateShort: (iso: string | null | undefined) => dateShort(iso, locale),
      relative: (iso: string | null | undefined, now: string | null | undefined) =>
        relativeTime(iso, now, locale),
      locale,
    }),
    [locale],
  );
}
