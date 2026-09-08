/**
 * The Live page's view model. Presentation decisions only, and no dependencies
 * that a bundler has to resolve.
 *
 * Split out of `lib/live.ts` for the same reason `chart-util` is split out of
 * `charts`: the hooks in `live.ts` pull in React and the polling layer, and a
 * module that does that cannot be loaded by `node --test`. Everything here is a
 * pure function over a payload, so the whole view model is unit-testable
 * against the fixtures without a browser.
 *
 * This module decides what a state should *look* like — which tone, which
 * message key, whether a figure may be presented as current. It decides nothing
 * about what a figure *is*. There is no subtraction, no ratio and no fallback
 * anywhere below that reconstructs a suppressed number from the ones beside it:
 * the backend calculates money and the dashboard displays it, and the seam
 * between those two jobs is this module's whole reason to exist.
 *
 * Two rules are enforced here once rather than in each component:
 *
 * **Unknown is not zero, and unknown is not false.** `live_armed: null` is
 * `UNKNOWN`, never `DISARMED`. A suppressed reserve is "unavailable", never
 * `$0.00`. A withdrawal preview of `"0.00"` is a real zero and is shown as one
 * — which is why `withdrawalView` distinguishes the two.
 *
 * **DISARMED is a safe state, not an error.** It carries no negative tone
 * anywhere in this file. The state that draws attention is `ARMED`, because
 * that is the one where a real-money order can leave the building, and
 * `UNKNOWN`, because a switch nobody can read has not been proven off.
 */

import { isZero, parseDecimal, signOf } from "./live-decimal.ts";
import {
  STALENESS_HORIZON_SECONDS,
  accountingStatusOf,
  derivedFiguresTrustworthy,
  parseSummary,
  type AccountingStatus,
  type LiveAccountingSummary,
} from "./live-contract.ts";
import {
  armStateOf,
  serviceStateOf,
  type ArmStateValue,
  type LiveSafetyPanel,
  type LiveServiceState,
} from "./live-safety.ts";
import type { MessageKey } from "./i18n";
import type { Tone } from "./types";

/* ------------------------------------------------------------- readiness -- */

export type ReadinessValue = "READY" | "NOT_READY" | "UNKNOWN";

export interface ReadinessView {
  ready: ReadinessValue;
  readyTone: Tone;
  readyKey: MessageKey;
  arm: ArmStateValue;
  armTone: Tone;
  armKey: MessageKey;
  /** The sentence about whether real money can move. Always rendered. */
  mutationKey: MessageKey;
  mutationTone: Tone;
  /** Why the switch is where it is, in the backend's own words. */
  armReason: string | null;
}

/**
 * READY and ARMED, resolved from whichever source can answer.
 *
 * Both sources pass the same two flags through from Prompt 1 — the accounting
 * contract says so explicitly for `live_ready` and `live_armed` — so they cannot
 * legitimately disagree. The safety panel is preferred because it carries the
 * arm switch's reason and timestamp beside the flag; the contract is the
 * fallback so the header still resolves when only the accounting service is up.
 */
export function readinessView(
  safety: LiveSafetyPanel | null,
  summary: LiveAccountingSummary | null,
): ReadinessView {
  const readyFlag = safety?.live_ready ?? summary?.live_ready ?? null;
  const ready: ReadinessValue =
    readyFlag === true ? "READY" : readyFlag === false ? "NOT_READY" : "UNKNOWN";

  let arm: ArmStateValue = armStateOf(safety);
  if (safety === null && summary !== null) {
    arm = summary.live_armed === true ? "ARMED" : summary.live_armed === false ? "DISARMED" : "UNKNOWN";
  }

  return {
    ready,
    readyTone: ready === "READY" ? "POSITIVE" : ready === "NOT_READY" ? "ATTENTION" : "MUTED",
    readyKey:
      ready === "READY"
        ? "live.ready.yes"
        : ready === "NOT_READY"
          ? "live.ready.no"
          : "live.ready.unknown",
    arm,
    // DISARMED is NEUTRAL, deliberately. It is the correct, safe, expected
    // state for this whole program and must not be coloured as a fault.
    armTone: arm === "ARMED" ? "NEGATIVE" : arm === "DISARMED" ? "NEUTRAL" : "ATTENTION",
    armKey:
      arm === "ARMED" ? "live.arm.armed" : arm === "DISARMED" ? "live.arm.disarmed" : "live.arm.unknown",
    mutationKey:
      arm === "ARMED"
        ? "live.mutation.enabled"
        : arm === "DISARMED"
          ? "live.mutation.disabled"
          : "live.mutation.unknown",
    mutationTone: arm === "ARMED" ? "NEGATIVE" : arm === "DISARMED" ? "MUTED" : "ATTENTION",
    armReason: safety?.arm.reason ?? null,
  };
}

