# Decision V4 — the ML probability engine

V4 reads the same seven measurements V2 and V3 read, and turns them into a
**calibrated probability** rather than a rule-based score. It exposes the shared
Decision Engine contract, plus one addition — `ProbabilityAssessment` — which is
what a V5 ensemble will consume.

Nothing in this milestone activates in production, changes the paper submission
gate, unmasks equity, or alters risk limits, the account halt, reconciliation,
or at-most-once semantics. A trained artifact is registered at the
`experimental` stage; there is no stage that makes a model trade.

---

## 1. Where the two halves live, and why

| Package | Holds | May use |
| --- | --- | --- |
| `autotrader.decision.probability` | The trained model as a **value**, and all scoring arithmetic | pandas, `math`, stdlib |
| `autotrader.decision.v4` | The engine: bars → features → probability → `DecisionResult` | pandas, `math`, stdlib |
| `autotrader.ml.v4` | Training: datasets, fitting, walk-forward, calibration, artifacts | numpy, the filesystem |

The decision package is fenced off by tests older than this milestone. It may
import only `pandas`, `math` and a short allowlist
(`tests/test_decision_contract.py::ALLOWED_IMPORT_ROOTS`); it may not open a
file, read a clock, or name a broker type. So the trained parameters travel to
it as a **record**, and the import arrow runs one way only:

    autotrader.ml.v4  ───imports───▶  autotrader.decision.probability
    (fits, numpy, disk)                (scores, pure Python)

`autotrader.ml` may import `autotrader.decision`; the reverse is forbidden by
`tests/test_ml_contracts.py::test_only_the_cli_reaches_into_the_ml_package`.
Neither guard was modified.

**One scoring implementation.** The obvious failure of that split is a model
that trains under one arithmetic and serves under another. numpy appears inside
the fitting loops and nowhere after them: the moment an estimator exists it
becomes a decision-layer value, and every probability quoted downstream — the
walk-forward metrics, the calibration curve, the artifact's test metrics — is
produced by `ProbabilityArtifact.probability_up`, the same method a live bar
goes through. `test_the_trained_model_and_the_live_engine_agree_on_the_same_bar`
pins it anyway.

---

## 2. The feature set

V4 reads exactly `autotrader.decision.features.SCORED_FEATURES`:

`ema_spread_z`, `ema_slope_z`, `rsi_centered`, `macd_hist_z`, `return_z`,
`volatility_ratio`, `volume_ratio`

All seven are unit-free by construction — each is either a ratio against the
market's own trailing baseline or a standardization against its own trailing
spread. Three consequences, and all three are why the set was reused rather than
extended:

1. They are comparable across BTC/USD and SPY, so one model is fitted per asset
   class rather than per symbol.
2. They are the same numbers V2 and V3 judge, so a V5 ensemble combines two
   readings of one set of measurements rather than two different views.
3. The raw levels deliberately left out — `ema_fast`, `macd`, `atr`, `close` —
   carry the price scale of whichever symbol and year they came from, which a
   linear model will happily fit and then fail to generalize from.

V4's **feature version is the decision layer's** `FEATURE_SCHEMA_VERSION`, and
an artifact is refused against any other. Training computes these features with
the same function the live engine calls, so there is no second feature layer to
drift.

---

## 3. No look-ahead, completed bars only

