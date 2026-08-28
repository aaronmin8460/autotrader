# ML Foundation (M1)

The offline machine-learning **data** foundation for a future Decision V4.

This milestone builds datasets and contracts. It does not build a trading
model, does not activate a strategy, does not submit an order, and is not
wired into any runtime. `autotrader.ml` is imported by nothing except the CLI,
and a test asserts that in both directions.

---

## 1. Scope

| In scope | Out of scope (M1) |
| --- | --- |
| Versioned feature-dataset schema | A trained V4 model |
| Historical feature dataset builder | Any ML-driven trading signal |
| Configurable label framework | Strategy activation of any kind |
| Temporal train/validation/test split with purge + embargo | Broker or execution integration |
| Probability-model interface and prediction contract | Hyperparameter search, deep learning |
| Calibration interfaces and reliability metrics | Live scoring, order sizing |
| Model artifact metadata and a filesystem registry | A registry `PRODUCTION` stage |
| Reproducible experiment metadata | Feature selection decisions |

**Relationship to docs/SPEC.md section 8.** The exclusion list bars
"machine-learning trading models" without an explicit, documented scope change.
This document is that scope change, and it is narrow: the *offline data
foundation* is in scope; an ML-generated trading signal reaching the risk
engine or the execution boundary remains out of scope and is a separate,
deliberate decision.

---

## 2. Module map

| Module | Owns |
| --- | --- |
| `autotrader.ml` | Errors, `AssetClass`, symbol-universe resolution |
| `autotrader.ml.storage` | External storage roots; the credential refusal |
| `autotrader.ml.schema` | `ColumnSpec`, `FeatureSchema`, version + fingerprint |
| `autotrader.ml.grid` | `BarGrid`: crypto boundaries vs equity sessions |
| `autotrader.ml.features` | Backward-only feature computation |
| `autotrader.ml.labels` | `LabelSpec` and the forward interval |
| `autotrader.ml.dataset` | The builder, the metadata sidecar, fingerprints |
| `autotrader.ml.splits` | Temporal split, purge, embargo, walk-forward |
| `autotrader.ml.calibration` | `Calibrator`, reliability metrics |
| `autotrader.ml.model` | `Prediction`, `ProbabilityModel`, null baseline |
| `autotrader.ml.registry` | Immutable artifacts, stages, provenance |
| `autotrader.ml.experiment` | The reproducibility record |
| `autotrader.ml.cli` | `autotrader ml ...` |

---

## 3. Dataset schema (version 1.0.0)

Column order is part of the contract: keys, provenance, features, label
metadata, label. Every column declares `lookback_bars` and `forward_bars`;
`ColumnSpec` refuses a non-label column with `forward_bars > 0`.

### Keys

| Column | Type | Meaning |
| --- | --- | --- |
| `symbol` | string | Canonical symbol, slash included for a pair |
| `feature_timestamp` | datetime64[ns, UTC] | **Start** of the newest completed bar the features read |
| `knowable_at` | datetime64[ns, UTC] | `feature_timestamp + 15m` — first instant this row could exist |

### Provenance (never model input)

`asset_class`, `grid_index`, `session_id`, `session_bar_count`,
`bars_present_in_window`.

### Features (13, all float64, all model input)

| Feature | Lookback |
| --- | --- |
| `return_1` | 2 |
| `return_4` | 5 |
| `return_16` | 17 |
| `ema_20_gap` | 20 |
| `ema_20_50_gap` | 50 |
| `realized_volatility_16` | 17 |
| `true_range_ratio` | 2 |
| `average_true_range_14` | 15 |
| `volume_ratio_32` | 32 |
| `close_position_in_bar` | 1 |
| `bars_since_session_start` | 1 |
| `session_progress` | 1 |
| `prior_bar_crosses_session_gap` | 2 |

`FEATURE_WINDOW_BARS = 50` is the longest window, and equals the strategy
layer's `SLOW_PERIOD`. Nothing is normalized against the dataset: global
scaling is look-ahead wearing a preprocessing hat, and belongs to a model
pipeline fitted on the training split alone.

### Label metadata and label

`label_entry_timestamp`, `label_exit_timestamp`, `label_knowable_at`,
`label_spans_session_gap`, `label_forward_return`, `label_valid`, `label`.

### Versioning

`FEATURE_SCHEMA_VERSION` is bumped by hand. The schema **fingerprint** is a
SHA-256 over the full column specification and changes on any edit at all.
`test_the_feature_contract_has_not_changed_without_a_version_bump` pins the
fingerprint, so redefining a feature without a version bump fails.

