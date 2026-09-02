/**
 * The risk view: the deployed policy's targets and hard caps against what the
 * broker says the account holds right now.
 *
 * The defect this module replaces: the operations page read its limits from
 * the operational API, whose deployed build derives them from the crypto
 * runtime's much lower risk-engine constants. That painted a book sized to a
 * 90% target under a 95% cap red, and a 9% position red, against lines that
 * belong to a different runtime.
 *
 * Two sources, joined here and only here:
 *
 *   * the **policy** comes from the equity paper API, which reads the running
 *     paper process's own start event and resolves it in the allocation
 *     registry - the target, the two hard caps, the cash reserve, the slot
 *     weight, the daily halt. Numbers, not strings, and flagged
 *     `authoritative` when they are the runtime's own;
 *   * the **observation** comes from the broker read on the operational API:
 *     equity, cash, positions, market values.
 *
 * Nothing in this file is a constant that could drift from either. When the
 * policy is not available the panel says so and shows no limit at all,
 * because a limit from memory is the mistake this module exists to end.
 *
 * Deliberately free of React so the test suite can run it directly.
 */

import type { PolicyPanel } from "./paper";
import type { PositionRow, PositionsPanel, PrimaryMetrics, RiskLimit, Tone } from "./types";

export type RiskStatus =
  | "ON TARGET"
  | "BELOW TARGET"
  | "ABOVE TARGET"
  | "NEAR CAP"
  | "OVER CAP"
  | "UNAVAILABLE";

/** How far from the target still reads as on target, in absolute fraction. */
export const TOTAL_TARGET_TOLERANCE = 0.02;
export const SYMBOL_TARGET_TOLERANCE = 0.01;

/** Inside this distance of a hard cap the row turns amber. */
export const NEAR_CAP_MARGIN = 0.01;

export interface RiskRow {
  key: "symbol" | "total" | "cash";
  label: string;
  /** The observed fraction of equity, or null when the broker could not be read. */
  current: number | null;
  /** The observed dollar figure behind `current`. */
  currentValue: number | null;
  /** The policy's target fraction, or null when the policy defines none. */
  target: number | null;
  /** The policy's hard cap fraction, or null for rows without one (cash). */
  cap: number | null;
  /** The dollar figure at the cap, when equity is known. */
  capValue: number | null;
  status: RiskStatus;
  tone: Tone;
  /** The symbol the per-symbol row is about. */
  subject: string | null;
  detail: string;
  /** Where the row sits on a 0..1 rail, or null. */
  rail: { current: number; target: number | null; cap: number | null } | null;
}

export interface RiskView {
  available: boolean;
  policyId: string | null;
  policyHash: string | null;
  authoritative: boolean;
  rows: RiskRow[];
  dailyLoss: RiskLimit | null;
  note: string;
}

function classify(
  current: number | null,
  target: number | null,
  cap: number | null,
  tolerance: number,
): { status: RiskStatus; tone: Tone } {
  if (current === null || !Number.isFinite(current)) {
    return { status: "UNAVAILABLE", tone: "MUTED" };
  }
  if (cap !== null && current > cap) return { status: "OVER CAP", tone: "NEGATIVE" };
  if (cap !== null && current > cap - NEAR_CAP_MARGIN) return { status: "NEAR CAP", tone: "ATTENTION" };
  if (target === null) return { status: "ON TARGET", tone: "POSITIVE" };
  if (Math.abs(current - target) <= tolerance) return { status: "ON TARGET", tone: "POSITIVE" };
  return current < target
    ? { status: "BELOW TARGET", tone: "NEUTRAL" }
    : { status: "ABOVE TARGET", tone: "NEUTRAL" };
}

function largest(positions: PositionsPanel | null): PositionRow | null {
  if (!positions || positions.source !== "BROKER") return null;
  let top: PositionRow | null = null;
  for (const row of positions.rows) {
    if (row.market_value === null) continue;
    if (top === null || (top.market_value ?? 0) < row.market_value) top = row;
  }
  return top;
}

/**
 * The three rows, computed from the policy and the broker read.
 *
 * `dailyLoss` is passed through from the operational API's own risk panel:
 * the 2% UTC-day halt is the same rule in both runtimes and that API measures
 * it against the stored baseline, which nothing else on the page can do.
 */
