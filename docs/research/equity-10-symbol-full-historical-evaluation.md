# Equity 10-Symbol Full Historical Evaluation — Decision Engines V1–V5

**Status: research / validation only.** Nothing was deployed, activated, unmasked, or sent to the VPS. No order was placed, cancelled, replaced, or simulated against a broker. Equity remains masked. `main` and `integration/final-development-candidate` are untouched. The only network calls were read-only historical-bars `GET`s through the repository's own market-data path, identical in kind to the pilot's.

| | |
|---|---|
| Base branch | `integration/final-development-candidate` |
| Base SHA | `aee7a77af090fd9d3dd60f66c400fa2360f2f478` (verified against `origin` before the worktree was created) |
| Research branch | `research/equity-10-symbol-full-eval` |
| Worktree | `/Volumes/AUTOTRADER_QA/worktrees/equity-10-symbol-full` |
| Harness | `studies/equity_v1_v5/` (pilot's, cherry-picked verbatim) + `studies/equity_10_full/` (12 modules), `tests/test_study_equity_10_full.py` |
| Artifacts | `/Volumes/AUTOTRADER_QA/reports/equity-10-symbol-full/` |
| Datasets | `/Volumes/AUTOTRADER_QA/datasets/equity-historical/` |
| Universe | SPY, QQQ, IWM, AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA — the frozen production `EQUITY_SYMBOLS`, exactly |
| Scored | 12 chronological windows × 10 symbols × 5 engines = **318,726 decision instants, 1,593,630 stored decisions** |
| V4 configuration | FROZEN: horizon 4 bars, materiality gate 0.002, null-capable — the horizon study's §20, unchanged |
| Compute | 6 workers on 8 cores; heavy scoring 339.9 + 35.3 min wall; ~7.5 h wall end-to-end |

---

## 1. Executive conclusion

**No engine demonstrates a defensible post-cost edge over buy-and-hold on net return** — over 4.9 years and ten symbols, buy-and-hold returned **+167.1%** at the equal-sleeve portfolio level against V5's +137.2%, V2's +84.4%, V3's +74.7% and V1's +71.3%, all after realistic costs. **V4 selected its class-frequency null in 116 of 120 cells**, and each of the four exceptions is disqualified by its own out-of-sample record. **V5's advantage over V3 is exposure, not information.**

One finding survives every robustness test this study can throw at it: **V3 is the only engine with a genuinely defensive, better-than-buy-and-hold risk-adjusted profile.** Portfolio Sharpe 0.978 and Sortino 1.418 against buy-and-hold's 0.872/1.238; maximum drawdown **−12.9% against −45.3%** (shallower on 10 of 10 symbols); +16.8%/yr during broad-market-drawdown bars where V1 and V5 lose money; better-than-random timing (13.5%/yr at 12.7% vol on 41% exposure, where a random 41%-exposure slice of the market would give ~8%/yr at ~18% vol); positive net on 9 of 10 symbols; survives leave-one-symbol-out; survives stress costs that destroy V1/V2; and passed the pre-declared holdout criterion. It is a real risk profile, not a return edge: V3 captured 0.3 points of the holdout's 20.6-point rally.

Per the classification rule frozen in `development_conclusion.md` **before** the holdout was examined: **B — PROMISING EQUITY SIGNAL, BUT LIVE SHADOW REQUIRED**, the signal being V3's defensive risk-adjusted profile and nothing else.

## 2. Dataset provenance

All ten frames were built by the pilot's own pipeline: download → de-duplicate → regular-session filter by the broker's calendar → re-null undefined VWAP → validate → fingerprint. SPY and QQQ reuse the pilot's frames under pinned digests (proven byte-identical to split-adjusted re-downloads); the other eight were downloaded **split-adjusted** on 2026-08-30. Provider: the broker's stock-bars endpoint, IEX feed, `us_equity`, 15m bars, request dates in America/New_York, stored timestamps UTC.

| Symbol | Rows | Missing | Missing % | Gap events | Duplicates | Ext-hours dropped | OHLC/schema valid | Adjustment | sha256 (12) |
|---|---|---|---|---|---|---|---|---|---|
| SPY | 36,751 | 49 | 0.13% | 3 | 0 | 5,345 | yes | raw ≡ split (proven) | d409cd3b1bdf |
| QQQ | 36,752 | 48 | 0.13% | 2 | 0 | 8,070 | yes | raw ≡ split (proven) | c53d984e5889 |
| IWM | 36,748 | 52 | 0.14% | 5 | 0 | 3,679 | yes | split | dbe6f1f0130e |
| AAPL | 36,751 | 49 | 0.13% | 3 | 0 | 2,101 | yes | split | 075ad6764ec1 |
| MSFT | 36,751 | 49 | 0.13% | 3 | 0 | 1,044 | yes | split | 892f2405a712 |
| NVDA | 36,749 | 51 | 0.14% | 5 | 0 | 2,752 | yes | split | 6569c2a40c2d |
| AMZN | 36,702 | 98 | 0.27% | 44 | 0 | 798 | yes | split | 9a3e7eb83045 |
| GOOGL | 36,515 | 285 | 0.77% | 207 | 0 | 1,037 | yes | split | 491a9768b09d |
| META | 36,749 | 51 | 0.14% | 5 | 0 | 815 | yes | split | 935398292a73 |
| TSLA | 36,750 | 50 | 0.14% | 4 | 0 | 2,276 | yes | split | bdb27a8af9ac |

No bar was manufactured; no market-closed interval was interpolated. GOOGL's 285 missing bars (207 mostly single-bar gap events, concentrated in late 2021) are the worst in the universe and stay **under the pre-declared 1.0% exclusion threshold**, so the universe is complete. They are single-venue feed thinness, and they have a real consequence measured in §8. Full sidecars: `*.session.provenance.json`; `run_manifest.json` carries every digest.

## 3. Full symbol universe

Exactly the ten production symbols, in the shipped processing order. No symbol was added, and none was removed for looking bad (IWM, the worst performer for most engines, stayed).

## 4. Historical date range

Requested 2021-01-04..2026-08-28 (exchange days) for all ten symbols — the same range for everyone, so no newer symbol gets an easier interval. The common **scored** region is 2021-09-30..2026-08-28: 1,233 sessions, bounded below by the measured warm-up (§8). The IEX feed's history is a rolling ~6-year window, so this exact dataset cannot be re-downloaded indefinitely; the fingerprinted Parquet is the durable artifact.

## 5. Split-adjustment proof

Four universe symbols split inside the window. The audit measures every session-boundary close→open step and compares the step across each known split date against the raw-frame signature `-(1 − 1/ratio)`:

| Symbol | Split | Raw signature | Measured step | Unadjusted? |
|---|---|---|---|---|
| NVDA | 10:1, 2024-06-10 | −90.0% | **−0.41%** | no |
| AMZN | 20:1, 2022-06-06 | −95.0% | **+2.47%** | no |
| GOOGL | 20:1, 2022-07-18 | −95.0% | **+0.81%** | no |
| TSLA | 3:1, 2022-08-25 | −66.7% | **+1.80%** | no |

Every overnight step ≥15% was listed and checked: all six are real, dated market events (META −24.2% 2022-02-03, −24.5% 2022-10-27, +19.8% 2023-02-02, +16.5% 2024-02-02, +16.3% 2022-04-28; NVDA +26.2% 2023-05-25). A synthetic-crater test pins the audit's refusal behaviour. Prices are split-adjusted, not dividend-adjusted — dividend steps remain in the data, visible.

## 6. Session / calendar methodology

Regular session only, 09:30–16:00 America/New_York, judged bar-by-bar against the broker's own calendar snapshot (`market_calendar_2020-01-01_2026-12-31.json`, 1,759 sessions — the pilot's snapshot, reused under its recorded provenance because it spans the study window with margin). The pilot's thirteen named-day validations (holidays, both DST directions, three kinds of early close, the non-actionable last bar) all ran through the same shipped session functions this study uses; the scored region contains **10 early closes**, **9 windows with a DST transition**, and the 2025-03-10 full provider outage, all inside scoring on purpose. Weekends and holidays contribute no bars; Friday close → Monday open is one bar boundary, never continuous elapsed time; fills that cross it are counted (`overnight_fills`: 179 V1, 5,723 V2, 3,533 V3, 1 V4, 207 V5).

