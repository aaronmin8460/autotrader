"""Historical evaluation of Decision Engines V1 through V5 on crypto bars.

This study answers one question with evidence: over stored BTC/USD and ETH/USD
15-minute history, does the ensemble (V5) earn its complexity against the
simpler engines it is built from?

**It reuses the shipped infrastructure rather than restating it.** The engines
are `autotrader.decision` V1-V5 exactly as they ship. The simulator, cost
models, metrics, splits, leakage audit and walk-forward runner are
`autotrader.research`. The V4 model is fitted by `autotrader.ml.v4` under its
own temporal-split, purge, embargo and calibration rules. This package adds
three things and nothing else:

`dataset`   provenance: what was downloaded, what was corrected, and the
            fingerprint that identifies it.

`adapters`  the seam between the two engine contracts. `autotrader.decision`
            scores the newest completed bar; `autotrader.research` consumes a
            whole frame. `DecisionSeriesEngine` carries a precomputed decision
            series across that gap without either side learning about the
            other.

`scoring`   the pass that produces those series, driving each shipped engine
            over a fixed lookback window, one completed bar at a time, exactly
            as a live runtime would.

**No production code is modified by this study and none of it is activated.**
The output is evidence for a human decision, not a decision.
"""
