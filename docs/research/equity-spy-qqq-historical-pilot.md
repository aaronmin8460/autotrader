# Equity SPY/QQQ Historical Pilot — V1–V5 Research Pipeline Validation

**Status:** research only. Nothing here was deployed, activated, unmasked, or sent to the VPS. No order was placed, cancelled, replaced or simulated against a broker. Every provider interaction was a read-only `GET` — historical bars and the market calendar.

| | |
|---|---|
| Base branch | `integration/final-development-candidate` |
| Base SHA | `aee7a77af090fd9d3dd60f66c400fa2360f2f478` (verified against `origin`) |
| Research branch | `research/equity-spy-qqq-historical-pilot` |
| Worktree | `/Volumes/AUTOTRADER_QA/worktrees/equity-historical-pilot` |
| Harness | `studies/equity_v1_v5/` (16 modules), `tests/test_study_equity_v1_v5.py` |
| Artifacts | `/Volumes/AUTOTRADER_QA/reports/equity-spy-qqq-pilot/` |
| Datasets | `/Volumes/AUTOTRADER_QA/datasets/equity-historical/` |
| Universe | SPY, QQQ only — deliberately not the ten-symbol universe |
| Compute | 2 worker processes, one per symbol, capped throughout; 61 min wall clock for both |
| Scored | 6 chronological windows, 7,028 bars/symbol, 5 engines = **70,280 decisions** |

The pilot's question is **pipeline correctness**, not which engine wins. Two symbols are enough to answer the first and are not enough to answer the second; every performance number below is labelled a diagnostic for that reason.

---

## 1. Data provenance

| Field | Value |
|---|---|
| Provider | Alpaca (`alpaca-py` 0.44.0) |
| Endpoint | `GET /v2/stocks/bars` via `autotrader.equity.data` — the repository's only stock market-data path |
| Feed | `iex` (`DataFeed.IEX`) — the Basic plan's entitlement; `sip` returns 403 without a paid subscription |
| Asset class | `us_equity` |
| Adjustment | Stored frames were downloaded `raw` (the shipped request sent no `adjustment` field); re-downloading them split-adjusted gives **byte-identical** frames, so the pilot is unaffected. The harness now defaults to `Adjustment.SPLIT`. See §16.1. |
| Timeframe | 15m base bars |
| Date semantics | Request dates are exchange days in `America/New_York`; stored timestamps are UTC |
| Session calendar | `GET /v2/calendar` via `autotrader.execution.equity.AlpacaMarketCalendar`, snapshotted to JSON |
| Session policy | Regular session only, 09:30–16:00 America/New_York, per the broker's own calendar |

The calendar was read once and written to `market_calendar_2020-01-01_2026-12-31.json` (1,759 sessions) so the study is reproducible without a live endpoint while still being the broker's calendar rather than a hardcoded holiday list. `SnapshotCalendar` satisfies the shipped `MarketCalendar` protocol, so every session rule the report exercises — `regular_session_bar_starts`, `session_bar_mask`, `session_wake_times`, `is_market_open` — is the production implementation running against the snapshot.

**Feed coverage boundary.** IEX 15m history begins around **2020-08-17** on this account — a rolling window of roughly six years, not a fixed epoch. The dataset start therefore moves forward over time, which is why the pilot pins explicit dates and records `retrieved_at_utc`. A study re-run in six months will not be able to reach 2021-01 bars.

**IEX is a single-venue feed.** It carries a real subset of consolidated volume. Bars for an interval with no IEX prints are absent rather than zero-volume. Neither is corrected for, and both are visible in the gap accounting below.

---

## 2. SPY/QQQ date ranges

Requested `2021-01-04 .. 2026-08-28` (exchange days), chosen to start well inside the coverage boundary and run to the most recent complete session.

| Symbol | First bar (UTC) | Last bar (UTC) | Sessions observed | Sessions scheduled |
|---|---|---|---|---|
| SPY | 2021-01-04T14:30:00Z | 2026-08-28T19:45:00Z | 1,419 | 1,420 |
| QQQ | 2021-01-04T14:30:00Z | 2026-08-28T19:45:00Z | 1,419 | 1,420 |

Both frames span 5 years 8 months of regular-session history.

---

## 3. Dataset quality

Download → de-duplicate → filter to regular session → re-null undefined VWAP → validate → fingerprint. No bar is ever manufactured; a gap is described, never filled.

| Symbol | Provider/feed | Adj | Range | Session bars | Ext dropped | Missing | Missing % | Valid | sha256 (16) |
|---|---|---|---|---|---|---|---|---|---|
| SPY | alpaca/iex | raw | 2021-01-04..2026-08-28 | 36751 | 5345 | 49 | 0.13% | yes | d409cd3b1bdf7847 |
| QQQ | alpaca/iex | raw | 2021-01-04..2026-08-28 | 36752 | 8070 | 48 | 0.13% | yes | c53d984e588955fa |

The expected-bar denominator is computed from each real session's own open and close, so a half day contributes 14 and a full day 26, and a holiday contributes nothing because it is not a session.

**Corporate actions.** Neither SPY nor QQQ split during the window: across 1,418 overnight close→open transitions per symbol, **zero** exceed ±15%, and the largest are real market events (2024-08-05 −4.02% SPY / −5.36% QQQ, the yen carry unwind; 2022-11-10 +3.70% / +4.89%, the CPI surprise; the April 2025 tariff sequence). Raw prices are therefore harmless for this pilot. They are **not** harmless for the ten-symbol universe — see §16.

**Two real provider outages were found**, identically in both symbols, which is what makes them the feed's and not the harness's:

| Date | Bars missing | Nature |
|---|---|---|
| 2025-03-10 | 26 (the whole session) | IEX published nothing for SPY or QQQ. Confirmed by re-fetching that single day in isolation. |
| 2024-12-23 | 22 (10:30 ET to the close) | IEX published only the first 4 regular-session bars. |

Total missing: 49 bars for SPY (0.13%), 48 for QQQ (0.13%). The pipeline detected both, reported them as two gap events rather than 48 separate holes, and fabricated nothing. Both fall inside the scoring window on purpose.

---

## 4. Session and calendar validation

Thirteen named days were inspected individually against the shipped session arithmetic. Every figure below is derived by `autotrader.equity.session`, not recomputed here.