## 7. Common scoring interval

One interval for all ten symbols and all five engines: **2021-09-30..2026-08-28**, split into twelve contiguous chronological windows of 102–103 sessions (§9). Every engine scored the identical bars per symbol; per-symbol totals differ only by provider gaps (31,744–31,890 scored bars). Total: 318,726 decision instants.

## 8. Warm-up verification

The declared `required_base_bars` (2,834) was not trusted, and neither was the pilot's 3,000. The worst-case sliding-window lookback was measured on every real frame (the number of base bars needed to hold, in full, the constituents of the 109 most recent usable 4-hour buckets):

| SPY | QQQ | IWM | AAPL | MSFT | NVDA | AMZN | GOOGL | META | TSLA |
|---|---|---|---|---|---|---|---|---|---|
| 2,885 | 2,885 | 2,901 | 2,885 | 2,885 | 2,902 | **3,373** | **4,552** | 2,898 | 2,885 |

**The pilot's 3,000 is insufficient on this universe.** GOOGL's and AMZN's missing-bar clusters in late 2021 destroy consecutive 4-hour buckets; GOOGL's worst case (at 2022-01-28) needs 4,552 bars. Per the pre-declared rule the common lookback was raised **uniformly to 4,750** (4.3% above the measured worst case) and documented. Before scoring, a probe drove V1/V2/V3 live at 12 calendar positions per symbol across the scored region: **0 blocked bars**. During scoring, every one of the 1,593,630 decisions was checked: **0 `INSUFFICIENT_*`, 0 `FEATURE_UNAVAILABLE`** anywhere.