/* ------------------------------------------------------------ accounting -- */

export type AccountingPresentation = "TRUSTED" | "STALE" | "SUPPRESSED" | "UNAVAILABLE";

export interface AccountingView {
  presentation: AccountingPresentation;
  status: AccountingStatus | null;
  statusLabel: string;
  tone: Tone;
  /** The backend's own sentence. Never paraphrased and never invented. */
  detail: string | null;
  /** Whether HWM / reserve / bucket / preview may be shown as current figures. */
  derivedTrustworthy: boolean;
  freshness: "FRESH" | "STALE" | "UNKNOWN";
  freshnessSeconds: number | null;
  stalenessHorizonSeconds: number;
}

/**
 * How much of the accounting payload may be believed, and how to say so.
 *
 * `SUPPRESSED` is the fail-closed case the contract designs for: under
 * `UNKNOWN_EXTERNAL_FLOW`, `REBUILD_REQUIRED`, `BROKER_UNAVAILABLE`,
 * `ACCOUNT_MISMATCH` and `NOT_INITIALIZED` the derived figures arrive as `null`
 * and the page says why rather than drawing a precise wrong number. `STALE`
 * keeps the figures and badges them, which is a different instruction to the
 * reader and is kept different here.
 */
export function accountingView(summary: LiveAccountingSummary | null): AccountingView {
  if (summary === null) {
    return {
      presentation: "UNAVAILABLE",
      status: null,
      statusLabel: "UNAVAILABLE",
      tone: "MUTED",
      detail: null,
      derivedTrustworthy: false,
      freshness: "UNKNOWN",
      freshnessSeconds: null,
      stalenessHorizonSeconds: STALENESS_HORIZON_SECONDS,
    };
  }
  const status = accountingStatusOf(summary);
  const trustworthy = derivedFiguresTrustworthy(summary);
  const stale = status === "STALE";
  const presentation: AccountingPresentation = !trustworthy
    ? "SUPPRESSED"
    : stale
      ? "STALE"
      : "TRUSTED";
  const freshnessRaw = summary.data_freshness;
  const freshness =
    freshnessRaw === "FRESH" || freshnessRaw === "STALE" ? freshnessRaw : "UNKNOWN";
  return {
    presentation,
    status,
    // The machine status is rendered verbatim in both locales; a gloss goes
    // beside it, never in place of it.
    statusLabel: summary.accounting_status,
    tone: presentation === "TRUSTED" ? "POSITIVE" : presentation === "STALE" ? "ATTENTION" : "NEGATIVE",
    detail: summary.accounting_status_detail,
    derivedTrustworthy: trustworthy,
    freshness,
    freshnessSeconds: summary.data_freshness_seconds,
    stalenessHorizonSeconds: STALENESS_HORIZON_SECONDS,
  };
}

/* ------------------------------------------------------------ withdrawal -- */

export type WithdrawalPreviewState =
  | "AVAILABLE"
  | "ZERO_BELOW_HWM"
  | "ZERO_NOT_HARVEST_ELIGIBLE"
  | "ZERO_EMPTY_BUCKET"
  | "UNAVAILABLE";

export interface WithdrawalView {
  state: WithdrawalPreviewState;
  reasonKey: MessageKey | null;
  /** OBSERVE_ONLY, rendered verbatim. A machine identifier, not a label. */
  mode: string | null;
  authorized: boolean | null;
  automatic: boolean | null;
}

/**
 * Why the withdrawal preview reads what it reads.
 *
 * The reasons are taken from the contract's own rule — the preview is the
 * bucket balance when harvest is due AND the account is at or above its
 * adjusted high-water mark, and zero otherwise — and are distinguished so the
 * page can say "no preview while below the adjusted HWM" rather than leaving an
 * operator to guess whether $0.00 means "nothing earned" or "not yet due".
 *
 * The comparison against the HWM reads the backend's own drawdown field rather
 * than comparing two equity figures locally. A page that decided for itself
 * whether it was below the mark would be a second implementation of the rule
 * that gates withdrawals.
 */
