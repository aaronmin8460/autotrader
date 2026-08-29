# Decision V5 — the versioned ensemble

V5 combines V3's **deterministic multi-timeframe score** with V4's **calibrated
probability**, attenuates the combination by **market regime** and by
**volatility**, and emits a BUY / HOLD / SELL **candidate** with a bounded
confidence and an exact account of which input moved it and by how much.

Nothing in this milestone activates in production. No default flips to V5, no
runtime starts preferring it, no gate opens, and no risk limit, account halt,
reconciliation rule or at-most-once guarantee changes. V5 produces a candidate;
the Risk Engine remains the only thing that decides whether a candidate becomes
an order.

---

## 1. Where it sits

    Decision Engine → DecisionResult → Risk Engine → Order Intent → Execution
    ^^^^^^^^^^^^^^^
    V1  V2  V3  V4  V5

| Module | Holds |
| --- | --- |
| `autotrader.decision.ensemble` | The ensemble **as a value**: weights, the hold band, regime adjustments, the blending arithmetic, and the attribution |
| `autotrader.decision.v5` | The engine: bars → V3 + V4 → attenuation → `DecisionResult` |

`ensemble.py` reads no bar and `v5.py` fits nothing. Both live inside the
decision package's existing fence: they may import only `pandas`, `math` and the
short allowlist in `tests/test_decision_contract.py::ALLOWED_IMPORT_ROOTS`, and
may not open a file, read a clock, or name a broker type. Those guards walk
every file in the package, so they covered the two new modules the moment they
appeared, and none of them was modified.

---

## 2. The arithmetic

Four steps, in this order. The order is part of the contract, because the
attribution is a chain decomposition along it.

```
blend  = w_v3·score_v3 + w_v4·score_v4                      # weights sum to 1
agree  = (1 + score_v3·score_v4) / 2                        # in [0, 1]
base   = m_c·(w_v3·conf_v3 + w_v4·conf_v4) + m_a·agree      # mix sums to 1

score      = blend · regime · volatility
confidence = base  · regime · volatility
```

`regime` and `volatility` are multipliers in `[0, 1]`. They **attenuate and
never amplify**, which is two properties at once:

- **The bounds hold by construction.** `blend` is a weighted mean of two values
  in `[-1, +1]` with weights summing to one, so it is in `[-1, +1]`; `base` is
  the same argument over `[0, 1]`. Multiplying either by numbers in `[0, 1]`
  cannot leave the interval. Nothing is clipped back into range — and
  `_require_bounded` **raises** on a genuinely out-of-range value rather than
  clamping it, so a construction that stopped being bounded is loud. Only
  floating-point drift past a bound the arithmetic already respects is absorbed.
- **Context cannot vote.** A regime reading is not an opinion about direction. If
  it were additive, a quiet market could manufacture a candidate out of two
  engines that named none. The most it can do is take conviction away.

**Confidence is not the score by another name.** V4 cannot separate the two — one
probability cannot corroborate itself — which is exactly what an ensemble can
fix. Two engines each highly confident and pointing opposite ways produce a score
near zero *and* a low confidence, because `agree` collapses. Two engines that
barely have an opinion do not become confident by sharing a sign, because the
component term stays small.

---

## 3. The HOLD band

`EnsembleBand` carries `buy_score`, `sell_score` and `min_confidence`, travels
with the ensemble version, and is recorded in the policy metadata of every
decision — configuration, not a constant in a branch.

**The band is closed.** V2 and V4 name a direction at `score >= buy_score`. V5
requires strictly more:

| Score | V5 |
| --- | --- |
| `> buy_score` | BUY |
| `== buy_score` | **HOLD** |
| inside the band | HOLD |
| `== sell_score` | **HOLD** |
| `< sell_score` | SELL |

A decision on the boundary resolves to HOLD rather than to whichever side is
marginally ahead. An ensemble whose two inputs cancel to a rounding error has
found a tie, and a tie is the thing a hold band exists to express. The band's
metadata records `"boundary_resolves_to": "HOLD"` so an audit reads the
convention off the decision rather than off this file at some later version.

**The band may not be narrower than the policy's.** `require_not_wider_than_policy`
is checked when the engine is built: V5 may refuse where V2/V3/V4 would trade and
may never trade where they would refuse. The shipped band is strictly wider than
both asset-class policies on all three numbers (0.30 / −0.30 / 0.45 against
crypto's 0.25 / −0.25 / 0.35 and equity's 0.25 / −0.25 / 0.40). Without that
check an ensemble would be a way to loosen the shipped thresholds while looking
like a combination, and nothing downstream could tell the two apart from the
candidate alone.