| Day | Date | Session | Local hours | UTC off | Scheduled bars | Observed | Actionable |
|---|---|---|---|---|---|---|---|
| ordinary winter session | 2025-01-15 | yes | 09:30-16:00 | -5 | 26 | 26 | 25 |
| ordinary summer session | 2025-07-15 | yes | 09:30-16:00 | -4 | 26 | 26 | 25 |
| DST spring-forward, first session after | 2025-03-10 | yes | 09:30-16:00 | -4 | 26 | 0 | 25 |
| DST fall-back, first session after | 2025-11-03 | yes | 09:30-16:00 | -5 | 26 | 26 | 25 |
| session before Thanksgiving | 2024-11-27 | yes | 09:30-16:00 | -5 | 26 | 26 | 25 |
| Thanksgiving early close | 2024-11-29 | yes | 09:30-13:00 | -5 | 14 | 14 | 13 |
| Christmas Eve early close | 2024-12-24 | yes | 09:30-13:00 | -5 | 14 | 14 | 13 |
| day before Independence Day early close | 2025-07-03 | yes | 09:30-13:00 | -4 | 14 | 14 | 13 |
| session before Good Friday | 2025-04-17 | yes | 09:30-16:00 | -4 | 26 | 26 | 25 |
| Good Friday: market closed | 2025-04-18 | no | - | - | 0 | 0 | 0 |
| Christmas Day: market closed | 2024-12-25 | no | - | - | 0 | 0 | 0 |
| Juneteenth: market closed | 2025-06-19 | no | - | - | 0 | 0 | 0 |
| Saturday: market closed | 2025-01-18 | no | - | - | 0 | 0 | 0 |

Invariants: closed_days_have_no_bars = PASS, full_sessions_schedule_26_bars = PASS, early_closes_schedule_14_bars = PASS, open_is_always_09_30_local = PASS, last_bar_of_session_is_never_actionable = PASS, both_utc_offsets_observed = PASS

---

## 5. Daylight-saving validation

The exchange keeps a constant 09:30–16:00 **local** session across both transitions; what moves is the UTC window. Both offsets are present in the data and both were exercised.

| | EST (winter) | EDT (summer) |
|---|---|---|
| UTC offset | −5 | −4 |
| Session in UTC | 14:30–21:00 | 13:30–20:00 |
| First bar start | 14:30 | 13:30 |
| Last bar start | 20:45 | 19:45 |
| Regular-session bars | 26 | 26 |
| Complete 1h buckets | 6 | 6 |
| Complete 4h buckets | 1 | 1 |

The derived-bar counts are **invariant across the transition**, which is the property that matters: V3 receives the same amount of context on either side of a clock change. Three of the six scoring windows contain a transition (both directions), verified by observing both UTC offsets inside the window.

The transition days themselves are not special-cased anywhere, because they do not need to be: the calendar reports the session in naive Eastern wall-clock, `session_from_local` attaches `MARKET_TIMEZONE` and converts to UTC once, and every comparison downstream is on instants.

---

## 6. Holiday validation

Holidays are absent from the broker's calendar rather than listed anywhere in the code. Verified: Good Friday 2025-04-18, Christmas Day 2024-12-25, Juneteenth 2025-06-19, and an ordinary Saturday all report no session and contribute zero bars. The snapshot contains 1,759 sessions over seven calendar years — consistent with ~251 sessions a year.

---

## 7. Early-close validation

The calendar reports **14 early closes** in the snapshot window, all at 13:00 local: the day after Thanksgiving, Christmas Eve when it falls on a weekday, and 3 July when it precedes a weekday Independence Day. Four fall inside the scoring window (2021-11-26, 2023-07-03, 2024-11-29, 2024-12-24).

A 13:00 close schedules **14** regular-session bars (09:30 → 12:45), not 26. Confirmed for every early close in the dataset.

**The consequence that matters, and it is not small:** an early-close session yields **3** complete 1-hour buckets and **zero** complete 4-hour buckets. A 4-hour UTC bucket needs 16 consecutive in-session bars and a 3.5-hour session offers no such run. This is measured, not argued — see §9 — and it is the reason the declared warm-up constant understates the real requirement (§10).

---

## 8. Overnight-gap semantics

Three separate mechanisms, each verified:

**No bar is invented between sessions.** Friday's 15:45 bar and Monday's 09:30 bar are adjacent rows in the frame; nothing fills the 62.5 hours between them. The frame contains exactly the bars the provider published for scheduled sessions.

**No derived bar spans the gap.** The aggregator buckets on the UTC clock and keeps a bucket only when it holds its full complement of base bars. A bucket straddling the close cannot fill from regular-session bars alone, so it is discarded. Measured across every derived bar of both symbols: zero violations (§9).

**A fill at the session boundary crosses the gap honestly.** The simulator fills a proposal at the *following* bar's open. For a proposal on a session's last bar, the following bar is the next session's opening print — so the fill happens at next morning's open, at whatever price the market reopened at. That is what a regular-hours strategy actually gets; it cannot fill after 16:00. A test constructs a 10-point overnight gap and asserts the fill takes it. `overnight_fills` counts how often this happened per engine per window.

**Overnight gap risk dominates the cost model.** Mean absolute overnight gap: **0.444%** (SPY), **0.602%** (QQQ). The realistic equity cost model charges 2 bp of slippage per side — **0.020%**. Gap risk is therefore roughly 22× (SPY) to 30× (QQQ) the modelled transaction cost. Any conclusion about equity cost sensitivity is dominated by whether the strategy holds overnight, not by the fee assumption.

---


## 9. 15m → 1h → 4h aggregation correctness

V3 reads three timeframes and fetches one. The other two are derived by `autotrader.decision.timeframes.aggregate_bars`, which buckets on the **UTC epoch** (never on the supplied window) and keeps a bucket only when it holds *exactly* `interval / base_interval` constituent bars. That rule was written for continuous crypto. The pilot tested whether it survives a market that is shut 17.5 hours a day.

Four ways a derived bar could be illegal, each checked over **every** derived bar of both symbols — not a sample:

| Check | SPY 1h | SPY 4h | QQQ 1h | QQQ 4h |
|---|---|---|---|---|
| Derived bars produced | 8,477 | 1,407 | 8,478 | 1,408 |
| Bars spanning a session boundary | **0** | **0** | **0** | **0** |
| Bars with the wrong constituent count | **0** | **0** | **0** | **0** |
| `usable_history` causality violations | **0** | **0** | **0** | **0** |

**Measured yield, split by session length** — the number the policy constants are supposed to describe. Counted over 1,409 full sessions and 10 early closes per symbol:

| Session length | 1h buckets completed (mean) | 4h buckets completed (mean) |
|---|---|---|
| 26 bars (full session) | 5.995 | 0.999 |
| 14 bars (early close) | **3.000** (exact, every session) | **0.000** (exact, every session) |

The full-session means fall a hair below the nominal 6 and 1 for a mechanical reason, not a semantic one: the two provider outages (§3) left two sessions unable to complete their buckets. Every session with all 26 bars present yielded exactly 6 and exactly 1.

This is the pilot's most consequential aggregation finding. The `EQUITY_BASE_BARS_PER_COMPLETE_BAR` constants (`{"15m": 1, "1h": 5, "4h": 26}`) describe the *full-session average*. The `1h: 5` figure is conservative and safe on both session lengths (a full session costs 26/6 = 4.33 base bars per 1h bar; an early close costs 14/3 = 4.67). The `4h: 26` figure is **not** conservative: an early close yields no 4-hour bar at all, so its true marginal cost is unbounded, and any window containing early closes needs more than 26 base bars per 4-hour bar.

