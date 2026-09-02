/**
 * The Shadows workspace's composition: two observers, one comparison table,
 * and the rule that decides when a figure is a conclusion and when it is not.
 *
 * Both observers record hypothetical books compounded from a normalized 100
 * with no costs. Their raw counts - bars, decisions, exposure, turnover - are
 * always shown. Their return and drawdown figures are shown as numbers only
 * once the observer's own sample threshold says the sample can carry one;
 * below it the cell reads INSUFFICIENT FOR PERFORMANCE CONCLUSION, with the
 * raw figure beside it labelled as raw, so nobody reads a week of bars as a
 * result.
 *
 * Deliberately free of React so the test suite can run it directly.
 */

import type { A1BOverview } from "./a1b";
import type { ShadowOverview } from "./shadow";

export const INSUFFICIENT = "INSUFFICIENT FOR PERFORMANCE CONCLUSION";

export interface ComparisonCell {
  /** The text to render. */
  text: string;
  /** A raw figure shown beside an insufficient-sample label, if any. */
  raw: string | null;
  /** True when `text` is a measured value rather than a label. */
  conclusive: boolean;
}

export interface ComparisonRow {
  key: string;
  label: string;
  hint: string;
  eda1: ComparisonCell;
  a1b: ComparisonCell;
  /** True for rows that are performance claims and gated on sample size. */
  performance: boolean;
}

export interface ShadowComparison {
  rows: ComparisonRow[];
  /** True when either observer's sample is below its own threshold. */
  insufficient: boolean;
  warning: string;
}

const DASH = "—";

function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  const magnitude = (Math.abs(value) * 100).toFixed(digits);
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  return `${sign}${magnitude}%`;
}

function plain(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  return value.toFixed(digits);
}

function cell(text: string): ComparisonCell {
  return { text, raw: null, conclusive: true };
}

function gated(value: string, sufficient: boolean): ComparisonCell {
  return sufficient
    ? { text: value, raw: null, conclusive: true }
    : { text: INSUFFICIENT, raw: value, conclusive: false };
}

function period(first: string | null | undefined, last: string | null | undefined): string {
  if (!first || !last) return DASH;
  return `${first.slice(0, 10)} → ${last.slice(0, 16).replace("T", " ")}`;
}