| Guard | Where |
| --- | --- |
| No negative shift, centred window, backfill or reversal anywhere in the decision package | `test_decision_contract.py::test_no_decision_module_contains_a_look_ahead_construct` (walks every file, so V4's two new modules are covered automatically) |
| Same, for the training module | `test_ml_v4_training.py::test_the_training_module_contains_no_look_ahead_construct` |
| Truncating the bars changes no earlier probability | `test_decision_v4.py::test_truncating_the_bars_changes_no_earlier_probability` |
| Appending a bar changes no earlier decision | `test_decision_v4.py::test_a_later_bar_cannot_alter_a_completed_bar_s_decision` |
| No feature column may declare a forward horizon | `ColumnSpec` refuses it for the feature role; `test_no_feature_column_declares_a_forward_horizon` |
| Every row stamped with `knowable_at` = feature bar + 15m | `test_every_row_is_stamped_with_when_it_could_first_have_existed` |
| Entry at least one bar after the feature bar | `autotrader.ml.labels` refuses `entry_offset_bars=0` (docs/SPEC.md 6F) |

The decision package cannot read a clock, so "this bar is complete" is the
caller's guarantee, stated the same way the ML contract states it:
`ProbabilityAssessment.knowable_at` is the feature bar's close, and a consumer
can refuse anything that would have arrived from the future.

---

## 4. Temporal validation and leakage

Model selection runs on `autotrader.ml.splits.walk_forward_folds` — anchored
folds that train on the past and are graded on their own future. There is no
shuffle in the module and no parameter that would enable one; a source guard
asserts the absence of `shuffle`, `KFold`, `train_test_split`, `permutation`,
`default_rng` and `RandomState`.

Three separate guards, because they fail differently:

- **Purging.** A training row survives only if its label had fully resolved
  before the first test bar began — cut on `label_knowable_at`, not on
  `feature_timestamp`, because only the former can detect the violation.
- **Embargo.** `--embargo-bars` drops further bars at each boundary, covering
  the autocorrelation purging cannot.
- **Fit on the training fold only.** The standardizer is refitted inside every
  fold from that fold's training rows.
  `test_perturbing_the_test_rows_moves_no_fitted_parameter` multiplies every
  test-split feature by a thousand and requires the fitted artifact to be
  byte-identical.

Calibration is fitted on the **validation** split: never training, where the
model has already seen the outcomes, and never test, which is straightforward
leakage.

---

## 5. The model choice, and the evidence for it

`autotrader ml v4-compare` fits three candidates on identical folds:

| Candidate | Family | Why it is on the list |
| --- | --- | --- |
| `baseline-frequency` | class frequency | The floor. A model that cannot beat the base rate has found nothing. |
| `logistic-l2` | L2 logistic regression | The robust tabular baseline. Readable coefficients, exact per-feature attribution. |
| `gradient-boosted` | 60 depth-3 trees | The one step up, for a genuine comparison. |

Selection rule, applied in order — all three are about refusing complexity that
has not been paid for:

1. Beat the baseline's mean walk-forward log loss by `MATERIAL_LOG_LOSS_IMPROVEMENT`
   (0.002 nats) or be inadmissible.
2. The best admissible mean log loss sets the standard.
3. Among candidates within 0.002 of that standard, the **simplest family wins**.

If nothing clears the bar, the baseline is returned and the rationale says so.
That is a real outcome, not a failure: a fair walk-forward on a market that
offered no edge over this horizon should conclude exactly that, and a function
that quietly promoted the least-bad candidate instead is the mechanism by which
noise reaches production.

### Recorded result

Command:

```
autotrader ml v4-compare <bars>.parquet --symbol BTC/USD \
    --folds 5 --embargo-bars 8 --seed 20260828
```

| candidate | log loss | Brier | ECE | AUC |
| --- | --- | --- | --- | --- |
| baseline-frequency | 0.693119 | 0.2500 | 0.0173 | 0.5000 |
| logistic-l2 | **0.199990** | 0.0582 | 0.0604 | 0.9837 |
| gradient-boosted | 0.214430 | 0.0534 | 0.1276 | 0.9911 |

**Selected: `logistic-l2`.** The boosted ensemble ranks marginally better (AUC
0.9911 against 0.9837) and is worse on both metrics that matter for a
probability — higher log loss and roughly double the calibration error. It is
also the more complex family. It loses on every clause of the rule at once.

The full record, fold by fold, is written to
`$AUTOTRADER_QA_REPORTS/v4-comparisons/`.

### What this evidence does and does not establish

**The only 15-minute bar file in this workspace is synthetic** — 2000
generated bars under `datasets/synthetic/`. An AUC of 0.98 is not a market
edge; it is a generated series being easy to predict, and it would be
dishonest to present it as anything else.

So this comparison establishes:

- the harness is correct end to end — folds advance, purging holds, the
  standardizer is fold-local, calibration improves log loss, the artifact
  round-trips, and the fitted model serves identically live;
- the selection rule discriminates, and on these numbers it rejected the more
  complex model rather than rubber-stamping the best headline metric.

It does **not** establish that any of these models has an edge on real
BTC/USD or SPY bars. Re-running `v4-compare` against a downloaded bar file is
the whole cost of finding out, and until that has been done, the honest reading
is: V4 ships the regularised logistic regression because nothing in this
repository justifies anything heavier — which is precisely the rule, applied to
the evidence that exists.

---

## 6. Calibration

A model that outputs 0.7 should be right about seventy per cent of the time it
says so. `autotrader.ml.calibration` ships identity and equal-width binning;
V4 adds **isotonic regression by pool-adjacent-violators**, fitted on the
validation split and stored in the artifact as the step function itself, so the
mapping applied live is the mapping that was measured.

No new dependency: PAV is about thirty lines, and thirty lines is cheaper than a
library a trading process would then carry.

Fitted values are held away from 0 and 1 by `1 / 2n`, where *n* is the
validation row count. A block that happened to contain no positives has an
observed frequency of exactly zero, and shipping that would be the model
asserting impossibility on the evidence of a few dozen rows.

Calibration is measured, not assumed: expected calibration error and Brier score
are reported next to log loss and AUC on every fold, and
`ProbabilityAssessment.calibrated` plus a `CALIBRATION_IDENTITY` reason token
travel with every result, so a consumer can decline to size on a raw score.

---

## 7. Determinism, and its one honest limit

Nothing reads a clock, a process id, or an unseeded generator. The Newton solver
runs a fixed number of iterations to a fixed tolerance. The boosted split search
breaks ties towards the lowest feature index and then the lowest threshold, and
every source of randomness a boosting implementation usually offers — row
subsampling, column subsampling, a shuffled split order — is **absent rather
than seeded**.

Given the same frame, configuration and seed, training produces byte-identical
artifact records, and
`test_the_same_frame_seed_and_configuration_produce_the_same_artifact` asserts
it for both families.

**What cannot be promised:** bit-identical output across *different numpy
builds*. `numpy.linalg.solve` dispatches to whichever BLAS the platform
provides, and summation order in a reduction is not part of numpy's API. This
is stated in `autotrader.ml.v4`'s module docstring rather than papered over, and
the experiment record stores library versions for exactly that reason — it turns
a surprising mismatch into a diagnosable one.

---

## 8. Crypto and equity

The session semantics each asset class already uses, unchanged. V4 invents no
session rules and no thresholds.

| | Crypto | Equity |
| --- | --- | --- |
| Grid | Every 15-minute UTC boundary | Regular-session boundaries from a broker calendar |
| Session id | UTC calendar date | Exchange session date |
| Overnight gap | None | Marked by `label_spans_session_gap` |
| `SessionPolicy.WITHIN_SESSION` | Refused as meaningless | Invalidates gap-crossing intervals |
| Policy | `CRYPTO_POLICY` (2.5× volatility tolerance, 0.35 confidence floor) | `EQUITY_POLICY` (2.0×, 0.40) |

Gating reuses `scoring.decide_signal` under the same asset-class policy V2 and
V3 use — the same confidence floor, the same hold band, the same asymmetric
refusal to enter a high-volatility regime. V4 therefore differs from V2 in
exactly one respect, how the score was arrived at, and a comparison between them
measures that rather than a pile of incidental threshold changes.

---

## 9. Score, confidence, and the contract V5 consumes

`score = 2p − 1`, `confidence = |2p − 1|`. An even-odds model reports zero
confidence rather than half of it. For a single calibrated binary probability
the two coincide in magnitude, which is a fact about what a probability is
rather than an oversight: unlike V2, where confidence measures whether five
separate factors agreed, there is only one quantity here and it cannot
corroborate itself.

`ProbabilityAssessment` carries what an ensemble needs:

```python
assessment = ProbabilityV4Engine.for_symbol("BTC/USD", artifact).assess(bars)
assessment.probability_up  # calibrated, or None when unavailable
assessment.model_version  # which trained model
assessment.feature_version  # which column contract it was fitted under
assessment.calibrated  # whether that probability is calibrated at all
assessment.reasons  # stable machine tokens
assessment.knowable_at  # feature bar + 15m
```

`probability_up` is `None` exactly when `available` is false. A bar with too
little history has no probability, and reporting even odds for one would be a
measurement rather than an absence — the one substitution that would be
invisible to every consumer downstream.

V5 combines this with `MultiTimeframeV3Engine.decide(bars).score` without
reaching inside either engine.

---

## 10. Artifacts

A trained model is a JSON record: coefficients or trees, the standardizer, the
calibration steps, and the provenance. `artifact_version` is the SHA-256 of
those bytes, recomputed by the registry rather than taken on trust.

Recorded on every artifact: model version, family, feature version and the exact
column list, label spec id, training window (interval, row count, symbols, asset
class), code revision, hyperparameters, seed, calibration method, and test
metrics. Registration is immutable — the same `(name, version)` twice is
refused — and the stage is `experimental`.

---

## 11. CLI

```
autotrader ml v4-compare <bars>.parquet --symbol BTC/USD [--sessions cal.json]
                         [--folds N] [--embargo-bars N] [--seed N]

autotrader ml v4-train   <bars>.parquet --symbol BTC/USD --model-version 1.0.0
                         [--sessions cal.json] [--no-calibrate]
                         [--git-sha SHA] [--git-branch BRANCH]
```

`v4-train` selects its candidate from the same comparison `v4-compare` prints,
so a training run cannot pick a model the evidence did not support. Git
provenance is supplied by the caller: the ML package runs no subprocess, which
is what keeps a library that fits models from being able to execute anything.

---

## 12. What was deliberately not built

- No V5 ensemble. Reconciling a deterministic score with a calibrated
  probability is a modelling decision nobody has made yet.
- No runtime wiring, no activation switch, no registry `PRODUCTION` stage.
- No hyperparameter search. Three candidates with stated hyperparameters, and a
  rule that prefers the simpler one.
- No model heavier than gradient-boosted trees. Reaching for one requires
  walk-forward evidence in this repository that these three cannot produce, and
  §5 is that evidence not existing yet.
