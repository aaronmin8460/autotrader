# Equity V4 Label/Horizon Study — Predeclared Design

**Written before any horizon was scored.** This file freezes the horizon set, the
evaluation methodology, and the winner rule. Committed to the research branch and
stored here before the first training run, so the selection rule cannot drift
toward whatever the results turn out to be.

| | |
|---|---|
| Date designed | 2026-08-29 |
| Base branch | `integration/final-development-candidate` @ `aee7a77af090fd9d3dd60f66c400fa2360f2f478` (verified against origin) |
| Research branch | `research/equity-v4-label-horizon` |
| Worktree | `/Volumes/AUTOTRADER_QA/worktrees/equity-v4-horizon` |
| Pilot evidence | `research/equity-spy-qqq-historical-pilot` @ `c98ca36` (reference only) |
| Universe | SPY, QQQ only |
| Status | RESEARCH ONLY. Nothing is deployed, unmasked, or sent to a broker or the VPS. |

## 1. Question

Is V4 selecting the null model in 11 of 12 pilot window-models primarily because
its 4-base-bar label horizon (~1 trading hour) is too short/noisy to carry
predictable signal — and if so, which single alternative horizon is defensible
enough to freeze for the 10-symbol full historical evaluation?

A negative result (no horizon helps) is a valid outcome and will be classified,
not retried.

## 2. Verified inputs

Re-verified in this session before design freeze:

- `origin/integration/final-development-candidate` = `aee7a77…` (matches).
- Pilot session frames match their recorded content digests:
  - SPY `d409cd3b1bdf7847…` (36,751 rows, 2021-01-04..2026-08-28)
  - QQQ `c53d984e588955fa…` (36,752 rows, same range)
- Pilot JSON confirms 11/12 window-models selected `class_frequency`; the one
  exception (QQQ/2023-summer, logistic, null 0.693374 → 0.690564, margin
  0.00281) emitted exactly 2 BUY signals at confidence 0.999678, from an
  isotonic calibration with 11 distinct output levels over 1,054 bars.
- The pilot proved the raw frames are byte-identical to split-adjusted
  re-downloads (SPY/QQQ had no splits), so these frames ARE the split-adjusted
  research data path for these two symbols. No redownload is needed or made.

## 3. Horizon set — FROZEN

Four candidate horizons, in 15-minute regular-session base bars:

| Horizon | Approx. trading time | Predicted % of labels crossing a session gap (full session, analytic) |
|---|---|---|
| 4 (current) | 1 trading hour | 5/26 ≈ 19% |
| 8 | 2 trading hours | 9/26 ≈ 35% |
| 16 | 4 trading hours | 17/26 ≈ 65% |
| 26 | one full regular session | 100% |

No candidate is replaced; no candidate will be added after evaluation starts,
whatever the first results look like. The analytic gap-crossing fractions above
are predictions to be verified by measurement (the pilot measured ~19% at h=4,
which matches).

**Session semantics (one consistent causal definition).** A horizon of N bars
means "N completed regular-session 15-minute bars later on the exchange
calendar", exactly as `autotrader.ml.labels` + the session-aware `equity_grid`
already define it: entry at the OPEN of grid bar t+1, exit at the OPEN of grid
bar t+1+N, where grid positions step through regular-session bars only
(09:30–16:00 America/New_York, broker calendar; weekends, holidays, early
closes and DST handled by the calendar; no bar is invented while the market is
closed). A holding period that crosses one or more session gaps is allowed and
flagged (`SessionPolicy.SPAN_SESSIONS`, the shipped default); a missing
provider bar invalidates the interval rather than being stepped over. At h=26
every label spans at least one overnight gap — that is the honest meaning of a
one-session horizon for a regular-hours strategy, and it is documented rather
than avoided.

## 4. Label family — FROZEN

The CURRENT V4 label semantics at every horizon: binary direction
(`v4-direction`), threshold 0.0, entry open t+1, exit open t+1+h,
SPAN_SESSIONS. Only `horizon_bars` varies in the primary comparison, so the
horizon effect is isolated from any label-definition effect.