**Why the session filter is load-bearing, not cosmetic.** The completeness rule is what keeps derived equity bars honest, and it only works while extended-hours bars are absent. The IEX feed serves pre-market and post-market candles in the same response. A test demonstrates the failure directly: with two pre-market candles left in, the 14:00 UTC 1-hour bucket *fills* — from two pre-market bars and two regular-session bars — and the aggregator, which counts rather than consults a calendar, emits a candle straddling the opening bell. `session_bar_mask` is applied before aggregation for exactly this reason. 5,345 extended-hours rows were dropped for SPY and 8,070 for QQQ.

**Alignment.** `usable_history` admits a derived bar only when `bucket_start + interval <= base_bar_start + base_interval`. Sampled across the whole frame for all three timeframes: zero violations. A 4-hour bar starting at 16:00 UTC is not usable on the 19:30 base bar, because it does not close until 20:00 and the base bar closes at 19:45.

---

## 10. Actual warm-up requirements

The declared constant was not trusted. `EQUITY_POLICY.required_base_bars(("15m","1h","4h"))` returns **2,834** (109 required 4-hour bars × 26 base bars each). Measured against the real frames:

| Measure | SPY | QQQ |
|---|---|---|
| Declared `required_base_bars` | 2,834 | 2,834 |
| Bars needed from frame start to reach 109 complete 4h bars | 2,859 | 2,834 |
| **Worst-case sliding-window lookback over the whole frame** | **2,885** | **2,885** |
| Worst case occurred at | 2025-04-21 | 2025-04-21 |
| 1h worst-case lookback (109 bars) | 483 | 483 |

**The declared constant understates the true equity requirement by 51 bars (~1.8%).** Two independent causes, both measured:

- An early-close session contributes 14 base bars and **zero** 4-hour bars, so it consumes lookback without producing context.
- A single missing 15-minute bar destroys the one 4-hour bucket its session had. SPY needs 2,859 bars from the frame start where QQQ needs 2,834 — a 25-bar difference caused by exactly one extra missing bar (2021-04-13) early in SPY's history, which cost a whole extra session.

The worst case at 2025-04-21 follows the 2025-03-10 outage and the surrounding early closes.

This is the same class of finding as the known crypto discrepancy (V3 declares 1,744 and still answers `INSUFFICIENT_HISTORY_4H` at 1,744). It is a **lower bound that assumes a perfect, gapless, all-full-session history**, and equities never have one.

**The pilot therefore uses a lookback of 3,000 base bars** — 4.0% above the measured worst case — and does not merely assert that this is enough. Every engine on every scored bar is checked for an `INSUFFICIENT_*` reason token, and the count is reported (§13). A study that quietly returned a flat equity curve because every engine declined for want of history would be indistinguishable from one reporting a strategy that never traded.

---

## 11. V4 temporal methodology

One model per scoring window, fitted only on that window's past. No model ever sees a bar from the window it scores.

| Property | Value |
|---|---|
| Scheme | Anchored walk-forward (train on everything up to the boundary) |
| Label horizon | 4 bars (`DEFAULT_HORIZON_BARS`) |
| Outer embargo | 26 bars = **one whole regular session** |
| Train→score gap | 30 bars (horizon + embargo) |
| Inner purge/embargo | applied per fold by `autotrader.ml.v4` |
| Candidate grading | `compare_candidates`, 4 anchored sub-folds, embargo 26 |
| Selection rule | `select_candidate` — must beat the class-frequency baseline by ≥ 0.002 mean log loss |
| Standardizer | refitted inside every fold, from that fold's training rows alone |
| Calibration | isotonic, fitted on validation rows using the estimator's uncalibrated scores |
| Seed | 0, fixed |
| Shuffling | none anywhere |

**The embargo is market time, not a row count.** 26 bars is one equity trading day. An embargo shorter than a session would let a model be fitted on the morning of a day it is about to be graded on.

**Equity-specific: labels may cross a session gap, and this is the shipped default.** `SessionPolicy.SPAN_SESSIONS` lets a 4-bar holding period beginning near the close resolve in the next session, and flags every such row with `label_spans_session_gap`. Roughly **19%** of training rows carry that flag. This is the honest choice for a regular-hours strategy: it holds positions overnight because it cannot trade them out after 16:00, so an overnight outcome is a real outcome. The alternative (`WITHIN_SESSION`) would discard the last four bars of every session and teach the model that end-of-day signals have no consequences.

The grid the labels are computed on is an `equity_grid` built from the broker's sessions, so `spans_session_gap` is true exactly when two bars belong to different sessions, and a missing bar invalidates an interval rather than being stepped over.

**Verification.** `assert_no_forward_information` re-checks every model by comparing *stored instants* rather than the row arithmetic that produced them — an off-by-one in the training code would surface there instead of being reproduced.

**What the models actually selected** is reported in §13.2. The honest headline: in **11 of the 12** window-models, no candidate beat its class-frequency baseline by the material 0.002 log-loss margin, so the baseline was selected — a constant probability, below the equity policy's 0.40 confidence floor, and therefore HOLD on every bar. The single exception (QQQ / `2023-summer`, logistic) emitted two signals whose confidence turns out to be a calibration artifact (§16.5).

That is a finding about the market, the feature set and the 4-bar horizon — not a failure of the pipeline. The comparison is recorded whether or not it selects a model, because "no candidate beat its null" is the result on a market that offered no edge, and a study that only recorded winners could not report it.

---

## 12. V5 causal methodology

V5 is an ensemble of a V3 and a V4 built under the same asset-class policy (the constructor refuses a mismatch, and refuses an uncalibrated model when the ensemble spec requires calibration).

- V5 consumes **only** the V4 artifact trained for its own window, under the same 30-bar train→score gap. No V5 decision in any window was informed by a model that had seen that window.
- Both sub-engines are driven over the **identical frame**, and `assess` asserts their timestamps match — an ensemble of two different bars is refused rather than blended.
- V5 is scored through the same `LiveDecisionEngine` sliding window as every other engine, so its inputs are causally bounded by construction, and it is included in the perturbation audit (§13) on equal terms.
- The shipped V5 policy is unchanged. No threshold, weight or band was tuned for SPY or QQQ.

**A verified compute consequence.** `EnsembleAssessment.deterministic` carries V3's complete `DecisionResult` and `.probabilistic` carries V4's complete `ProbabilityAssessment`. Checked on real bars: V5's internal V3 reading is **identical** (same signal, same score to 1e-12) to a standalone V3 decision on the same window. The full study can therefore obtain V3, V4 and V5 from one V5 pass instead of three separate passes — see §17.

---


## 13. Scoring integrity, causality and reproducibility


### 13.1 Common scoring window

| Window | Dates | Sessions | Bars | DST | Early closes | No data |
|---|---|---|---|---|---|---|
| 2021-autumn | 2021-10-15..2021-12-15 | 43 | 1106 | yes | 2021-11-26 | - |
| 2022-spring | 2022-02-15..2022-04-18 | 43 | 1118 | yes | - | - |
| 2023-summer | 2023-06-01..2023-07-31 | 41 | 1054 | - | 2023-07-03 | - |
| 2024-yearend | 2024-11-15..2025-01-15 | 40 | 994 | - | 2024-11-29, 2024-12-24 | - |
| 2025-spring | 2025-02-15..2025-04-21 | 44 | 1118 | yes | - | 2025-03-10 |
| 2026-summer | 2026-06-01..2026-08-28 | 63 | 1638 | - | - | - |