## 9. Walk-forward methodology

Twelve contiguous chronological windows (~5 months each), w01 2021-09-30..2022-02-25 through w12 2026-04-02..2026-08-28 (`run_manifest.json` → `split_definitions`). For V4, one model per symbol × window, anchored on all history up to the window boundary minus a 30-bar gap (4-bar horizon + 26-bar embargo = one full regular session), the shipped `compare_candidates` (4 anchored sub-folds, purge on `label_knowable_at`, embargo 26) and the shipped `select_candidate` with the **unweakened 0.002 gate**; seed 0, no shuffle, standardizers refitted per fold, isotonic calibration on validation rows only. Labels on the session-aware equity grid, `SPAN_SESSIONS`. Training/validation are strictly past; test (the scored window) strictly future; no random split, no future-fit normalization, no calibration on test rows. Stored-instant gap assertions passed on all 120 cells. V1/V2/V3/V5 use production semantics unchanged — no threshold tuning, no per-symbol parameters.

## 10. Final holdout methodology

w12 (2026-04-02..2026-08-28) was mechanically locked: the runner refuses `--stage holdout-score` until `holdout_unlock.json` exists, and that file carries the SHA-256 of `development_conclusion.md` — the frozen development verdict **and pre-declared holdout pass/fail criteria** — written before any w12 cell existed. Honesty note: w12 overlaps the pilot's published 2026-summer window for SPY/QQQ, so the holdout is virgin for eight symbols and for every portfolio-level figure, partially known for two. Holdout independence was **not** consumed: no rerun after seeing w12 occurred, and no parameter changed at any point.

## 11. Transaction-cost assumptions

Three models, replayed from the same stored decision series so they differ by cost alone: **frictionless** (0/0 — diagnostic upper bound), **equity-marketable** (0 commission, 2 bp adverse slippage per side — the realistic model all primary conclusions use), **stress** (50 bp fee + 20 bp slippage — a bound, not an equity scenario). No crypto cost assumption was applied to equities. Execution: a proposal on bar *t* fills at bar *t+1*'s open; final-bar proposals stay unexecuted; no same-bar or lookahead fill exists in the machinery (§21).

## 12–19. Engine methodology and results

All results: equal-sleeve portfolio over w01–w12, realistic cost, unless stated.

**12. V1 (EMA cross).** Net +71.3% (gross +91.2%), Sharpe 0.755, maxDD −25.2%, 2,800 round trips, turnover 705×, cost drag 14.1 points, stress −95.9%. Win rate 33%, avg hold 61 bars. High-turnover and cost-fragile: the gross-to-net gap and the stress collapse make it undeployable at any cost assumption above optimistic-retail.

**13. V2 (multi-factor).** Net +84.4% (gross +104.7%), Sharpe 0.837, maxDD −21.8%, 2,671 trades, turnover 705×, cost drag 14.1 points, stress −94.7%. The strongest holdout window of any engine (+18.6%) but the same cost fragility as V1, and Sharpe below buy-and-hold.

**14. V3 (multi-timeframe).** Net +74.7% (gross +78.2%), **Sharpe 0.978, Sortino 1.418, maxDD −12.9%** (drawdown duration 7,776 bars), 507 trades, turnover 152×, cost drag 3.1 points, stress −10.6% (survives where V1/V2 die). Win rate 39%, profit factor 1.53, avg hold 258 bars (~10 sessions). P&L 100% realized — the only engine with zero open terminal positions. Its character is fully described by §41's regime table: it beat the buy-and-hold mean in **all five negative windows** and lost to it in **all seven positive ones**.

**15. V4 (calibrated probability).** Methodology as §9. Net +15.3% — produced entirely by 2 positions (§16). Its table-topping Sharpe 1.181 is an artifact of 6.8% exposure and is refuted by its own stability rows: 98% of P&L from one window, 63% from one symbol, one completed trade in 4.9 years.

