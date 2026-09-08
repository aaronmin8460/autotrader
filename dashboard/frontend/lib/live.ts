/**
 * The Live page's data layer: two polls, and a re-export of the view model.
 *
 * The pure presentation logic lives in `lib/live-view`, which has no React and
 * no fetch in it so `node --test` can load it directly. This module is the
 * part that talks to the network, and it is re-exported through here so a
 * component imports one name from one place.
 *
 * Every request below is a GET. There is no mutation in this file and no
 * endpoint behind one: both real-money services declare
 * `ALLOWED_METHODS = {GET, HEAD}` and assert it against their own route tables.
 */

"use client";

import { usePoll, type PollState } from "./api";
import { LIVE_ACCOUNTING_ENDPOINT, type LiveAccountingSummary } from "./live-contract";
import { LIVE_SAFETY_ENDPOINT, type LiveSafetyPanel } from "./live-safety";

export * from "./live-view";

/**
 * How often the Live records re-read.
 *
 * The same 5 s cadence as the account record. A real-money page is not a reason
 * to poll faster: this system trades on completed 15-minute bars, and a page
 * that streamed would be showing motion rather than information.
 */
export const LIVE_POLL_INTERVAL_MS = 5_000;

export type LiveAccountingState = PollState<LiveAccountingSummary>;
export type LiveSafetyState = PollState<LiveSafetyPanel>;

/** Poll the frozen accounting contract. GET, and there is no other method. */
export function useLiveAccounting(): LiveAccountingState {
  return usePoll<LiveAccountingSummary>(LIVE_ACCOUNTING_ENDPOINT, LIVE_POLL_INTERVAL_MS);
}

/** Poll the real-money safety panel. GET, and there is no other method. */
export function useLiveSafety(): LiveSafetyState {
  return usePoll<LiveSafetyPanel>(LIVE_SAFETY_ENDPOINT, LIVE_POLL_INTERVAL_MS);
}