---

## 4. Label framework

Nothing about the target is hardcoded except one rule.

| Field | Meaning |
| --- | --- |
| `kind` | `forward_return` (continuous), `direction` (binary), `ternary` (BUY/HOLD/SELL) |
| `horizon_bars` | Bars held |
| `entry_offset_bars` | Bars between the feature bar and entry. **Minimum 1.** |
| `entry_price_column` / `exit_price_column` | `open` or `close` only |
| `threshold_mode` | `absolute` (return fraction) or `volatility` (multiple of trailing sigma) |
| `upper_threshold` / `lower_threshold` | Class boundaries |
| `volatility_column` | Defaults to `realized_volatility_16`, a backward-looking feature |
| `session_policy` | `span_sessions` (flagged) or `within_session` (refused) |

### The one non-configurable rule

`entry_offset_bars >= 1`. A decision made from bar *t* cannot be filled inside
bar *t*: bar *t*'s open is already in the past by the time bar *t* closes.
This is the backtester's `signal on bar t -> fill at bar t+1` rule and
docs/SPEC.md section 6F. `entry_offset_bars=0` is refused outright.

### The interval, per row

```
feature bar t            features end here; knowable_at = t + 15m
t + entry_offset         label_entry_timestamp  (price = entry_price_column)
t + entry_offset + h     label_exit_timestamp   (price = exit_price_column)
                         label_knowable_at = label_exit_timestamp + 15m
```

`LabelSpec.describe()` renders this in one sentence and is stored verbatim in
every dataset's metadata sidecar. `label_forward_return` is stored whatever the
kind, so a threshold can be re-examined without rebuilding.

Rows whose horizon runs past the end of the grid, whose entry or exit bar was
never published, or which the session policy excludes get `label_valid=False`
and a null label. **Nothing is imputed.**

---

## 5. Sessions: crypto and equity are not the same clock

| | Crypto | Equity |
| --- | --- | --- |
| Grid | Every 15-minute UTC boundary | Regular-session bars from a broker calendar |
| `has_session_gaps` | `False` | `True` |
| `session_id` | UTC calendar date (a label, not a break) | Exchange session date |
| `session_bar_count` | 96 | 26 full day, 14 on a 13:00 early close |
| Midnight | Not a gap | n/a |
| Next bar after 15:45 ET | n/a | 09:30 ET next session |

The equity grid comes from an explicit session calendar, never inferred from
which bars are in the file: a holiday and a provider outage look identical in
Parquet, and an early close looks like a full day with six missing bars.

`StaticMarketCalendar` satisfies the existing `MarketCalendar` protocol.
Snapshot the broker's calendar once, in your own script, and store it:

```python
from autotrader.execution.equity import AlpacaMarketCalendar
from autotrader.ml.grid import StaticMarketCalendar, write_sessions

calendar = StaticMarketCalendar.from_calendar(
    AlpacaMarketCalendar(...), date(2024, 1, 1), date(2026, 8, 28)
)
write_sessions(Path("sessions.json"), calendar.sessions)
```

`autotrader.ml` never imports that module. The snapshot is the seam.

### Missing bars

Bars are reindexed onto the grid; an unpublished interval is a row with NaN
prices and `is_present=False`. Every rolling window requires a full complement
of observations, so a hole propagates as NaN rather than being bridged.
`bars_present_in_window` counts what was really there, and rows below
`minimum_bars_present_in_window` (default 50) are dropped. **No price is ever
forward-filled.**

---

## 6. Temporal split

Cuts are along the time axis. There is no shuffle parameter.

- **Purge (not optional).** A training row survives only if
  `label_knowable_at <= ` the first feature bar of the next split. This is what
  ordering alone does not catch: overlapping labels mean a training row's
  outcome can be decided by prices inside the validation window.
- **Embargo (a modelling judgement).** `embargo_bars` drops that many further
  grid positions before each boundary. Default 0.
- **Snap to session (default on).** Boundaries move forward to a session edge,
  so no session is divided between splits.
- **Only labelled rows are split**; the unlabelled tail is reported and excluded.
- `assert_no_leakage(split)` re-checks the property directly.
- `walk_forward_folds(...)` applies the same guards per anchored fold — the
  primitive V4's model-selection evidence should be produced from.

---

## 7. Model contract