**16. V4 null-selection frequency.** **116 of 120 cells (96.7%) selected the class-frequency null.** The four exceptions cleared the 0.002 gate by 0.0003–0.0013 and every one fails out of sample (ground-truth labels on the scored window, models scored through the shipped path):

| Cell | Family | Inner margin | OOS log-loss gain vs raw null | OOS AUC | Window economics |
|---|---|---|---|---|---|
| AMZN/w01 | logistic | +0.0023 | −0.00007 | 0.515 | 3 signals, 0 trades, 0.0% |
| QQQ/w04 | logistic | +0.0033 | +0.00011 | 0.527 | 0 signals (max prob 0.667 < conf floor) |
| QQQ/w05 | logistic | +0.0026 | **−0.0051** | 0.515 | 1 signal from a **one-row 0.99984 bin**, held to study end → +60% continuous |
| TSLA/w04 | GBM | +0.0023 | **−0.0187** | 0.498 | 11 signals incl. one-row 0.9998 bin → +57% |

Across all 120 cells the selected models' mean OOS gain is −0.00068; the shadow models (both non-selected families trained anyway, 236 fits) average **−0.0097 gain and AUC 0.4998–0.5033** (range 0.459–0.550, both sides of chance). The gate is hiding nothing: what it rejects is worse than a constant. The horizon study's `FEATURE_INFORMATION_LIMIT` finding **generalizes from two correlated ETFs to the full ten-symbol universe**.

**17. V4 calibration audit.** All 330 trained models audited (per-step validation support via the shipped isotonic apply rule). **158 (48%) carry an extreme calibrated step (≥0.99 or ≤0.01) supported by fewer than 30 validation rows** — among the 220 non-constant families, ~72%, matching the horizon study's 77%. The thinnest observed supports are **one row**, repeatedly. Both economically consequential V4 entries in this study came from one-row bins. Calibrated ECE remains in-sample by construction and cannot detect this. **No V4 or V5 output should be allowed to size a position until extreme thin-bin steps are handled**; this is now a three-study finding.

**18. V5 (ensemble).** Net +137.2% — the best engine net — with Sharpe 0.887, Sortino 1.261, maxDD −30.2%, exposure 0.842, 10 completed trades, avg hold 11,429 bars (~1.7 years), 92% of P&L unrealized in open positions on 7/10 sleeves. Its profile is a noisier buy-and-hold: Sharpe 0.887 vs 0.872, and its per-window record (+8/−4) tracks the market's sign in 11 of 12 windows.

**19. V3 vs V5 disagreement.** Of 318,726 compared bars, 96,075 differ: **95,909 are V5 suppressing a V3 signal, 166 are V5 adding one.** With V4 null nearly everywhere, the null's constant base-rate probability is blended into V5 as a small bullish thumb (the horizon study's measured mechanism), pushing the ensemble past its bands into fewer exits and near-permanent longs. Where trends ran (NVDA +7.19 vs V3's +3.58, GOOGL +1.95 vs +0.32), suppression looked brilliant; where they broke (TSLA −0.17 vs V3's +0.68, META +1.15 vs +1.37, AAPL +0.45 vs +0.66), it was costly; net across the market regime table, V5 loses −8.2%/yr in broad-drawdown bars where V3 makes +16.8%/yr. **The difference is not driven by information — V4 carries none (§16) — so V5's advantage over V3 is leveraged market exposure, not defensible ensemble value.**

## 20–29. Per-symbol results (continuous w01–w12, realistic cost, net return)

| Engine | SPY | QQQ | IWM | AAPL | MSFT | NVDA | AMZN | GOOGL | META | TSLA |
|---|---|---|---|---|---|---|---|---|---|---|
| V1 | +0.243 | +0.384 | −0.076 | +0.361 | +0.024 | +3.769 | +0.285 | +0.962 | −0.079 | +1.258 |
| V2 | +0.273 | +0.536 | −0.148 | +0.218 | −0.003 | +4.985 | +0.140 | +0.779 | +0.325 | +1.333 |
| V3 | +0.138 | +0.308 | −0.012 | +0.662 | +0.292 | +3.579 | +0.128 | +0.319 | **+1.374** | +0.678 |
| V4 | 0.000 | +0.962 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | +0.566 |
| V5 | +0.705 | +0.718 | +0.131 | +0.448 | +0.455 | +7.186 | **+1.151** | **+1.951** | +1.145 | −0.168 |
| B&H | +0.765 | +0.981 | +0.337 | +1.223 | +0.796 | +9.392 | +0.609 | +1.568 | +0.697 | +0.347 |