### 13.2 V4 walk-forward plan (one model per window)

| Symbol | Window | Train rows | Last train bar | First scored bar | Gap | Overnight labels | Selected | Log loss |
|---|---|---|---|---|---|---|---|---|
| SPY | 2021-autumn | 4960 | 2021-10-13T18:45 | 2021-10-15T13:30 | 30 | 950 | class_frequency | 0.691 |
| SPY | 2022-spring | 7132 | 2022-02-11T19:45 | 2022-02-15T14:30 | 30 | 1370 | class_frequency | 0.693 |
| SPY | 2023-summer | 15544 | 2023-05-30T18:45 | 2023-06-01T13:30 | 30 | 2990 | class_frequency | 0.693 |
| SPY | 2024-yearend | 25076 | 2024-11-13T19:45 | 2024-11-15T14:30 | 30 | 4830 | class_frequency | 0.690 |
| SPY | 2025-spring | 26567 | 2025-02-13T19:45 | 2025-02-18T14:30 | 30 | 5120 | class_frequency | 0.690 |
| SPY | 2026-summer | 34828 | 2026-05-28T18:45 | 2026-06-01T13:30 | 30 | 6718 | class_frequency | 0.690 |
| QQQ | 2021-autumn | 5010 | 2021-10-13T18:45 | 2021-10-15T13:30 | 30 | 960 | class_frequency | 0.689 |
| QQQ | 2022-spring | 7182 | 2022-02-11T19:45 | 2022-02-15T14:30 | 30 | 1380 | class_frequency | 0.692 |
| QQQ | 2023-summer | 15594 | 2023-05-30T18:45 | 2023-06-01T13:30 | 30 | 3000 | logistic | 0.691 |
| QQQ | 2024-yearend | 25126 | 2024-11-13T19:45 | 2024-11-15T14:30 | 30 | 4840 | class_frequency | 0.691 |
| QQQ | 2025-spring | 26617 | 2025-02-13T19:45 | 2025-02-18T14:30 | 30 | 5130 | class_frequency | 0.691 |
| QQQ | 2026-summer | 34878 | 2026-05-28T18:45 | 2026-06-01T13:30 | 30 | 6728 | class_frequency | 0.691 |

### 13.3 Scoring integrity

| Symbol | Window | Engine | Decisions | Insufficient history | Series mismatches | Overnight fills |
|---|---|---|---|---|---|---|
| SPY | 2021-autumn | V1 | 1106 | 0 | 0 | 2 |
| SPY | 2021-autumn | V2 | 1106 | 0 | 0 | 26 |
| SPY | 2021-autumn | V3 | 1106 | 0 | 0 | 21 |
| SPY | 2021-autumn | V4 | 1106 | 0 | 0 | 0 |
| SPY | 2021-autumn | V5 | 1106 | 0 | 0 | 7 |
| SPY | 2022-spring | V1 | 1118 | 0 | 0 | 0 |
| SPY | 2022-spring | V2 | 1118 | 0 | 0 | 28 |
| SPY | 2022-spring | V3 | 1118 | 0 | 0 | 14 |
| SPY | 2022-spring | V4 | 1118 | 0 | 0 | 0 |
| SPY | 2022-spring | V5 | 1118 | 0 | 0 | 0 |
| SPY | 2023-summer | V1 | 1054 | 0 | 0 | 0 |
| SPY | 2023-summer | V2 | 1054 | 0 | 0 | 19 |
| SPY | 2023-summer | V3 | 1054 | 0 | 0 | 13 |
| SPY | 2023-summer | V4 | 1054 | 0 | 0 | 0 |
| SPY | 2023-summer | V5 | 1054 | 0 | 0 | 4 |
| SPY | 2024-yearend | V1 | 994 | 0 | 0 | 0 |
| SPY | 2024-yearend | V2 | 994 | 0 | 0 | 17 |
| SPY | 2024-yearend | V3 | 994 | 0 | 0 | 10 |
| SPY | 2024-yearend | V4 | 994 | 0 | 0 | 0 |
| SPY | 2024-yearend | V5 | 994 | 0 | 0 | 1 |
| SPY | 2025-spring | V1 | 1118 | 0 | 0 | 0 |
| SPY | 2025-spring | V2 | 1118 | 0 | 0 | 23 |
| SPY | 2025-spring | V3 | 1118 | 0 | 0 | 14 |
| SPY | 2025-spring | V4 | 1118 | 0 | 0 | 0 |
| SPY | 2025-spring | V5 | 1118 | 0 | 0 | 0 |
| SPY | 2026-summer | V1 | 1638 | 0 | 0 | 0 |
| SPY | 2026-summer | V2 | 1638 | 0 | 0 | 35 |
| SPY | 2026-summer | V3 | 1638 | 0 | 0 | 23 |
| SPY | 2026-summer | V4 | 1638 | 0 | 0 | 0 |
| SPY | 2026-summer | V5 | 1638 | 0 | 0 | 0 |
| QQQ | 2021-autumn | V1 | 1106 | 0 | 0 | 1 |
| QQQ | 2021-autumn | V2 | 1106 | 0 | 0 | 24 |
| QQQ | 2021-autumn | V3 | 1106 | 0 | 0 | 19 |
| QQQ | 2021-autumn | V4 | 1106 | 0 | 0 | 0 |
| QQQ | 2021-autumn | V5 | 1106 | 0 | 0 | 8 |
| QQQ | 2022-spring | V1 | 1118 | 0 | 0 | 1 |
| QQQ | 2022-spring | V2 | 1118 | 0 | 0 | 30 |
| QQQ | 2022-spring | V3 | 1118 | 0 | 0 | 13 |
| QQQ | 2022-spring | V4 | 1118 | 0 | 0 | 0 |
| QQQ | 2022-spring | V5 | 1118 | 0 | 0 | 0 |
| QQQ | 2023-summer | V1 | 1054 | 0 | 0 | 0 |
| QQQ | 2023-summer | V2 | 1054 | 0 | 0 | 22 |
| QQQ | 2023-summer | V3 | 1054 | 0 | 0 | 15 |
| QQQ | 2023-summer | V4 | 1054 | 0 | 0 | 0 |
| QQQ | 2023-summer | V5 | 1054 | 0 | 0 | 6 |
| QQQ | 2024-yearend | V1 | 994 | 0 | 0 | 0 |
| QQQ | 2024-yearend | V2 | 994 | 0 | 0 | 17 |
| QQQ | 2024-yearend | V3 | 994 | 0 | 0 | 13 |
| QQQ | 2024-yearend | V4 | 994 | 0 | 0 | 0 |
| QQQ | 2024-yearend | V5 | 994 | 0 | 0 | 5 |
| QQQ | 2025-spring | V1 | 1118 | 0 | 0 | 1 |
| QQQ | 2025-spring | V2 | 1118 | 0 | 0 | 23 |
| QQQ | 2025-spring | V3 | 1118 | 0 | 0 | 13 |
| QQQ | 2025-spring | V4 | 1118 | 0 | 0 | 0 |
| QQQ | 2025-spring | V5 | 1118 | 0 | 0 | 0 |
| QQQ | 2026-summer | V1 | 1638 | 0 | 0 | 2 |
| QQQ | 2026-summer | V2 | 1638 | 0 | 0 | 28 |
| QQQ | 2026-summer | V3 | 1638 | 0 | 0 | 21 |
| QQQ | 2026-summer | V4 | 1638 | 0 | 0 | 0 |
| QQQ | 2026-summer | V5 | 1638 | 0 | 0 | 0 |