One optional secondary diagnostic is permitted AFTER the primary comparison,
only if the primary evidence strongly suggests the label definition itself
(not the horizon) is the problem. It will be explicitly marked diagnostic and
cannot become the winner.

## 5. Evaluation structure

**Windows.** The pilot's six chronological scoring windows, unchanged
(2021-autumn, 2022-spring, 2023-summer, 2024-yearend, 2025-spring,
2026-summer).

**Selection set vs holdout.** Horizon selection uses only the FIRST FIVE
windows (10 symbol×window cells per horizon). 2026-summer (both symbols) is
the untouched holdout: no 2026-summer result at h∈{8,16,26} is computed or
inspected until the winner rule has been applied to the selection set.
Honesty note: h=4 results for 2026-summer were already published by the pilot
(null selected, both symbols) and have been seen; the holdout therefore guards
the alternative horizons, which is where the selection risk lives.

**Walk-forward (per symbol × window × horizon).** Identical to the pilot
except the outer train→score gap scales with the horizon:

- training rows: everything before `first_scored_bar − (h + 26) − 1`
- outer gap: h bars (label resolution) + 26 bars (one full regular session
  embargo)
- inner model selection: the existing `compare_candidates` (3 candidates:
  class-frequency null, logistic-L2, gradient-boosted; 4 anchored sub-folds;
  `embargo_bars=26`; purge on `label_knowable_at`, which scales with the
  horizon automatically)
- selection rule: the existing `select_candidate`, materiality gate 0.002 mean
  log loss, UNCHANGED
- final model: the existing `train_model` (60/20/20 temporal split, isotonic
  calibration on validation), seed 0, deterministic

**Out-of-sample (OOS) window evaluation.** Ground-truth labels for window bars
are computed on the full-frame grid (future bars used only as outcome truth,
never as model input; the artifact scoring a window was trained strictly
before it). Per cell, report OOS on the window's bars:

- log loss / Brier / ECE / ROC AUC of the SELECTED artifact
- the same for the pure null (constant = training-frame base rate,
  uncalibrated)
- OOS gain = LL_null − LL_selected (positive = selected generalized)
- prediction and confidence distributions
- shadow diagnostics: logistic and GBM artifacts trained on the same data even
  where the null was selected, marked SHADOW, never used by the winner rule

**Cross-horizon comparability.** Primary OOS metrics are computed on the
common evaluable subset: window bars whose labels are valid at ALL four
horizons (the last ~27 bars of the final window drop for every horizon alike).
Full per-horizon sets are reported as secondary.

## 6. Overlapping-label audit

For horizon h, labels of rows i and j overlap iff |i−j| < h (grid positions).
Per horizon, the study documents: the overlap factor h, the measured purge
counts at every inner-fold and outer boundary (expected ≈ h + entry offset at
each boundary), and passes `assert_no_leakage` plus
`assert_no_forward_information` on every fold/model. Explicit tests prove that
no training row's label resolves inside its validation/test interval at any
horizon, including across weekend, holiday, early-close and DST boundaries.

## 7. Calibration audit (per trained model)

- validation rows used to fit isotonic calibration
- number of distinct calibrated output levels
- support (validation rows) behind each isotonic step, esp. top and bottom
- extreme confidence: any calibrated p ≥ 0.99 or ≤ 0.01 traced to its step
  support; produced-by-thin-bin (<30 validation rows) is flagged
- Brier score and ECE (existing `autotrader.ml.calibration` implementations)
- comparison against the identity-calibration (uncalibrated) scores where
  useful

## 8. V5 downstream diagnostic

For each horizon: the EXISTING V5 policy, completely unchanged, driven per
window with that horizon's per-window V4 artifact, 3,000-bar lookback, over
the same six windows; replayed under frictionless / equity-marketable / stress
cost models from one stored decision series. Report per horizon: trade count,
turnover, realized + unrealized PnL, forced-final-liquidation result,
realistic-cost net, max drawdown, V3/V5 disagreement, and bars where the V4
input changes V5's signal vs the h=4 baseline. The pilot's stored h=4 V5
decisions are reused only if a sampled recomputation reproduces them exactly;
otherwise recomputed. Economic replay is confirmation/falsification only —
the horizon is selected on predictive evidence first.