Buy-and-hold wins net on 7–8 of 10 symbols per engine. Exceptions: V3 on META (+1.37 vs +0.70) and TSLA; V5 on AMZN, GOOGL, META; V1/V2 on TSLA. V3's max drawdown is shallower than buy-and-hold's on **10 of 10** symbols (e.g. META −17.5% vs −75.0%, NVDA −36.9% vs −68.1%). V4's two nonzero cells are the §16 artifacts. Full per-symbol metric blocks (Sharpe, Sortino, vol, drawdown duration, trades, turnover, exposure, win rate, holding, profit factor, drag): `finalize/*_summary_full.json`.

## 30. Portfolio results

Equal-capital **independent sleeves** ($10,000 per symbol from $100,000; the shipped `replay_portfolio`). This is explicitly *not* a shared-account simulation — sleeves never compete for a dollar, and no claim of shared-capital interaction is made.

| Engine | Net | Gross | Sharpe | Sortino | MaxDD | Trades | Turnover | PF | +Symbols | +Windows | Cost drag | Forced-liq net | Key weakness |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| V1 | +71.3% | +91.2% | 0.755 | 1.049 | −25.2% | 2,800 | 705× | 1.20 | 8/10 | 7/12 | 14.1 pts | +71.3% | cost-fragile: stress −96% |
| V2 | +84.4% | +104.7% | 0.837 | 1.164 | −21.8% | 2,671 | 705× | 1.23 | 8/10 | 7/12 | 14.1 pts | +84.4% | cost-fragile: stress −95% |
| **V3** | +74.7% | +78.2% | **0.978** | **1.418** | **−12.9%** | 507 | 152× | 1.53 | 9/10 | 8/12 | 3.1 pts | +74.7% | captures ~45% of B&H net; loses every strong up-window |
| V4 | +15.3% | +15.3% | (1.18)* | (1.71)* | −3.4% | 1 | 0.4× | n/a | 2/10 | 2/12 | 0.0 pts | +15.3% | 116/120 null; both positions are one-row calibration-bin artifacts |
| V5 | +137.2% | +137.3% | 0.887 | 1.261 | −30.2% | 10 | 2.7× | 1.67 | 9/10 | 8/12 | 0.1 pts | +137.2% | quasi-B&H by exit suppression; 92% unrealized |
| Cash | 0.0% | 0.0% | — | — | 0.0% | 0 | 0 | — | — | — | 0 | 0.0% | — |
| B&H | +167.1% | +167.2% | 0.872 | 1.238 | −45.3% | 0 | 1× | n/a | 10/10 | 8/12 | 0.0 pts | +167.0% | −45% drawdown, −75% on META |

*V4's risk figures describe 6.8% exposure across 2 artifact positions and carry no evidential weight.

## 31. Cash comparison