export function withdrawalView(summary: LiveAccountingSummary | null): WithdrawalView {
  if (summary === null) {
    return { state: "UNAVAILABLE", reasonKey: null, mode: null, authorized: null, automatic: null };
  }
  const shell = {
    mode: summary.withdrawal_mode,
    authorized: summary.withdrawal_authorized,
    automatic: summary.automatic_transfer_enabled,
  };
  if (!derivedFiguresTrustworthy(summary) || summary.withdrawal_preview === null) {
    return { state: "UNAVAILABLE", reasonKey: "live.withdrawal.previewUnavailable", ...shell };
  }
  // Exactly as everywhere else on this page: the decimal string is inspected
  // digit by digit, never through `Number`.
  if (!isZero(parseDecimal(summary.withdrawal_preview))) {
    return { state: "AVAILABLE", reasonKey: null, ...shell };
  }
  // A zero preview. Which of the three zeros it is decides what to say.
  if (!summary.harvest_eligible) {
    return { state: "ZERO_NOT_HARVEST_ELIGIBLE", reasonKey: "live.withdrawal.notHarvestDue", ...shell };
  }
  // `signOf` rather than a leading-minus test: `"-0.00"` is textually negative
  // and numerically flat, and an account exactly at its high-water mark is not
  // in a drawdown.
  if (signOf(parseDecimal(summary.current_drawdown_from_adjusted_hwm)) < 0) {
    return { state: "ZERO_BELOW_HWM", reasonKey: "live.withdrawal.belowHwm", ...shell };
  }
  return { state: "ZERO_EMPTY_BUCKET", reasonKey: "live.withdrawal.emptyBucket", ...shell };
}

/* --------------------------------------------------------------- service -- */

export interface ServiceView {
  state: LiveServiceState;
  tone: Tone;
  labelKey: MessageKey;
  detail: string | null;
  unit: string;
}

/**
 * The Live unit's state, and the tone it deserves.
 *
 * `NOT_INSTALLED` is `MUTED`, not negative. Prompt 1 prepared the unit and
 * deliberately left installing it as the first step of the first-day runbook,
 * so on this program's timeline it is the expected state and colouring it as a
 * fault would train an operator to ignore the colour.
 */
export function serviceView(safety: LiveSafetyPanel | null): ServiceView {
  const state = serviceStateOf(safety);
  const tone: Tone =
    state === "RUNNING"
      ? "POSITIVE"
      : state === "NOT_INSTALLED" || state === "DISABLED"
        ? "MUTED"
        : state === "STOPPED"
          ? "ATTENTION"
          : "ATTENTION";
  const labelKey: MessageKey =
    state === "RUNNING"
      ? "live.service.running"
      : state === "NOT_INSTALLED"
        ? "live.service.notInstalled"
        : state === "DISABLED"
          ? "live.service.disabled"
          : state === "STOPPED"
            ? "live.service.stopped"
            : "live.service.unknown";
  return {
    state,
    tone,
    labelKey,
    detail: safety?.service.detail ?? null,
    unit: safety?.service.unit ?? "autotrader-equity-live.service",
  };
}

/* ----------------------------------------------------------------- guard -- */

export interface GuardView {
  status: "ACTIVE" | "INACTIVE" | "UNKNOWN";
  tone: Tone;
  labelKey: MessageKey;
  reason: string | null;
  riskDay: string | null;
}

/**
 * The deposit-day guard.
 *
 * `ACTIVE` is `ATTENTION` rather than `NEGATIVE`: the guard working is the
 * system behaving correctly, and the state it describes — entries blocked,
 * exits available — is a restriction to be aware of, not a failure.
 */
export function guardView(safety: LiveSafetyPanel | null): GuardView {
  const raw = safety?.deposit_day_guard.status;
  const status = raw === "ACTIVE" ? "ACTIVE" : raw === "INACTIVE" ? "INACTIVE" : "UNKNOWN";
  return {
    status,
    tone: status === "ACTIVE" ? "ATTENTION" : status === "INACTIVE" ? "MUTED" : "ATTENTION",
    labelKey:
      status === "ACTIVE"
        ? "live.guard.active"
        : status === "INACTIVE"
          ? "live.guard.inactive"
          : "live.guard.unknown",
    reason: safety?.deposit_day_guard.reason ?? null,
    riskDay: safety?.deposit_day_guard.risk_day ?? null,
  };
}

/* -------------------------------------------------------------- contract -- */

/**
 * Validate a payload before the page renders it.
 *
 * A payload that fails the frozen contract does not reach a card. The page
 * shows the problems instead, because silently rendering a partial payload is
 * exactly the adaptation the freeze exists to prevent.
 */
export function validated(payload: LiveAccountingSummary | null): {
  summary: LiveAccountingSummary | null;
  problems: string[];
} {
  if (payload === null) return { summary: null, problems: [] };
  return parseSummary(payload);
}
