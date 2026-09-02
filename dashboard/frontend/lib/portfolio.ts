/**
 * Portfolio arithmetic: weights, allocation slices, P&L contribution, and the
 * target-vs-actual join.
 *
 * Every figure here is derived from one broker read (positions, market
 * values, equity, cash) and, for targets, from the paper runtime's own
 * recorded decisions. Nothing is invented: a position without a market value
 * has no weight, a symbol without a recorded target has no target, and the
 * functions say so with `null` rather than a zero.
 *
 * Deliberately free of React so the test suite can run it directly.
 */

import type { PaperTargetRow } from "./paper";
import type { AssetClass, PositionRow, PositionsPanel, PrimaryMetrics } from "./types";

export type SliceKind = AssetClass | "CASH" | "OTHER";

export interface AllocationSlice {
  label: string;
  kind: SliceKind;
  value: number;
  fraction: number | null;
}

export interface Contribution {
  symbol: string;
  asset_class: AssetClass;
  pnl: number;
  fraction: number | null;
}

export interface TargetVsActualRow {
  symbol: string;
  stance: string | null;
  action: string;
  target_weight: number | null;
  target_source: string;
  actual_weight: number | null;
  target_value: number | null;
  actual_value: number | null;
  delta_weight: number | null;
  delta_value: number | null;
  quantity: string;
  price: number | null;
  unrealized_pnl: number | null;
  last_decision_at: string | null;
  last_order_side: string | null;
  bar_timestamp: string | null;
}

/** How many slices before the tail is folded into "Other". */
export const MAX_ALLOCATION_SLICES = 14;

export function equityOf(metrics: PrimaryMetrics | null): number | null {
  return metrics && metrics.equity.available && metrics.equity.value ? metrics.equity.value : null;
}

/** A position's share of account equity, or null when either side is unknown. */
export function weightOf(row: PositionRow, equity: number | null): number | null {
  if (equity === null || equity <= 0 || row.market_value === null) return null;
  return row.market_value / equity;
}

/** Each position's weight by symbol. */
export function positionWeights(
  positions: PositionsPanel | null,
  equity: number | null,
): Record<string, number | null> {
  const out: Record<string, number | null> = {};
  for (const row of positions?.rows ?? []) out[row.symbol] = weightOf(row, equity);
  return out;
}

/**
 * The allocation: every position, then cash, largest first.
 *
 * Cash is the broker's settled cash, not equity minus positions - the two
 * differ by unsettled amounts and the chart should show what the account
 * actually holds. When there are too many positions the smallest fold into
 * one "Other" slice so the labels stay readable.
 */
export function allocationSlices(
  positions: PositionsPanel | null,
  metrics: PrimaryMetrics | null,
): AllocationSlice[] {
  const equity = equityOf(metrics);
  const rows = (positions?.rows ?? []).filter((row) => row.market_value !== null && row.market_value > 0);
  const slices: AllocationSlice[] = rows
    .map((row) => ({
      label: row.symbol,
      kind: row.asset_class,
      value: row.market_value as number,
      fraction: equity ? (row.market_value as number) / equity : null,
    }))
    .sort((left, right) => right.value - left.value);

  let folded = slices;
  if (slices.length > MAX_ALLOCATION_SLICES) {
    const head = slices.slice(0, MAX_ALLOCATION_SLICES - 1);
    const tail = slices.slice(MAX_ALLOCATION_SLICES - 1);
    const value = tail.reduce((sum, slice) => sum + slice.value, 0);
    head.push({
      label: `Other (${tail.length})`,
      kind: "OTHER",
      value,
      fraction: equity ? value / equity : null,
    });
    folded = head;
  }

  if (metrics && metrics.cash.available && metrics.cash.value !== null) {
    folded.push({
      label: "Cash",
      kind: "CASH",
      value: metrics.cash.value,
      fraction: equity ? metrics.cash.value / equity : null,
    });
  }
  return folded;
}

/** Unrealized P&L per position, from the broker's own entry price. */
export function pnlContributions(positions: PositionsPanel | null): Contribution[] {
  return (positions?.rows ?? [])
    .filter((row) => row.unrealized_pnl !== null)
    .map((row) => ({
      symbol: row.symbol,
      asset_class: row.asset_class,
      pnl: row.unrealized_pnl as number,
      fraction: row.unrealized_pnl_fraction,
    }))
    .sort((left, right) => right.pnl - left.pnl);
}

/**
 * The target-vs-actual join for the equity book.
 *
 * Target weight is the paper runtime's newest recorded decision for the
 * symbol (zero for a FLAT stance, null when no decision has been recorded);
 * actual weight is the broker market value over broker equity from the same
 * operational poll. Target market value is the target weight times *current*
 * equity, which is what the allocator would size against now.
 */
export function targetVsActual(
  targets: PaperTargetRow[],
  positions: PositionsPanel | null,
  metrics: PrimaryMetrics | null,
): TargetVsActualRow[] {
  const equity = equityOf(metrics);
  const bySymbol = new Map((positions?.rows ?? []).map((row) => [row.symbol, row] as const));
  return targets.map((target) => {
    const position = bySymbol.get(target.symbol) ?? null;
    const actualValue = position?.market_value ?? (position === null ? 0 : null);
    const actualWeight = actualValue !== null && equity ? actualValue / equity : null;
    const targetWeight = target.target_weight ?? null;
    const targetValue = targetWeight !== null && equity ? targetWeight * equity : null;
    return {
      symbol: target.symbol,
      stance: target.stance_label ?? null,
      action: target.action ?? "HOLD",
      target_weight: targetWeight,
      target_source: target.target_source ?? "NOT_RECORDED",
      actual_weight: actualWeight,
      target_value: targetValue,
      actual_value: actualValue,
      delta_weight: targetWeight !== null && actualWeight !== null ? actualWeight - targetWeight : null,
      delta_value: targetValue !== null && actualValue !== null ? actualValue - targetValue : null,
      quantity: position?.quantity ?? target.actual_quantity,
      price: position?.price ?? null,
      unrealized_pnl: position?.unrealized_pnl ?? null,
      last_decision_at: target.target_decided_at ?? null,
      last_order_side: target.last_order_side ?? null,
      bar_timestamp: target.bar_timestamp,
    };
  });
}