If system load from the concurrent crypto study is high, the V5 replay stage
waits; preparation and tests come first. ≤2 workers throughout.

## 9. Benchmarks

Null (class frequency), current V4 (h=4 column), V3 (pilot stored decisions —
horizon-independent, reused after sampled verification), current V5 (= h=4
V5), cash (0%), buy-and-hold per window. V4 is not required to beat
buy-and-hold; its job is incremental predictive information.

## 10. Winner rule — FROZEN BEFORE RESULTS

A horizon h* ∈ {8, 16, 26} replaces 4 only if ALL of P1–P10 hold on the
selection set (5 windows × 2 symbols = 10 cells/horizon):

- **P1 Materiality.** h* selects a non-null model (production gate, 0.002) in
  ≥ 4 of 10 cells, and in strictly more cells than h=4.
- **P2 Both symbols.** ≥ 2 non-null cells on SPY AND ≥ 2 on QQQ.
- **P3 Multiple windows.** Non-null cells appear in ≥ 3 of the 5 windows.
- **P4 Not one-window-dominated.** Excluding the window with the largest OOS
  gain, the mean OOS gain over the remaining non-null cells stays > 0.
- **P5 Calibration credible.** Median distinct calibrated levels over
  non-null models ≥ 5, and median OOS ECE of selected models ≤ null's + 0.02.
- **P6 No thin-bin confidence.** Every calibrated p ≥ 0.99 or ≤ 0.01 emitted
  on scored bars comes from an isotonic step with ≥ 30 validation rows;
  a cell violating this does not count toward P1–P3.
- **P7 Neighbor coherence.** h* is not an isolated spike: an adjacent horizon
  in the set also shows ≥ 2 non-null cells or a positive mean OOS gain.
- **P8 Leakage clean.** All purge/embargo/no-forward-information audits and
  tests pass at h*.
- **P9 V5 sanity.** Under the realistic equity cost model, V5-with-h* shows no
  pathology: turnover ≤ 3× V5-with-h4, max drawdown not worse by > 10
  percentage points, forced-final-liquidation result well-formed.
- **P10 Holdout.** Only computed after P1–P9: on 2026-summer, h* must not
  reverse the thesis — (a) at least one symbol selects non-null OR mean OOS
  gain ≥ 0, and (b) h*'s mean OOS gain on the holdout ≥ h=4's.

**Tie-break:** if two horizons satisfy all criteria with similar evidence, the
SHORTER wins. Total return is never the selector.

## 11. Failure rule

If no horizon satisfies the rule, no new search is invented. The result is
classified as one of: HORIZON_NOT_THE_PROBLEM, LABEL_SEMANTICS_PROBLEM,
FEATURE_INFORMATION_LIMIT, CALIBRATION_INSTABILITY, SAMPLE_SIZE_LIMIT,
MIXED/INCONCLUSIVE — with the evidence for the classification — and the most
defensible baseline is frozen for the 10-symbol run (expected: retain h=4 and
keep V4 null-capable).

## 12. Compute & checkpointing

- ≤ 2 worker processes; heavy scoring deferred while the concurrent crypto
  study saturates the machine.
- Every scoring stage checkpoints per symbol×window×horizon cell (JSON +
  parquet under this directory); resume is proven by test before any long run
  (completed cells skipped, no duplicates).
- Seed 0 everywhere; training determinism re-verified on one cell.

## 13. Outputs

All heavy artifacts under `/Volumes/AUTOTRADER_QA/reports/equity-v4-label-horizon/`;
final report at `/Volumes/AUTOTRADER_QA/reports/equity-v4-label-horizon-study.md`;
code and tests only in Git on `research/equity-v4-label-horizon`.
