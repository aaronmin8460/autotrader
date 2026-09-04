"use client";

/**
 * The translation layer: one provider, one hook, two catalogues.
 *
 * `t("account.equity")` is the only way a component obtains a user-facing
 * string. There is no inline English anywhere in a component, and no
 * `locale === "ko" ? … : …` conditional either - a component that branched on
 * the locale would be a third catalogue nobody maintains.
 *
 * **Authoritative identifiers never pass through here.** `PARTICIPATE`,
 * `FILLED`, `EDA-1`, `AAPL`, a policy hash and a unit name are rendered
 * verbatim in both locales. `gloss()` returns a translation to place *beside*
 * such a value, and returns the empty string in English so the English UI
 * shows the identifier alone.
 *
 * Hydration: the server renders `DEFAULT_LOCALE`, and `useSyncExternalStore`
 * hands the browser its own snapshot on the render *after* hydration. That is
 * the supported way to serve two snapshots without a mismatch warning.
 */

import { createContext, useCallback, useContext, useMemo, useSyncExternalStore } from "react";
import type { ReactNode } from "react";

import { en, type MessageKey, type Messages } from "./en";
import { ko } from "./ko";
import {
  DEFAULT_LOCALE,
  readLocale,
  serverLocale,
  subscribeLocale,
  writeLocale,
  type Locale,
} from "./locale";

export type { Locale } from "./locale";
export { LOCALES, DEFAULT_LOCALE } from "./locale";
export type { MessageKey } from "./en";

const CATALOGUES: Record<Locale, Messages> = { en, ko };

/** Values a message may interpolate. Numbers are pre-formatted by the caller. */
export type Vars = Record<string, string | number>;

export interface I18n {
  locale: Locale;
  setLocale: (next: Locale) => void;
  /** A user-facing string. Falls back to English, then to the key itself. */
  t: (key: MessageKey, vars?: Vars) => string;
  /** A translation to render BESIDE an authoritative identifier. `""` in English. */
  gloss: (identifier: string) => string;
}

const FALLBACK: I18n = {
  locale: DEFAULT_LOCALE,
  setLocale: () => {},
  t: (key) => en[key] ?? key,
  gloss: () => "",
};

const I18nContext = createContext<I18n>(FALLBACK);

function interpolate(template: string, vars?: Vars): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (whole, name: string) =>
    name in vars ? String(vars[name]) : whole,
  );
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const locale = useSyncExternalStore(subscribeLocale, readLocale, serverLocale);

  const t = useCallback(
    (key: MessageKey, vars?: Vars) => {
      const catalogue = CATALOGUES[locale] ?? en;
      const template = catalogue[key] ?? en[key] ?? key;
      return interpolate(template, vars);
    },
    [locale],
  );

  const gloss = useCallback(
    (identifier: string) => {
      if (locale === DEFAULT_LOCALE) return "";
      const key = `gloss.${identifier}` as MessageKey;
      const catalogue = CATALOGUES[locale] ?? en;
      return catalogue[key] ?? "";
    },
    [locale],
  );

  const value = useMemo<I18n>(
    () => ({ locale, setLocale: writeLocale, t, gloss }),
    [locale, t, gloss],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18n {
  return useContext(I18nContext);
}

/** Just the translate function, for components that need nothing else. */
export function useT(): I18n["t"] {
  return useContext(I18nContext).t;
}