Gate order is V2's, deliberately: confidence floor, then the buy side (where a
high-volatility regime still refuses), then the sell side (which nothing
refuses), then the band.

---

## 4. Regime and volatility

**The regime is read on both timescales.** V3 classifies the 4-hour context, V4
classifies the base bar, and `combine_regimes` takes the union:

| Context | Base | Ensemble |
| --- | --- | --- |
| anything | `HIGH_VOLATILITY` | `HIGH_VOLATILITY` |
| `HIGH_VOLATILITY` | anything | `HIGH_VOLATILITY` |
| anything | `UNKNOWN` | `UNKNOWN` |
| otherwise | — | the context regime |

Disorder on either scale is disorder. Taking only the broad reading would let a
violent 15-minute bar inside a calm four hours be scored as though the hour were
calm.

`RegimeAdjustments` is how much conviction each state leaves intact, and the five
numbers are **ordered rather than free** — validated, not merely documented:

| State | Multiplier | Why |
| --- | --- | --- |
| trend, reading aligned | 1.00 | the case the engines were built for |
| range | 0.80 | a range is a weaker argument for a directional call |
| trend, reading opposed | 0.50 | counter-trend, and it says so |
| high volatility | 0.40 | disorder, on top of the outright BUY block |
| unknown | 0.00 | no conviction survives, so no band can be cleared |

`unknown` at zero makes an unclassified regime a HOLD **by arithmetic rather
than by a branch**. It is unreachable today — `classify_regime` always names a
state and an unavailable component stops the decision earlier — and it is the
right answer if it ever stops being unreachable.

**V5 introduces no volatility constant of its own.** The attenuation is
`scoring.volatility_factor` on the base bar against the asset-class policy's
`high_volatility_ratio` — the same inverse-excess discount V2 already applies,
1.0 inside the tolerance and falling as `limit / ratio` beyond it. The higher
timeframe's expansion has already entered through the regime, so a second
constant would be a second copy of an argument `config.py` has made once.

---

## 5. Component attribution

Every available decision carries an exact decomposition. Contributions are the
chain differences along the ordering in §2:

| Component | Score contribution | Confidence contribution |
| --- | --- | --- |
| `v3_deterministic` | `w_v3·score_v3` | `m_c·w_v3·conf_v3` |
| `v4_probability` | `w_v4·score_v4` | `m_c·w_v4·conf_v4` |
| `component_agreement` | — | `m_a·agree` |
| `regime` | `blend·(r − 1)` | `base·(r − 1)` |
| `volatility` | `blend·r·(v − 1)` | `base·r·(v − 1)` |

They telescope: the terms sum to `blend·r·v` exactly. `EnsembleAttribution`
**refuses to exist** unless they do. An attribution that does not reconstruct its
own decision is worse than none — it looks like an audit trail, survives
serialization, and is discovered to be wrong by whoever eventually needs it.

An attenuation's contribution is signed against the direction it shrinks, which
is why the reason tokens read `COMPONENT_REGIME_LOWERED` rather than borrowing
V2's bullish/bearish vocabulary: the regime term on a bullish bar is negative,
and calling that "bearish" would report the regime as bearish when what happened
is that it removed conviction from a bullish reading.

Because two attenuations applied in sequence have no order-free decomposition,
`ATTENUATION_ORDER` is written down and travels in the record.

**Recoverable after the fact** means after `to_dict`, a log line and a JSON round
trip: the attribution lives in `DecisionResult.policy["attribution"]`, which is
the half of the record that survives serialization.

---

## 6. Both components, or no decision

An unavailable V3 or an unavailable V4 is a HOLD naming which component was
missing and the token it gave — never a fallback to the one that answered.
Falling back would silently turn V5 into V4 on exactly the bars where the
multi-timeframe context could not be established, which is when that context is
most load-bearing.

`scoring.unavailable_reasons` is how that is detected: the two prefixes
`INSUFFICIENT_HISTORY` and `FEATURE_UNAVAILABLE` are now named constants rather
than an inline format string, so "V3 had no answer" is a fact a consumer reads
instead of one it infers from a score of zero — which is also what a genuinely
balanced bar scores, and the two mean opposite things.