Every engine beats cash net-of-costs over the full period (V4's +15.3% included). In a sample whose buy-and-hold return is +167%, beating cash with long-only exposure is **beta, not edge**, and this study does not count it as one.

## 32. Buy-and-hold comparison

No engine beats buy-and-hold's net at the portfolio level or on a majority of symbols (V3: 48/110 development symbol-windows; V5: 30/110). On risk-adjusted measures exactly one engine does: V3 (Sharpe 0.978 vs 0.872; Sortino 1.418 vs 1.238; maxDD −12.9% vs −45.3%). §41 and §49 weigh whether that is defensible; the holdout (§10, §44) did not contradict it.

## 33. Realized / unrealized decomposition (continuous, realistic, summed sleeves)

| Engine | Realized | Unrealized | Open terminal positions |
|---|---|---|---|
| V1 | +$440,676 | +$40,400 | 10/10 |
| V2 | +$525,809 | +$45,186 | 10/10 |
| V3 | **+$761,483** | **$0** | **0/10** |
| V4 | +$56,620 | +$60,048 | 1/10 |
| V5 | +$83,493 | **+$963,396** | 7/10 |
| B&H | $0 | +$1,236,151 | 10/10 |

V5's result is 92% an unrealized mark on multi-year open positions; V3's is 100% realized cash.

## 34. Forced-final-liquidation results

Every replay reports both terminal states; the forced sale is priced under the same cost model as every prior fill. At 2 bp slippage the two states differ by ≤4 bp of return for every engine (table §30, `forced-liq net` column; per-cell in `cells/*.json → terminal`). **No engine's ranking depends on the terminal-bar treatment.**

## 35–37. Trade frequency, turnover, cost drag

V1/V2: ~2,700–2,800 round trips, 705× turnover, 14.1 points of drag — their gross edge does not survive honest friction, and stress costs (−95%) settle the question. V3: 507 trades, 152× turnover, 3.1 points drag. V5: 10 trades, 2.7× turnover, 0.05 points. V4: 1 trade. Cost sensitivity is monotone and correctly ordered in every cell across frictionless → realistic → stress.

## 38. Max drawdown comparison

V3 −12.9% < V2 −21.8% < V1 −25.2% < V5 −30.2% < B&H −45.3% (portfolio). V3 is shallower than buy-and-hold on all ten symbols individually; V5 matches B&H's deep drawdowns on the symbols that fell hardest (−68% NVDA sleeve, −69% TSLA sleeve).

## 39. Risk-adjusted comparison

Portfolio Sharpe: V3 0.978 > V5 0.887 > B&H 0.872 > V2 0.837 > V1 0.755. Sortino: V3 1.418 > V5 1.261 > B&H 1.238 > V2 1.164 > V1 1.049. Only V3's margin comes with a mechanism (§41) rather than more exposure; V5's 0.015 Sharpe edge over B&H is noise-level and disappears in the Sortino/drawdown trade-off. Per-symbol, V3 beats B&H's Sharpe on 5 of 10 — the portfolio-level advantage comes from diversification of its exit timing across symbols.

## 40. Per-window stability

Mean per-window net return (10 symbols, realistic; B&H mean for context):

| Window | V1 | V2 | V3 | V4 | V5 | B&H |
|---|---|---|---|---|---|---|
| w01 | +0.037 | +0.049 | +0.038 | 0.000 | −0.042 | −0.020 |
| w02 | −0.009 | −0.013 | −0.017 | 0.000 | −0.088 | −0.162 |
| w03 | −0.085 | −0.065 | −0.022 | 0.000 | −0.059 | −0.169 |
| w04 | +0.137 | +0.143 | +0.197 | +0.057 | +0.072 | +0.427 |
| w05 | +0.048 | +0.046 | +0.089 | +0.001 | +0.038 | +0.158 |
| w06 | +0.133 | +0.119 | +0.144 | 0.000 | +0.167 | +0.252 |
| w07 | −0.007 | −0.004 | +0.071 | 0.000 | +0.020 | +0.076 |
| w08 | +0.134 | +0.154 | +0.061 | 0.000 | +0.104 | +0.254 |
| w09 | −0.039 | −0.040 | −0.004 | 0.000 | +0.010 | −0.032 |
| w10 | +0.098 | +0.091 | +0.072 | 0.000 | +0.163 | +0.267 |
| w11 | −0.080 | −0.086 | −0.047 | 0.000 | −0.039 | −0.088 |
| w12 (holdout) | +0.145 | +0.186 | +0.003 | 0.000 | +0.039 | +0.206 |

Positive windows: V3 and V5 8/12, V1/V2 7/12, V4 2/12. Dispersion (dev σ of window mean): V3 0.074, V1/V2 0.084, V5 0.087. Strongest-window P&L fraction: V1 28%, V2 32%, V3 34%, V5 43%, **V4 98%** — V4 is flagged as single-window-dominated; no other engine is. V3 beat the B&H mean in all five negative windows and lost in all seven positive ones — that regularity *is* the engine.

## 41. Regime analysis (and per-symbol stability)

Three causal, deterministic labellings (nothing fitted, nothing retrospective): SPY's trailing-peak drawdown state (calm > −5%, pullback −5..−10%, drawdown ≤ −10%), V3's own stored per-bar regime, and per-symbol trailing 26-bar realized vol vs its trailing 60-session median. Annualized mean per-bar net return, pooled across symbols:

| Regime (bars) | V1 | V2 | V3 | V5 |
|---|---|---|---|---|
| market: calm (180,883) | +16.4% | +15.9% | +14.2% | +34.2% |
| market: pullback (49,487) | +4.5% | +3.9% | −3.9% | +8.1% |
| **market: drawdown (88,346)** | −2.9% | −0.0% | **+16.8%** | −8.2% |
| volatility: high (148,617) | −2.9% | −2.3% | +14.6% | +18.9% |
| volatility: low (143,319) | +20.4% | +20.8% | +9.2% | +15.1% |

V3 is the only engine that *makes* money in broad-market drawdowns — observed in both independent drawdown episodes (2022 bear: beat B&H 10/10 symbols in w02, 8/10 in w03; 2025 spring: 7/10 in w09) — and the only one positive in high-vol bars alongside V5. Its cost is the pullback whipsaw (−3.9%/yr) and weak up-capture.

**Symbol stability.** Positive symbols: V3 and V5 9/10, V1/V2 8/10, V4 2/10. Best symbol is NVDA for every engine.

## 42. Leave-one-symbol-out sensitivity

Removing NVDA (the luckiest symbol for every engine): V1 +37.4%, V2 +38.4%, V3 +43.2%, V5 +72.6% (vs B&H-without-NVDA ≈ +81%). Every engine's sign survives; nobody's thesis rests on one ticker — and nobody's deficit to buy-and-hold closes either. V4 without QQQ: +6.3%, i.e. the artifact halves.

## 43. (folded into 41) — regime definitions and evidence above.

## 44. Leakage evidence

- **Causality audits: 50/50 PASS** (5 engines × 10 symbols; 5 probes each, probes strictly inside the scored region at the study's 4,750-bar lookback; whole-record comparison — signal, score at 9 dp, confidence, regime, reasons; the shipped auditor run alongside). **0 changed decisions, 0 vacuous probes.** The audit frame ends mid-region (w06) and V4/V5 were audited under the model actually serving those bars (w07's).
- **Training gaps:** stored-instant assertions on all 120 cells — every model's last training bar precedes its first scored bar by ≥30 bars.
- **Holdout gating:** enforced by the runner and evidenced by `holdout_unlock.json`'s digest of the frozen conclusion.

## 45. Stored = live evidence

Every window verified before its checkpoint was accepted: V1/V2/V5 stored series vs freshly driven live engines on ~12 sampled bars per engine per window, and the single-pass V3/V4 recovery vs standalone engines on ~8 sampled bars per window under **whole-record bit-identity** (every reason token included). ~6,200 sampled decisions across 120 cells: **zero mismatches**. The single-pass optimization (V3/V4/V5 from one V5 drive — the pilot's measured 42% saving) was therefore used everywhere and proven everywhere.

## 46. Reproducibility

`repro/study_reproducibility.json`: dataset digests re-verified 10/10; three predeclared representative cells (SPY/w05, GOOGL/w09, NVDA/w07) **retrained from scratch to byte-identical serialized records** (fixed `trained_at`; artifacts, comparisons, audits identical); the head of each representative window **re-scored to row-identical stored series** for V3, V4 and V5 (120 bars each). All PASS. `run_manifest.json` records code SHA `156ad86`, base SHA `aee7a77`, every dataset fingerprint, the full split definitions, seeds, cost models, and configuration.

## 47. Full regression result

Targeted study gates ran clean throughout (`ruff check`, `ruff format --check`, `git diff --check`, 45 study tests + 66 carried pilot/data tests). The full repository suite was run after heavy scoring completed, alone on the machine: **2,881 passed, 43 skipped, 0 failed** (151 s; recorded in `regression_result.txt` beside this report).

## 48. Limitations

1. **One macro epoch.** 2021–2026 is a single, extraordinary large-cap bull cycle with two drawdown episodes. Every conclusion — including V3's defensive profile — is conditional on it.
2. **Long-only engines in a rising market.** "Beats cash" is beta here. Buy-and-hold is the binding benchmark and it was only beaten risk-adjusted, by one engine.
3. **Independent sleeves.** The portfolio result is the sum of ten $10k books; production shares one account and its risk ceilings. Sequencing/contention effects are unmodelled by explicit design of the shipped research layer.
4. **IEX single-venue volume** feeds every volume-derived feature and gate; a SIP feed would change inputs. GOOGL/AMZN early-history thinness measurably raised the warm-up requirement (§8).
5. **Flat 2 bp slippage**, no market impact, spread widening, or size dependence; fine for these liquid names at small notional, not beyond.
6. **Split-adjusted, not dividend-adjusted**; holding across ex-dates shows price steps that are not losses. Affects level comparisons by ~1–2%/yr for the index ETFs.
7. **V5's figures are mostly unrealized marks** on multi-year positions (92%), and window-level V5/V4 replays understate what a position held across a window boundary experiences in the continuous replay (both are reported).
8. **The holdout is partially known for SPY/QQQ** through the pilot; virgin for the other eight and all portfolio figures.
9. **Sharpe on 15m bars** annualized with the equity clock (6,552 bars/yr); overnight gap risk (mean |gap| 0.44–1.5% per symbol) dominates modelled slippage ~20–75×, so cost conclusions are about friction, not gap risk.
10. **Trade-level statistics** for V5 (10 trades) and V4 (1) carry no weight; they are reported because the format requires them.

## 49. Whether any engine has credible Equity edge

- **Net edge over buy-and-hold: no engine, full stop.** The best net (V5 +137%) trails B&H by 30 points and is B&H-shaped exposure obtained by suppressing exits on a null-informed blend.
- **Predictive edge in V4: none.** 116/120 null; survivors fail OOS; shadows are worse than a constant at chance AUC. Three studies now converge on `FEATURE_INFORMATION_LIMIT` for the shipped seven features.
- **V5 over V3: not for defensible reasons.** The mechanism is a base-rate thumb, not information.
- **V3: a credible defensive risk-adjusted profile, short of an edge claim.** Better Sharpe/Sortino than B&H with a 3.5×-shallower drawdown, a coherent measured mechanism (drawdown-bar profitability, better-than-random timing), reproduced across two drawdown episodes, 9/10 symbols positive, LOSO-robust, stress-cost-survivable, and holdout-consistent (its +0.3% in a +20.6% B&H window passed the pre-declared direction-conditional test, exactly as its up-window weakness predicted). It earns further *observation*, not deployment, and not a robust-candidate claim.

## 50. Exact operational recommendation

1. **Do not deploy anything.** No engine met the robust-candidate bar.
2. The next equity step in the roadmap remains the **operational SPY Paper smoke** (explicitly out of this study's scope and not performed). Only after it passes, and only if the operator chooses to spend the horizon on it: **LIVE SHADOW for V3 alone** — decisions recorded, nothing executed — judged against the pre-registered expectation that its value is defensive (shallower drawdowns, drawdown-regime profitability), not net outperformance. V5 may be shadowed only as an observer of V3+null-V4 blending; V1/V2 have no path at realistic-to-adverse costs; V4 must not size anything while one-row calibration bins can emit 0.9998 (fix or floor extreme-step support first — a modelling decision this study deliberately did not make).
3. Before any *further* V4 research: not another OHLCV tuning pass. The three-study evidence points at new information (the crypto studies' conclusion) or a different label family — and either belongs after the operational milestones, not before.

---

## Summary decision answers

1. **Best net engine:** V5 (+137.2%) — still 30 points behind buy-and-hold.
2. **Best gross engine:** V5 (+137.3%); best gross-vs-net survivor V3 (3.5-point gap).
3. **Lowest drawdown:** V4 (−3.4%, artifact) → meaningfully: **V3 (−12.9%)**.
4. **Most stable across symbols:** V3 and V5 (9/10 positive); V3 with the smaller best-symbol dependence (48% vs 52%).
5. **Most stable across windows:** V3 (8/12 positive, lowest dispersion 0.074, best-window share 34%).
6. **Does V4 provide useful incremental information? No.** 116/120 null; the rest is calibration artifact.
7. **Does V5 outperform V3 for defensible reasons? No.** More exposure via null-blend exit suppression.
8. **Does any engine beat cash after realistic cost? Yes, all five** — in a +167% B&H sample; this is beta.
9. **Does any engine beat buy-and-hold on a defensible risk-adjusted basis? V3, at the portfolio level** (Sharpe 0.978/Sortino 1.418/maxDD −12.9% vs 0.872/1.238/−45.3%), with a measured mechanism; per-symbol it wins Sharpe on only 5/10, so the claim is portfolio-level and defensive.
10. **Does any result survive removal of the strongest symbol? Yes** — all engines' signs survive LOSO (V3 +43% ex-NVDA); no deficit to B&H closes.
11. **Does any result survive removal of the strongest window? Yes** for V1/V2/V3/V5 (best-window shares 28–43%); **no** for V4 (98%).
12. **Does the final holdout support or reject the development conclusion? Supports it** — V4 10/10 null; V3 passed the pre-declared H1 (positive, shallower drawdown in an up-window) while capturing almost none of the rally, exactly as the development characterization predicted; V5 stayed quasi-passive.
13. **Enough evidence for Equity LIVE SHADOW after operational Paper validation? Yes, narrowly, for V3 only** — on the defensive risk-adjusted evidence, under the pre-registered expectations above.

---

## Final classification

### B. PROMISING EQUITY SIGNAL, BUT LIVE SHADOW REQUIRED

The rule frozen before the holdout: B if H1 (direction-conditional benchmark test) passed and H2 (V4 null-consistency) showed no genuine information. Both passed. The promising signal is exactly one thing — **V3's defensive risk-adjusted profile** — and the evidence excludes everything stronger: no engine beats buy-and-hold net, V4 carries no predictive information, and V5's ensemble premium is exposure. This is not authorization to deploy; the SPY Paper smoke and a pre-registered LIVE SHADOW are the mandatory gates between this report and any further claim.

**EQUITY 10-SYMBOL HISTORICAL EVALUATION COMPLETE — LIVE SHADOW REQUIRED.**