export function buildRiskView(
  metrics: PrimaryMetrics | null,
  positions: PositionsPanel | null,
  policy: PolicyPanel | null | undefined,
  dailyLoss: RiskLimit | null | undefined,
): RiskView {
  const equity =
    metrics && metrics.equity.available && metrics.equity.value ? metrics.equity.value : null;
  const exposure =
    metrics && metrics.exposure.available && metrics.exposure.value !== null
      ? metrics.exposure.value
      : null;
  const cash = metrics && metrics.cash.available && metrics.cash.value !== null ? metrics.cash.value : null;

  if (!policy) {
    return {
      available: false,
      policyId: null,
      policyHash: null,
      authoritative: false,
      rows: [],
      dailyLoss: dailyLoss ?? null,
      note: "The deployed sizing policy could not be read from the equity paper API, so no target or cap is shown. Nothing here is taken from a constant.",
    };
  }

  const top = largest(positions);
  const topFraction = top && equity && top.market_value !== null ? top.market_value / equity : null;
  const totalFraction = exposure !== null && equity ? exposure / equity : null;
  const cashFraction = cash !== null && equity ? cash / equity : null;

  const symbol = classify(topFraction, policy.target_slot_weight, policy.hard_symbol_cap, SYMBOL_TARGET_TOLERANCE);
  const total = classify(totalFraction, policy.target_gross, policy.hard_gross_cap, TOTAL_TARGET_TOLERANCE);
  // Cash mirrors the total: too little cash is the same fact as too much exposure.
  const cashStatus: { status: RiskStatus; tone: Tone } =
    cashFraction === null
      ? { status: "UNAVAILABLE", tone: "MUTED" }
      : total.status === "OVER CAP" || total.status === "NEAR CAP"
        ? { status: "BELOW TARGET", tone: total.tone }
        : Math.abs(cashFraction - policy.cash_reserve_target) <= TOTAL_TARGET_TOLERANCE
          ? { status: "ON TARGET", tone: "POSITIVE" }
          : cashFraction > policy.cash_reserve_target
            ? { status: "ABOVE TARGET", tone: "NEUTRAL" }
            : { status: "BELOW TARGET", tone: "NEUTRAL" };

  const rows: RiskRow[] = [
    {
      key: "symbol",
      label: "Per-symbol exposure",
      current: topFraction,
      currentValue: top?.market_value ?? null,
      target: policy.target_slot_weight,
      cap: policy.hard_symbol_cap,
      capValue: equity ? equity * policy.hard_symbol_cap : null,
      status: symbol.status,
      tone: symbol.tone,
      subject: top?.symbol ?? null,
      detail:
        "Largest single-symbol market value against account equity. The target slot is the policy's per-symbol share when nothing outside the equity book uses the account; the hard cap is what Risk refuses to project past on any order.",
      rail: topFraction === null ? null : { current: topFraction, target: policy.target_slot_weight, cap: policy.hard_symbol_cap },
    },
    {
      key: "total",
      label: "Total account exposure",
      current: totalFraction,
      currentValue: exposure,
      target: policy.target_gross,
      cap: policy.hard_gross_cap,
      capValue: equity ? equity * policy.hard_gross_cap : null,
      status: total.status,
      tone: total.tone,
      subject: null,
      detail:
        "Aggregate long market value against account equity, both books counted. The target is what the allocator aims for with every reserved slot active; the hard cap blocks any exposure-increasing order that would project past it.",
      rail: totalFraction === null ? null : { current: totalFraction, target: policy.target_gross, cap: policy.hard_gross_cap },
    },
    {
      key: "cash",
      label: "Cash reserve",
      current: cashFraction,
      currentValue: cash,
      target: policy.cash_reserve_target,
      cap: null,
      capValue: null,
      status: cashStatus.status,
      tone: cashStatus.tone,
      subject: null,
      detail: "Settled cash against account equity. The reserve target is one minus the target gross; it is what the policy deliberately leaves undeployed.",
      rail: cashFraction === null ? null : { current: cashFraction, target: policy.cash_reserve_target, cap: null },
    },
  ];

  return {
    available: rows.some((row) => row.current !== null),
    policyId: policy.policy_id,
    policyHash: policy.config_hash,
    authoritative: policy.authoritative,
    rows,
    dailyLoss: dailyLoss ?? null,
    note: policy.authoritative
      ? `Targets and caps are ${policy.policy_id}'s, read from the running paper process. Only a hard-cap breach is red.`
      : "The paper process's policy name could not be read; these are fallback registry figures, not the runtime's own.",
  };
}

/** True when the view still carries a figure the fractional policy retired. */
export function carriesStaleLegacyLimit(view: RiskView): boolean {
  if (!view.policyId || !view.policyId.startsWith("EDA1_FRACTIONAL")) return false;
  return view.rows.some(
    (row) =>
      (row.cap !== null && (Math.abs(row.cap - 0.05) < 1e-9 || Math.abs(row.cap - 0.3) < 1e-9)) ||
      (row.target !== null && (Math.abs(row.target - 0.05) < 1e-9 || Math.abs(row.target - 0.3) < 1e-9)),
  );
}
