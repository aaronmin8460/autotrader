/**
 * How current a record is, against the interval it was read on.
 *
 * Split out of the component so it can be tested directly. The threshold is
 * derived rather than invented: a record polled every `intervalMs` is stale
 * once it is older than three of those intervals plus one request timeout -
 * long enough that a single missed poll is not an alarm, short enough that a
 * dead backend is visible inside one cycle. Inventing a fixed number here
 * would be inventing a claim about how often the backend updates.
 */

export type Freshness = "FRESH" | "STALE" | "WAITING" | "OFFLINE";

/** Matches `REQUEST_TIMEOUT_MS` in `lib/api`; one abandoned poll is tolerated. */
export const FRESHNESS_TIMEOUT_MS = 8_000;

export const FRESHNESS_INTERVALS = 3;

export function freshnessOf(
  generatedAt: string | null | undefined,
  connected: boolean,
  intervalMs: number,
  now: number = Date.now(),
): { state: Freshness; ageSeconds: number | null } {
  if (!generatedAt) return { state: connected ? "WAITING" : "OFFLINE", ageSeconds: null };
  const at = new Date(generatedAt).getTime();
  if (Number.isNaN(at)) return { state: "WAITING", ageSeconds: null };
  const ageSeconds = Math.max(0, Math.round((now - at) / 1000));
  if (!connected) return { state: "OFFLINE", ageSeconds };
  const limitSeconds = (intervalMs * FRESHNESS_INTERVALS + FRESHNESS_TIMEOUT_MS) / 1000;
  return { state: ageSeconds > limitSeconds ? "STALE" : "FRESH", ageSeconds };
}
