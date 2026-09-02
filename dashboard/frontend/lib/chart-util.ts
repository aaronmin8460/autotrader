/**
 * Chart helpers with no React in them, so the test suite can run them.
 */

/** Why a series is missing, in words. */
export function chartUnavailableLabel(reason: string | null | undefined): string {
  switch (reason) {
    case "NO_BARS":
      return "No bars for this range";
    case "BROKER_NOT_CONFIGURED":
      return "No market-data credentials";
    case "PROVIDER_UNREADABLE":
      return "Provider unreadable";
    case "PROVIDER_BUDGET_EXHAUSTED":
      return "Chart budget exhausted; retry later";
    case "INVALID_SYMBOL":
      return "Not a chartable symbol";
    default:
      return "Chart unavailable";
  }
}

export function chunk<T>(items: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let index = 0; index < items.length; index += size) {
    out.push(items.slice(index, index + size));
  }
  return out;
}

/** The symbols a request should carry: upper-cased, de-duplicated, sorted. */
export function normalizeSymbols(symbols: ReadonlyArray<string>): string[] {
  return Array.from(new Set(symbols.map((symbol) => symbol.trim().toUpperCase()).filter(Boolean))).sort();
}
