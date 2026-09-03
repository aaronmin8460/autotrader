/**
 * Small display preferences, in the same external-store shape as the locale.
 *
 * A preference here changes what the chrome looks like and nothing else. No
 * trading state, no account figure and no policy value is stored in, or keyed
 * on, anything in this file — and a browser with site data blocked gets a
 * working dashboard with the default.
 */

export interface Pref<T> {
  read: () => T;
  write: (next: T) => void;
  subscribe: (listener: () => void) => () => void;
  server: () => T;
}

export function createPref<T>(
  key: string,
  fallback: T,
  parse: (raw: string) => T | null,
  serialize: (value: T) => string,
): Pref<T> {
  let current: T | null = null;
  const listeners = new Set<() => void>();

  return {
    read() {
      if (current !== null) return current;
      if (typeof window === "undefined") return fallback;
      try {
        const raw = window.localStorage.getItem(key);
        current = raw === null ? fallback : (parse(raw) ?? fallback);
      } catch {
        current = fallback;
      }
      return current;
    },
    write(next: T) {
      current = next;
      if (typeof window !== "undefined") {
        try {
          window.localStorage.setItem(key, serialize(next));
        } catch {
          // Not persisted; the session still applies it.
        }
      }
      for (const listener of listeners) listener();
    },
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    server: () => fallback,
  };
}

/** Whether the left navigation is collapsed to its icon rail. */
export const navCollapsedPref = createPref<boolean>(
  "autotrader.nav.collapsed",
  false,
  (raw) => (raw === "1" ? true : raw === "0" ? false : null),
  (value) => (value ? "1" : "0"),
);
