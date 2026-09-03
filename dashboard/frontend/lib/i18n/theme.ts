/**
 * The theme store. Dark is the design; light is a complete second palette.
 *
 * Same external-store shape as the locale for the same reason, and the same
 * pre-paint bootstrap writes `data-theme` before the first frame so switching
 * never flashes. Nothing about trading state is keyed on it.
 */

export const THEMES = ["dark", "light"] as const;
export type Theme = (typeof THEMES)[number];

/** Dark is the designed mode and the default, independent of the OS setting. */
export const DEFAULT_THEME: Theme = "dark";

export const THEME_STORAGE_KEY = "autotrader.theme";

export function isTheme(value: unknown): value is Theme {
  return value === "dark" || value === "light";
}

let current: Theme | null = null;
const listeners = new Set<() => void>();

export function readTheme(): Theme {
  if (current !== null) return current;
  if (typeof window === "undefined") return DEFAULT_THEME;
  let stored: string | null = null;
  try {
    stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  } catch {
    stored = null;
  }
  current = isTheme(stored) ? stored : DEFAULT_THEME;
  return current;
}

export function writeTheme(next: Theme): void {
  current = next;
  if (typeof window !== "undefined") {
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // Not persisted; the session still switches.
    }
    document.documentElement.dataset.theme = next;
  }
  for (const listener of listeners) listener();
}

export function subscribeTheme(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function serverTheme(): Theme {
  return DEFAULT_THEME;
}