### 13.4 Causality audit

| Symbol | Engine | Audit-ready | Probes | Scored bars | Changed decisions | Vacuous probes | Verdict |
|---|---|---|---|---|---|---|---|
| SPY | V1 | yes | 5 | 25 | 0 | 0 | PASS |
| SPY | V2 | yes | 5 | 25 | 0 | 0 | PASS |
| SPY | V3 | yes | 5 | 25 | 0 | 0 | PASS |
| SPY | V4 | yes | 5 | 25 | 0 | 0 | PASS |
| SPY | V5 | yes | 5 | 25 | 0 | 0 | PASS |
| QQQ | V1 | yes | 5 | 25 | 0 | 0 | PASS |
| QQQ | V2 | yes | 5 | 25 | 0 | 0 | PASS |
| QQQ | V3 | yes | 5 | 25 | 0 | 0 | PASS |
| QQQ | V4 | yes | 5 | 25 | 0 | 0 | PASS |
| QQQ | V5 | yes | 5 | 25 | 0 | 0 | PASS |

### 13.5 Reproducibility

| Symbol | Check | Result |
|---|---|---|
| SPY | dataset_digest | PASS |
| SPY | training_determinism | PASS |
| SPY | scoring_determinism_V1 | PASS |
| SPY | scoring_determinism_V2 | PASS |
| SPY | scoring_determinism_V3 | PASS |
| SPY | scoring_determinism_V4 | PASS |
| SPY | scoring_determinism_V5 | PASS |
| QQQ | dataset_digest | PASS |
| QQQ | training_determinism | PASS |
| QQQ | scoring_determinism_V1 | PASS |
| QQQ | scoring_determinism_V2 | PASS |
| QQQ | scoring_determinism_V3 | PASS |
| QQQ | scoring_determinism_V4 | PASS |
| QQQ | scoring_determinism_V5 | PASS |


---


## 14. Pilot metrics — diagnostics only


These are **diagnostics, not results.** Six non-contiguous windows on two ~0.9-correlated index ETFs cannot rank an engine, and the compounded figure is what holding each engine through all six windows would have produced — not a track record. Read them to check that the machinery produces sane, cost-sensitive numbers, which is what the pilot was for.

Starting capital is $100,000 per window, identical across every engine, symbol and cost model, so the curves are directly comparable. The three cost models replay the **same stored decision series**, so they differ by the cost assumption and nothing else.

**What the numbers actually show:**

*Cost sensitivity is the dominant effect, and it is correctly ordered.* Every engine's return falls monotonically from `frictionless` → `equity-marketable` → `stress`, and cost drag rises monotonically. SPY V1 goes +0.60% → −1.59% → −53.42%. That is not a modelling artifact: V1 takes 53 round trips, and the stress model charges ~1.4% per round trip, so ~74% of drag is arithmetic. The realistic equity model charges 4 bp per round trip and costs V1 about 2.2%.