```python
Prediction(
    model_version, artifact_version, feature_schema_version, label_spec_id,
    symbol, asset_class, timestamp, knowable_at,
    probability_down, probability_up, probability_neutral,   # neutral: None if binary
    calibrated_confidence,
)
```

Validated on construction: probabilities finite and in `[0, 1]`, summing to 1
within `1e-6`; `knowable_at == timestamp + 15m`; timestamps UTC-aware. An
invalid prediction cannot exist.

`ProbabilityModel` is a Protocol: `fit(features, labels, *, seed)`,
`predict_proba(features)`, `hyperparameters()`. `fit` must be deterministic
given `(features, labels, seed)` and the hyperparameters.

`ClassFrequencyModel` is the **null baseline** — it predicts the training class
frequencies and ignores its features. It exercises the contract without a new
dependency and is the floor any candidate has to clear.

Calibration: `IdentityCalibrator` (the honest default — "these are raw scores")
and `BinnedCalibrator` (dependency-free reliability binning). Isotonic and
Platt scaling slot into the same `Calibrator` protocol when a dependency is
justified. `reliability_table`, `expected_calibration_error` and `brier_score`
are the metrics. **Fit a calibrator on validation data only.**

---

## 8. Registry

Layout under `AUTOTRADER_QA_MODELS/registry/`:

```
<model_name>/<model_version>/
    artifact.json     immutable provenance record
    stage.json        current stage + append-only history
    model.<ext>       the model file
```

- `artifact_version` is the SHA-256 of the model file. Identity is bytes.
- Registering the same `(name, version)` twice is **refused**, not overwritten.
- `verify()` re-hashes the stored file against its record.
- Stages: `EXPERIMENTAL`, `CANDIDATE`, `ARCHIVED`. **There is no `PRODUCTION`
  stage and no `activate()`.** A test asserts both absences by name.
- There is no index file; the registry is the directory tree.

---

## 9. Experiment records

`experiment_id` is a SHA-256 over exactly the determinants of the outcome:
dataset fingerprints, schema version + fingerprint, label spec, split spec,
model name/version, hyperparameters, seed. `created_at`, notes and metrics are
excluded — running the same experiment later does not make it a different
experiment, and results are not inputs.

A `seed` is **required**, not defaulted.

Git provenance is supplied by the caller (`GitProvenance`), so no library
module here runs a process. `dirty` is three-valued: `None` means "could not
check", which is different from `False`.

---

## 10. Storage

| Variable | Holds |
| --- | --- |
| `AUTOTRADER_QA_DATASETS` | Built feature datasets + metadata sidecars |
| `AUTOTRADER_QA_MODELS` | `registry/` — artifacts and provenance |
| `AUTOTRADER_QA_REPORTS` | `experiments/` — experiment records |

There is **no fallback root**. An unset or unmounted variable raises with the
remedy in the message. The root is never created: `mkdir -p` on an unmounted
mount point silently produces an ordinary directory on the internal disk, which
is the failure this policy exists to prevent.

Every JSON write is scanned for credential-shaped keys (`api_key`, `secret`,
`token`, `password`, `passphrase`, `credential`, `private_key`, `access_key`,
`authorization`) at any depth and refused before a byte reaches disk.

---

## 11. CLI

```
autotrader ml schema           print the column contract and its fingerprint
autotrader ml build-dataset    stored bars -> versioned dataset + sidecar
autotrader ml split            report a temporal split and prove it does not leak
autotrader ml registry-list    list artifacts, with hash verification
autotrader ml registry-show    one artifact's full provenance
```

Exit codes: `0` success, `1` a controlled refusal, `2` unreadable input.

---

## 12. What V4 will need from this branch

1. A bar file per symbol (`autotrader download` / `autotrader equity-download`).
2. For equities, a session calendar JSON snapshotted from the broker calendar.
3. A `LabelSpec` — the target definition is a decision, not a default.
4. `autotrader ml build-dataset` per symbol, producing a fingerprinted dataset.
5. `SplitSpec` (or `walk_forward_folds`) for leak-free evidence.
6. A `ProbabilityModel` implementation. Start with a tabular baseline measured
   against `ClassFrequencyModel`; the choice should be evidence-driven from
   walk-forward results, which do not yet exist.
7. A `Calibrator` fitted on validation only.
8. `ExperimentMetadata` with an explicit seed.
9. `ModelRegistry.register(...)` for the artifact.
10. A separate, deliberate decision — outside this package — before any
    prediction reaches the risk engine.