The price is stated up front. `required_base_bars` is the larger of the two
requirements, which is V3's:

| | V4 alone | V5 |
| --- | --- | --- |
| crypto | 109 | **1744** |
| equity | 109 | **2834** |

Both exceed `MAX_LOOKBACK_BARS`, which this milestone deliberately does not
change, for the same reason D1 gave: the runtime's fetch window is runtime policy
with a real API-budget cost, and widening it is a decision for whoever wires an
engine in.

---

## 7. Versioning

The ensemble is versioned the way V4 versions its models, with the same two
identifiers for the same two failure modes:

| Identifier | Changes when | Invalidates |
| --- | --- | --- |
| `ENSEMBLE_CONTRACT_VERSION` | the record's *shape* changes | every stored ensemble at once |
| `EnsembleSpec.ensemble_version` | any weight, band or adjustment changes | one configuration |

`EnsembleSpec.from_record` refuses a record written under another contract
version rather than reading it as though the fields still meant what they used
to. A decision's policy metadata carries the ensemble version, every number under
it, both engine versions, and the trained model's own identity — model version,
family, feature version, label spec, calibration method — so a stored decision
can be matched to exactly what produced it without consulting anything outside
the record.

The shipped ensemble is `v5-balanced-1.0.0`: equal weights, the wide band, and
calibration required.

**The weights are not fitted.** There is no walk-forward evidence in this
repository that a deterministic score deserves more weight than a calibrated
probability or the reverse, and a weight chosen by looking at this system's data
would be exactly the fitted constant §2 of the spec refuses. Equal is the choice
that involves no tuning; a deployment with evidence sets them and names the
result.

---

## 8. Calibration is required by default

V4 reports whether its probability was calibrated at all. Blending an
uncalibrated logistic score with a deterministic one *as though it were odds* is
an unstated assumption buried inside a number the layers downstream are entitled
to read as a probability.

So the shipped ensemble refuses an uncalibrated artifact **when the engine is
built**, not at the thousandth bar of a backtest. A deployment that means to run
one sets `requires_calibration=False`; the flag is recorded with every decision
and an `ENSEMBLE_MODEL_UNCALIBRATED` token appears on every result, so an audit
can tell which regime a candidate was produced under.

---

## 9. What V5 cannot do

| | Guard |
| --- | --- |
| Reach a broker, execution, risk, state or reconciliation | `test_decision_contract.py::test_the_decision_package_imports_nothing_that_can_reach_a_broker` and `::test_the_decision_package_never_names_an_execution_or_state_module` (walk every file) |
| Import outside the decision fence | `test_decision_v5.py::test_the_new_modules_stay_inside_the_decision_packages_import_boundary` |
| Size, price or approve anything | `::test_a_v5_candidate_carries_nothing_that_could_size_or_place_an_order`, `::test_the_engine_exposes_no_ordering_sizing_or_approval_surface` |
| Bypass or pre-approve risk | `::test_the_risk_engine_still_needs_what_no_decision_can_supply` — a V5 record carries neither `reference_price` nor `requested_quantity`, and `evaluate_risk` takes a request, a context and a policy |
| Become the default anywhere | `::test_nothing_outside_the_decision_package_has_started_preferring_v5`, `::test_the_runtimes_still_call_the_crossover_they_always_did` |
| Read a bar that has not happened | `test_decision_contract.py::test_no_decision_module_contains_a_look_ahead_construct`, `test_decision_v5.py::test_a_future_bar_cannot_change_a_decision_already_made` |

`DecisionResult.is_actionable` remains what it always was: a statement that an
engine produced a candidate, and no statement at all about whether risk, account
safety or reconciliation will let it become an order.

---

## 10. What was deliberately not built

- **No runtime wiring, no activation switch, no default.** Same posture as V2,
  V3 and V4.
- **No fitted weights.** See §7. Fitting them needs walk-forward evidence this
  repository does not have, and producing that evidence is a research task, not
  a side effect of building the combiner.
- **No third component.** V1's crossover is inside V3's lineage already, and V2
  is V3's base timeframe; adding either would be counting one reading twice.
- **No per-symbol ensemble.** The features are unit-free and the policies are
  per asset class, so a per-symbol configuration would be tuning wearing a
  structure's clothes.
- **No stacking or meta-learner.** A trained combiner over two engines' outputs
  is a fourth model with its own leakage surface, and the walk-forward apparatus
  to justify one is the same evidence §7 says does not exist yet.