*Turnover is what kills V1 and V2 at realistic costs.* Both go from positive under zero cost to roughly flat or negative under the equity model. V3, which trades far less (11–14 round trips against V1's 53), carries about a fifth of the cost drag.

*The two symbols disagree, which is the point.* V3 returns **+5.31%** on SPY and **−10.43%** on QQQ under the same cost model over the same windows. Two highly correlated index ETFs, opposite signs. Any ranking of engines drawn from this pilot would be noise, and this is the cleanest available demonstration of why the ten-symbol study exists.

*V4's apparent win is an artifact.* QQQ V4 shows +5.10% with 12.8% exposure from **two signals** — the same two bars whose 0.9997 confidence came from a thin isotonic calibration bin (§16.5). It is the best V4 number in the pilot and it is luck on a suspicious signal.

*V5 held rather than traded.* V5 shows 0 completed round trips with 65–75% exposure: it entered positions inside windows and was still holding at each window's end, so its P&L is unrealized and marked to the last close. Its negative compounded figures reflect that, and its near-zero cost drag (0.100%) reflects having traded very little.

*Trade counts are too small for the trade-level statistics to mean anything.* Win rate and profit factor over 11–55 completed round trips across six short windows are reported for completeness and should not be interpreted.


### 14.1 Zero-cost diagnostic — an upper bound, not a result
| Symbol | Engine | Compounded return | Worst window MaxDD | Signals | Round trips | Mean exposure | Cost drag |
|---|---|---|---|---|---|---|---|
| SPY | V1 | 0.60% | -12.63% | 113 | 53 | 49.55% | 0.00% |
| SPY | V2 | 4.73% | -12.65% | 3843 | 55 | 54.82% | 0.00% |
| SPY | V3 | 5.82% | -4.48% | 2502 | 11 | 47.93% | 0.00% |
| SPY | V4 | 0.00% | 0.00% | 0 | 0 | 0.00% | 0.00% |
| SPY | V5 | -7.40% | -21.06% | 400 | 0 | 65.13% | 0.00% |
| QQQ | V1 | 2.36% | -12.42% | 114 | 53 | 46.85% | 0.00% |
| QQQ | V2 | 2.75% | -12.11% | 3747 | 51 | 52.35% | 0.00% |
| QQQ | V3 | -9.91% | -11.54% | 2429 | 14 | 44.87% | 0.00% |
| QQQ | V4 | 5.12% | -3.68% | 2 | 0 | 12.78% | 0.00% |
| QQQ | V5 | -12.12% | -25.22% | 468 | 0 | 75.21% | 0.00% |

### 14.2 Realistic equity cost model (`equity-marketable`: 0 bp fee, 2 bp slippage)
| Symbol | Engine | Compounded return | Worst window MaxDD | Signals | Round trips | Mean exposure | Cost drag |
|---|---|---|---|---|---|---|---|
| SPY | V1 | -1.59% | -12.89% | 113 | 53 | 49.55% | 2.20% |
| SPY | V2 | 2.37% | -12.93% | 3843 | 55 | 54.82% | 2.29% |
| SPY | V3 | 5.31% | -4.53% | 2502 | 11 | 47.93% | 0.48% |
| SPY | V4 | 0.00% | 0.00% | 0 | 0 | 0.00% | 0.00% |
| SPY | V5 | -7.50% | -21.08% | 400 | 0 | 65.13% | 0.10% |
| QQQ | V1 | 0.13% | -12.66% | 114 | 53 | 46.85% | 2.21% |
| QQQ | V2 | 0.59% | -12.36% | 3747 | 51 | 52.35% | 2.11% |
| QQQ | V3 | -10.43% | -11.68% | 2429 | 14 | 44.87% | 0.58% |
| QQQ | V4 | 5.10% | -3.68% | 2 | 0 | 12.78% | 0.02% |
| QQQ | V5 | -12.21% | -25.22% | 468 | 0 | 75.21% | 0.10% |

### 14.3 Stress cost model (`stress`: 50 bp fee, 20 bp slippage — a bound, not a scenario)
| Symbol | Engine | Compounded return | Worst window MaxDD | Signals | Round trips | Mean exposure | Cost drag |
|---|---|---|---|---|---|---|---|
| SPY | V1 | -53.42% | -21.34% | 113 | 53 | 49.55% | 72.38% |
| SPY | V2 | -52.84% | -22.12% | 3843 | 55 | 54.82% | 74.85% |
| SPY | V3 | -10.54% | -8.06% | 2502 | 11 | 47.93% | 16.49% |
| SPY | V4 | 0.00% | 0.00% | 0 | 0 | 0.00% | 0.00% |
| SPY | V5 | -10.58% | -21.61% | 400 | 0 | 65.13% | 3.48% |
| QQQ | V1 | -52.61% | -21.13% | 114 | 53 | 46.85% | 72.36% |
| QQQ | V2 | -51.07% | -21.60% | 3747 | 51 | 52.35% | 69.47% |
| QQQ | V3 | -26.46% | -16.78% | 2429 | 14 | 44.87% | 19.72% |
| QQQ | V4 | 4.39% | -3.68% | 2 | 0 | 12.78% | 0.70% |
| QQQ | V5 | -15.14% | -25.50% | 468 | 0 | 75.21% | 3.48% |


---


## 15. Limitations

Stated plainly, because several of them bound what the metrics above are allowed to mean.

1. **Two symbols cannot rank an engine.** SPY and QQQ are both large-cap US index ETFs with ~0.9 correlation. They are one bet, not two. No statement about which engine is best for equities can be made from this pilot, and none is made.

2. **V4 selected the null model in 11 of 12 windows.** No candidate beat the class-frequency baseline by the required 0.002 mean log loss, so V4 emitted a constant probability on every bar, fell below the equity policy's 0.40 confidence floor, and returned HOLD everywhere. The one exception (QQQ / `2023-summer`, logistic) emitted exactly **2 signals in 1,054 bars**, both from the extreme bin of a coarse isotonic calibration (§16.5). The V4 *pipeline* is therefore fully validated — training, temporal splits, purge, embargo, calibration, artifact provenance, causal consumption — but V4's *predictive* behaviour is essentially untested, because on 11 of 12 windows there was nothing to predict with. Note that V4's headline pilot return (+5.12% on QQQ) comes entirely from those two artifact bars: it is luck on a suspicious signal, not evidence of edge.

3. **Consequently, V5's evidence is partial.** On 11 of 12 windows V4 was constant, so V5 reduced to V3 blended against a fixed number. The ensemble mechanics, the policy agreement check, the band logic and the causal consumption of the artifact are all exercised; the behaviour of a V5 whose probabilistic half is *informative* is only observed on two bars.

4. **The scored interval is six windows, not the full history.** 7,028 bars per symbol out of ~33,750 available. The windows were chosen for calendar coverage, so they over-represent awkward days by design. They are not a return series and the compounded figures are not a track record — the windows are not contiguous.

5. **Prices are split-adjusted but not dividend-adjusted.** For SPY/QQQ this is provably immaterial — neither split, and the split-adjusted frames are byte-identical to the raw ones (§16.1). The residual dividend steps (~0.3% quarterly for SPY) are left in, visible, and not corrected for; a strategy holding across an ex-dividend date sees a price drop it did not lose money on.

6. **IEX is a single-venue feed.** Volume is a subset of consolidated volume, so every volume-derived feature (the participation floor, `volatility_ratio`'s volume half, VWAP) is measured against partial data. This affects V2/V3/V4/V5 gating in a way that a SIP subscription would change. Not quantified here.

7. **Trade counts are low and unrealized P&L dominates.** Windows are ~40 sessions; positions opened inside a window and still open at its end are marked to the last close and reported as unrealized. Win rate and profit factor over so few completed round trips are not statistically meaningful.

8. **No slippage model beyond a flat 2 bp.** No market impact, no size-dependence, no spread widening at the open or on gap days. For SPY/QQQ at small notional 2 bp is conservative (real quoted spread is well under 1 bp); it would not be for a less liquid name.

9. **The `stress` cost model is a bound, not an equity scenario.** It charges 0.5% per side in fees — a crypto-taker figure, and roughly 250x a realistic US equity commission of zero. Its purpose is to answer "does anything survive being badly wrong about costs?", and the answer for V1 and V2 is no. It should not be read as a pessimistic equity case.

10. **The provider's history window moves.** IEX coverage begins ~6 years back on a rolling basis, so the exact dataset used here cannot be re-downloaded indefinitely. The stored Parquet and its `frame_sha256` are the durable artifact, not the request.

11. **No full regression suite was run.** 881 targeted tests across the equity, ML, decision, research and study suites passed. The full ~2,000-test suite was deliberately not run concurrently with the other active research task on this machine.

---

## 16. Issues found

### 16.1 Corporate-action adjustment was never chosen — FIXED (research-enabling, default unchanged)

**Classification: genuine production defect, unambiguous, fix is additive and behaviour-preserving.**

`build_bars_request` set no `adjustment` field, so the provider served `raw` — not as a decision, but because the field was absent. Measured on the shipped path, NVDA's 2024-06-10 ten-for-one split appears as a **−89.91%** step between two consecutive session closes (1208.42 → 121.93). That is not a return, and EMA, RSI, MACD, ATR, `return_z` and the V4 label horizon all read it as one. Requesting `Adjustment.SPLIT` over the identical window yields **+0.90%** — the move the market actually made.

- **Impact on this pilot: none.** SPY and QQQ did not split; verified across 1,418 overnight transitions per symbol, zero exceed ±15%.
- **Impact on the ten-symbol run: blocking.** NVDA (10:1, 2024-06-10), AMZN (20:1, 2022-06), GOOGL (20:1, 2022-07) and TSLA (3:1, 2022-08) all split inside the candidate window. Each would inject a fabricated one-bar crash of 66–95% into the sample.
- **Impact on the live runtime: real but narrow.** A ~200-bar lookback spans ~8 sessions, so a split corrupts the indicators for a few sessions a year per symbol.

**Fix applied, in two parts.**

*Production (`e1afec2`).* `adjustment` is now an explicit parameter on `build_bars_request`, `fetch_bars_for_symbols`, `fetch_bars` and `download_bars`, and the metadata sidecar records which adjustment produced a file. **`DEFAULT_ADJUSTMENT` is `None` and the default request is byte-identical to before** — pinned by a test — because raw prices are the ones an order fills at and reconciliation compares stored fills against what the broker reported. No runtime caller passes the new argument; equity remains masked and unactivated. Four tests added.

*Research (`1f9cbca`).* The study's own download path now defaults to `Adjustment.SPLIT`, so a harness extended to ten symbols is correct by construction rather than correct by having been pointed at two ETFs that never split.

**The change costs this pilot nothing, and that is verified rather than assumed.** The whole 2021-01..2026-08 window was re-downloaded split-adjusted and reduced through the identical session filter. The resulting frames are **byte-identical** to the stored raw ones:

| Symbol | Rows (raw) | Rows (split) | sha256 raw | sha256 split | Identical |
|---|---|---|---|---|---|
| SPY | 36,751 | 36,751 | `d409cd3b1bdf7847…` | `d409cd3b1bdf7847…` | **yes** |
| QQQ | 36,752 | 36,752 | `c53d984e588955fa…` | `c53d984e588955fa…` | **yes** |

Every number in this report is therefore the same under either adjustment, and the prerequisite for the ten-symbol run is satisfied in code rather than left as an instruction. Dividends are deliberately left unadjusted: `Adjustment.ALL` back-adjusts historical prices for distributions, moving them away from what could actually have been traded.

### 16.2 `required_base_bars` understates the equity warm-up — DOCUMENTED, not changed

**Classification: ambiguous intended semantics. Not silently changed.**

`EQUITY_POLICY.required_base_bars(("15m","1h","4h"))` returns 2,834; the measured worst case on real SPY and QQQ bars is **2,885** (§10). The constant is an average-case figure and is documented as an estimate ("Exact for a continuously traded market and an estimate for a session-traded one"), so it is arguably behaving as designed — but an integrator sizing a fetch window from it will come up short whenever early closes or missing bars fall inside the window, and the failure mode is a stream of `INSUFFICIENT_HISTORY_4H` HOLDs rather than an error.

No production change made: whether this constant should be a guaranteed bound or an average is a design decision, and the same discrepancy already exists on the crypto side (1,744 declared, still insufficient at 1,744). **Recommendation for the ten-symbol run: size the lookback empirically per symbol and assert zero `INSUFFICIENT_*` reasons on scored bars**, as this pilot does, rather than trusting the constant.

### 16.3 The shipped causality audit is vacuous at equity warm-up lengths — WORKED AROUND in the harness

**Classification: research-harness limitation of a production utility, not a production defect.**

`autotrader.research.leakage.audit_engine_causality` places its perturbation probes evenly across the whole frame via `probe_indices`. That is correct for an engine warming up in ~100 bars. With V3's 3,000-bar equity warm-up in a ~3,024-bar audit frame, **every probe lands inside the warm-up**, no signal exists at or before the cutoff, and the comparison reduces to `() == ()` — a pass that establishes nothing.

The study's `studies.equity_v1_v5.leakage` therefore (a) places probes strictly inside the scored region, (b) records how many decisions each probe actually covered and reports a probe covering none as an **audit failure**, and (c) compares whole `DecisionRecord`s — signal, score to 9 dp, confidence, regime and reasons — rather than signal sets, so a score that moved without crossing a threshold is still caught. The shipped audit is still run alongside, and its findings are reported separately.

No production change: the shipped function is not wrong for its designed use. If the ten-symbol study wants a single auditor, `probe_indices` should gain an optional floor.

### 16.4 Two IEX provider outages — DATA FACT, no code change

2025-03-10 (whole session absent) and 2024-12-23 (22 of 26 bars absent), identically for SPY and QQQ, confirmed by isolated re-fetch. The pipeline detected both, reported them as gap events, and fabricated nothing — which is the behaviour under test. Both were deliberately kept inside the scoring window.

---

### 16.5 Isotonic calibration can emit near-certain probabilities from a thin bin — REPORTED, measure it in the full run

**Classification: ambiguous intended semantics. Isotonic calibration behaving as isotonic calibration does. Not silently changed.**

Eleven of the twelve window models selected the class-frequency baseline. The one that did not — QQQ / `2023-summer`, `MODEL_LOGISTIC` — produced a calibrated probability taking only **11 distinct values across 1,054 bars**, spanning −0.0468 to **0.9997**. Two bars landed in the top bin and were scored at 99.97% confidence.

That is not a claim about those two bars; it is what isotonic regression does when a monotone step is fitted over a sparse extreme region. The calibration bin behind 0.9997 is supported by a small number of training samples, and the output is a step function, not a smooth probability.

**It propagates, and it lands exactly where it matters.** V5 blends V4's confidence, so those two bars became the window's **two highest-confidence V5 readings** — 0.8678 and 0.8245, the only 2 of 1,054 bars above 0.6:

| Bar (UTC) | V3 | V4 | V5 |
|---|---|---|---|
| 2023-06-14 16:15 | BUY, conf 0.8074 | BUY, conf **0.9997** | BUY, conf **0.8678** |
| 2023-06-14 16:30 | BUY, conf 0.6771 | BUY, conf **0.9997** | BUY, conf **0.8245** |

V3 independently agreed on both, so the ensemble was not driven by V4 alone — but V4 lifted V5 above every other bar in the window, and confidence is what a production risk layer would size against. Both bars additionally carry `LOW_PARTICIPATION` and mutually contradictory drivers (`EMA_SLOPE_Z_BULLISH` with `EMA_SPREAD_Z_BEARISH`).

No production change: capping or flooring a calibrated probability is a modelling decision with its own costs, and one window on one symbol is not the evidence to make it on. **For the ten-symbol run, record per model: the number of distinct calibrated levels, and the training-sample support behind the top and bottom bins.** If extreme bins are routinely thin, that is the finding — and it should be settled before any V4 or V5 output is allowed to size a position.

---


## 17. Plan for the full ten-symbol evaluation

### 17.1 Prerequisites — must be done before the run starts

1. **Download with `Adjustment.SPLIT`.** Already in place: the capability was added to the market-data boundary (`e1afec2`) and the study's download path now defaults to it (`1f9cbca`). Four of the ten symbols split inside the candidate window; on raw bars each would inject a fabricated 66–95% one-bar crash. Still verify per symbol that no overnight close→open transition exceeds ±15%, as this pilot did for SPY/QQQ.
2. **Re-snapshot the market calendar** for the exact study window, and store it beside the datasets.
3. **Size the lookback empirically per symbol.** Do not use `required_base_bars`. Compute the worst-case sliding-window requirement on each symbol's real frame (the pilot's method), take the maximum across the universe, add ≥ 4% margin. Expect ~2,900–3,000; single-name equities have more missing bars than SPY/QQQ, so the figure may be higher.
4. **Assert zero `INSUFFICIENT_*` reasons** on every scored bar, per engine per symbol, and fail the run if any appear.
5. **Re-run the dataset quality audit per symbol.** Single names on IEX will have materially more missing bars than SPY/QQQ's 0.13%. Set a threshold in advance (suggest: flag any symbol above 1.0% missing, and any single gap event over one session) and decide before seeing the results whether such a symbol stays in the universe.

### 17.2 Scoring plan

- **Universe:** the frozen ten (`EQUITY_SYMBOLS`). One process per symbol.
- **Common scoring window:** one interval, identical for all ten symbols and all five engines. Either the full post-warm-up history or an extended version of the pilot's calendar-stratified windows — but the same bars for every engine, as here.
- **Score V3, V4 and V5 from a single V5 pass.** Verified in this pilot: `EnsembleAssessment.deterministic` is bit-identical to a standalone V3 decision, and `.probabilistic` is V4's own assessment. Scoring them separately recomputes V3 and V4 twice each. This is a measured **42% saving on total scoring cost** and changes no result.
- **V4 walk-forward:** one model per window, anchored, 30-bar train→score gap (4-bar horizon + 26-bar session embargo), seed fixed, `compare_candidates` → `select_candidate` re-run per window. Record the comparison whether or not it selects a model.
- **Costs:** report `frictionless` (upper-bound diagnostic), `equity-marketable`, and `stress`, replayed from one stored decision series so they differ only by the cost model.
- **Causality:** run the study's probe-inside-the-scored-region audit for all 5 engines × all 10 symbols, and require zero vacuous probes.

### 17.3 Measured compute cost

Measured on this machine over all 12 window-symbol runs (70,280 scored bar-engine pairs), Python 3.11, single core, 3,000-bar lookback:

| Engine | ms/bar | Share of total |
|---|---|---|
| V1 | 23.9 | 5% |
| V2 | 44.4 | 9% |
| V3 | 160.0 | 33% |
| V4 | 44.1 | 9% |
| V5 | 218.0 | 44% |
| **All five, scored separately** | **490.4** | 100% |
| **V1 + V2 + V5 (V3/V4 reused from V5)** | **286.2** | 58% — a **42% saving** |

Non-scoring overhead was **197 s per symbol** for six V4 trainings, the aggregation audit, the coverage report, 60 replays and 60 live-series verifications — under 6% of the run.

Scoring ten symbols:

| Plan | Bars/symbol | CPU hours | 4 workers | 6 workers | 8 workers |
|---|---|---|---|---|---|
| Full history, engines scored separately | ~33,750 | 46.0 h | 11.5 h | 7.7 h | 5.8 h |
| **Full history, V3/V4 reused from V5** | ~33,750 | **26.8 h** | **6.7 h** | **4.5 h** | **3.4 h** |
| Calendar-stratified windows, reused | ~7,000 | 5.6 h | 1.4 h | 0.9 h | 0.7 h |

Doubling the walk-forward to 12 windows per symbol roughly doubles the overhead, to ~400 s/symbol ≈ **1.1 CPU-h** for the universe — immaterial beside the scoring cost.

**Total for the recommended plan (full history, V5 reuse, 12 walk-forward windows): ~28 CPU-hours ≈ 4.7 hours wall clock at 6 workers.**

### 17.4 Recommended worker count

**6 workers** on this 8-core machine for a dedicated run: it leaves two cores for the OS and an interactive session, and the workload is CPU-bound pure Python with no shared state between symbols, so it scales close to linearly. Use **4** if another research task is active — that is what kept this pilot to 2, and the pilot still finished two symbols in 61 minutes.

Practical notes carried forward from this run:
- One OS process per symbol, launched independently. A `multiprocessing.Pool` with the `spawn` context under `python -m` was not worth the debugging; independent processes give per-symbol logs and restartability for free.
- Give each run its own `PYTHONPYCACHEPREFIX` to avoid `__pycache__` races with a concurrent session.
- Checkpoint per window. A 5-hour run that has to restart from zero is a 10-hour run.
- Log during training, not only during scoring. This pilot's first progress line arrives only after all six models are fitted, which makes several minutes of real work look like a hang.

### 17.5 What the full run should be expected to show

V4 selected the class-frequency baseline in **11 of 12** window-models, and the one exception produced two signals whose confidence was a calibration artifact (§16.5). The concurrent crypto study reached the same conclusion on its own universe. The base case for the ten-symbol run is therefore that **V4 again finds no material edge over class frequency** on this feature set and this 4-bar horizon. If so, V5 will again reduce to V3-plus-a-constant, and the study's real finding will be about the V4 feature set and label definition rather than about which engine wins.

That is worth knowing before committing five hours of compute. Two cheaper experiments would answer it first:

1. **Sweep the V4 label horizon.** Four bars is one hour of market time — very short for a 15-minute equity bar, and short enough that the label may be mostly microstructure noise. This is the single most likely explanation for eleven null models, and it is testable on two or three symbols.
2. **Run a one-window, ten-symbol V4-only probe** — roughly 20 minutes of compute — and count how many symbols clear the materiality bar at all before paying for the full history.

Also worth pre-committing: **record the number of distinct calibrated probability levels and the training support behind the extreme bins** for every model that does select a real candidate (§16.5).

The pipeline is ready either way; this is about spending the compute well.

---

## 18. Final classification

### A. EQUITY HISTORICAL PIPELINE READY FOR 10-SYMBOL EVALUATION

The one defect that would have invalidated the ten-symbol run — unadjusted prices across splits — was found, fixed and tested inside this pilot, and the study's download path now requests split-adjusted bars by default. No outstanding blocker remains.

| Question | Answer |
|---|---|
| Is SPY/QQQ historical data trustworthy? | **Yes.** 36,751 / 36,800 scheduled SPY bars and 36,752 / 36,800 QQQ bars over 5y8m; 0.13% missing, entirely two identified provider outages; schema validation clean; no splits; fingerprinted and reproducible. The IEX single-venue volume caveat stands. |
| Are session semantics proven? | **Yes.** Regular-session filtering, both DST directions, 14 early closes, holidays, weekends, the half-open close, and the deliberately non-actionable last bar were each validated against the broker's own calendar through the shipped session functions. All six invariants pass. |
| Are V1–V5 causally valid? | **Yes.** All 10 engine×symbol audits pass: `all_causal=True`, **0** changed decisions under a 1.5x future-price perturbation, **0** vacuous probes (4–22 real decisions behind each), and **0** live-series mismatches across 70,280 scored decisions. The pilot had to strengthen the probe placement to make this claim non-vacuous at equity warm-up lengths (§16.3). |
| May the full ten-symbol run safely proceed? | **Yes.** The split-adjustment blocker is fixed in code; every other check passed. |
| Estimated compute for the full run | **~28 CPU-hours ≈ 4.7 h wall clock at 6 workers** (full history, V3/V4 reused from V5, 12 walk-forward windows). ~1.0 h if the calendar-stratified window approach is kept. Without the V5-reuse optimisation, ~47 CPU-hours. |
| Recommended worker count | **6** on a dedicated 8-core machine; **4** when sharing with another study. |

**EQUITY SPY/QQQ HISTORICAL PILOT COMPLETE — READY FOR FULL EVALUATION.**
