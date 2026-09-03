/**
 * The locale store: two locales, one preference, one place it is written.
 *
 * Implemented as an external store rather than React state so the provider can
 * read it through `useSyncExternalStore`. That matters for one reason: the
 * server renders in the default locale and the browser may hold a different
 * one, and `useSyncExternalStore` is the only way to serve two different
 * snapshots to the two renders **without a hydration mismatch**. Reading
 * `localStorage` during render would produce exactly the mismatch this file
 * exists to avoid.
 *
 * The preference is a display preference and nothing else. No trading state,
 * no account state and no policy figure is stored here or keyed on it.
 */

export const LOCALES = ["en", "ko"] as const;
export type Locale = (typeof LOCALES)[number];

/** What the server renders and what a browser with no stored answer gets. */
export const DEFAULT_LOCALE: Locale = "en";

export const LOCALE_STORAGE_KEY = "autotrader.locale";

/** The `lang` attribute value for a locale. Used on <html>. */
export const HTML_LANG: Record<Locale, string> = { en: "en", ko: "ko" };

export function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && (LOCALES as readonly string[]).includes(value);
}

/**
 * The browser's own preference, when it expresses one for a locale we have.
 *
 * Consulted only when nothing is stored. Deliberately *not* derived from the
 * brokerage account's country: where an account is domiciled says nothing
 * about what language its operator reads.
 */
function fromNavigator(): Locale | null {
  if (typeof navigator === "undefined") return null;
  const candidates = navigator.languages?.length ? navigator.languages : [navigator.language];
  for (const tag of candidates) {
    if (!tag) continue;
    const base = tag.toLowerCase().split("-")[0];
    if (isLocale(base)) return base;
  }
  return null;
}

let current: Locale | null = null;
const listeners = new Set<() => void>();

/** The locale this browser should use, resolving the preference once. */
export function readLocale(): Locale {
  if (current !== null) return current;
  if (typeof window === "undefined") return DEFAULT_LOCALE;
  let stored: string | null = null;
  try {
    stored = window.localStorage.getItem(LOCALE_STORAGE_KEY);
  } catch {
    // A browser with site data blocked still gets a working dashboard.
    stored = null;
  }
  current = isLocale(stored) ? stored : (fromNavigator() ?? DEFAULT_LOCALE);
  return current;
}

export function writeLocale(next: Locale): void {
  current = next;
  if (typeof window !== "undefined") {
    try {
      window.localStorage.setItem(LOCALE_STORAGE_KEY, next);
    } catch {
      // Preference is not persisted; the session still switches.
    }
    document.documentElement.lang = HTML_LANG[next];
  }
  for (const listener of listeners) listener();
}

export function subscribeLocale(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** What the server renders. Constant by definition. */
export function serverLocale(): Locale {
  return DEFAULT_LOCALE;
}

/**
 * The pre-paint bootstrap.
 *
 * Runs as an inline script in <head> so `lang` and `data-theme` are correct
 * before the first paint - no theme flash, and assistive technology reads the
 * right language from the first frame. Inline script is permitted by the
 * deployed CSP (`script-src 'self' 'unsafe-inline'`), which the Next.js App
 * Router bootstrap already requires; this adds no new allowance.
 */
export const BOOTSTRAP_SCRIPT = `(function(){try{
var l=localStorage.getItem(${JSON.stringify(LOCALE_STORAGE_KEY)});
if(l!=="en"&&l!=="ko"){var n=(navigator.languages&&navigator.languages[0])||navigator.language||"en";l=String(n).toLowerCase().indexOf("ko")===0?"ko":"en";}
document.documentElement.lang=l;
var t=localStorage.getItem("autotrader.theme");
document.documentElement.dataset.theme=(t==="light"||t==="dark")?t:"dark";
}catch(e){document.documentElement.dataset.theme="dark";}})();`;