/** Build the EDA-1 Shadow vs A1-B U30 table from the two payloads. */
export function compareShadows(
  eda1: ShadowOverview | null,
  a1b: A1BOverview | null,
): ShadowComparison {
  const eda1Book = eda1?.hypothetical.eda1 ?? null;
  const eda1Steps = eda1 ? eda1.hypothetical.steps : null;
  const a1bSteps = a1b ? a1b.hypothetical.steps : null;
  const eda1Sufficient = Boolean(eda1?.comparison.sample_is_sufficient);
  const a1bSufficient = Boolean(a1b?.hypothetical.sample_is_sufficient);

  const rows: ComparisonRow[] = [
    {
      key: "period",
      label: "Observation period",
      hint: "First and last recorded bar in the observer's record.",
      eda1: cell(period(eda1?.hypothetical.first_bar, eda1?.hypothetical.last_bar)),
      a1b: cell(period(a1b?.hypothetical.first_bar, a1b?.hypothetical.last_bar)),
      performance: false,
    },
    {
      key: "steps",
      label: "Observed steps",
      hint: "Bar-to-bar returns the hypothetical book has been compounded over.",
      eda1: cell(plain(eda1Steps)),
      a1b: cell(plain(a1bSteps)),
      performance: false,
    },
    {
      key: "decisions",
      label: "Decision count",
      hint: "Recorded per-symbol observations.",
      eda1: cell(plain(eda1?.comparison.bars_compared)),
      a1b: cell(plain(a1b?.summary.observations)),
      performance: false,
    },
    {
      key: "universe",
      label: "Universe",
      hint: "Symbols observed each cycle.",
      eda1: cell(eda1 ? `${eda1.service.universe.length} symbols` : DASH),
      a1b: cell(a1b ? `${a1b.service.universe_size} symbols` : DASH),
      performance: false,
    },
    {
      key: "exposure",
      label: "Average exposure",
      hint: "Mean fraction of the hypothetical book held long across observed steps.",
      eda1: cell(pct(eda1Book?.long_exposure_fraction, 1)),
      a1b: cell(pct(a1b?.hypothetical.average_exposure, 1)),
      performance: false,
    },
    {
      key: "current",
      label: "Current simulated exposure",
      hint: "Fraction of the hypothetical book held long at the latest bar.",
      eda1: cell(
        eda1 && eda1Book
          ? `${pct(eda1Book.current_long_symbols.length / Math.max(1, eda1.service.universe.length), 1)} · long ${eda1Book.current_long_symbols.length}/${eda1.service.universe.length}`
          : DASH,
      ),
      a1b: cell(
        a1b?.hypothetical.current_exposure !== null && a1b?.hypothetical.current_exposure !== undefined
          ? `${pct(a1b.hypothetical.current_exposure, 1)} · long ${a1b.hypothetical.long_symbols}/${a1b.service.universe_size}`
          : DASH,
      ),
      performance: false,
    },
    {
      key: "turnover",
      label: "Simulated turnover",
      hint: "EDA-1: stance changes per step. A1-B: absolute weight change per step.",
      eda1: cell(eda1Book?.turnover_per_step === null || eda1Book?.turnover_per_step === undefined ? DASH : `${eda1Book.turnover_per_step.toFixed(3)} stance Δ/step`),
      a1b: cell(a1b?.hypothetical.turnover_per_step === null || a1b?.hypothetical.turnover_per_step === undefined ? DASH : `${pct(a1b.hypothetical.turnover_per_step, 2).replace("+", "")} weight/step`),
      performance: false,
    },
    {
      key: "disagreements",
      label: "Hard disagreements",
      hint: "EDA-1: bars where its stance differed from V3's. A1-B has no execution counterpart to disagree with.",
      eda1: cell(plain(eda1?.comparison.stance_disagreement_count)),
      a1b: cell("N/A · no counterpart"),
      performance: false,
    },
    {
      key: "regime",
      label: "Current regime",
      hint: "EDA-1 participation state resolved by each observer for the current session.",
      eda1: cell(eda1?.regime.state ?? DASH),
      a1b: cell(a1b?.regime.state ?? DASH),
      performance: false,
    },
    {
      key: "return",
      label: "Simulated return",
      hint: "Hypothetical, frictionless, from a normalized 100. Not account equity.",
      eda1: gated(pct(eda1Book?.cumulative_return), eda1Sufficient),
      a1b: gated(pct(a1b?.hypothetical.cumulative_return), a1bSufficient),
      performance: true,
    },
    {
      key: "drawdown",
      label: "Max drawdown",
      hint: "Worst peak-to-trough of the hypothetical book over the observed steps.",
      eda1: gated(pct(eda1Book?.max_drawdown), eda1Sufficient),
      a1b: gated(pct(a1b?.hypothetical.max_drawdown), a1bSufficient),
      performance: true,
    },
    {
      key: "benchmark",
      label: "Equal-weight benchmark",
      hint: "Every universe symbol held throughout, same period, same convention.",
      eda1: gated(pct(eda1?.hypothetical.benchmark_return), eda1Sufficient),
      a1b: gated(pct(a1b?.hypothetical.benchmark_return), a1bSufficient),
      performance: true,
    },
  ];

  const insufficient = !eda1Sufficient || !a1bSufficient;
  return {
    rows,
    insufficient,
    warning:
      a1b?.hypothetical.sample_warning ??
      eda1?.comparison.sample_warning ??
      "Shadow samples are far too small for any performance conclusion.",
  };
}
