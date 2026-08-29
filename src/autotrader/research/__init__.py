"""Quant research infrastructure: how a Decision Engine gets evaluated.

This package exists to make a claim about a strategy checkable before anything
is risked on it. It is **research only**. Nothing in it submits, cancels or
prices an order, contacts a broker, reads a credential, opens a socket, or
touches the operational SQLite state. A study is arithmetic over stored bars.

**The layers, and why they are separate.**

`engines`     the integration contract. A `DecisionEngine` supplies its
              identity, parameters, warm-up and signals - nothing else. Every
              other module here consumes that protocol and names no strategy,
              which is what lets a future Decision Engine V2/V3/V4/V5 be
              evaluated by writing an adapter rather than by this package
              learning about it. Production strategy code is never rewritten to
              suit research; `EmaCrossEngine` adapts the existing crossover
              without reimplementing it.

`costs`       transaction cost assumptions, per asset class, stated by name.
              Slippage is adverse by construction and there is no setting that
              makes trading free by accident.

`replay`      the deterministic simulator. Signals fill at the next bar, never
              their own (docs/SPEC.md section 6F); money is exact `Decimal`;
              the same inputs always produce the same result.

`trades`      round-trip accounting. An open position at the end is reported as
              unrealized rather than folded into the trade list.

`metrics`     performance, with each undefined figure left as `None` rather
              than zero, and annualization tied to an explicit bar clock.

`splits`      contiguous, strictly ordered train/test windows. There is no
              shuffle parameter anywhere, and the final holdout is carved off
              before any window is generated.

`leakage`     the auditor. Structural checks over splits, and perturbation
              checks that prove an engine cannot see the future by trying to
              make it.

`walkforward` one out-of-sample record per window, reported as a distribution
              rather than a single number.

`experiments` bounded sweeps with a hard ceiling, full reproducibility records,
              and a selection that must name the windows it used.

`storage`     external paths only. Refuses to write inside the repository.

`reproducibility`  code version, library versions, dataset digest, seed.

**What this package deliberately does not do.** It does not size against the
production risk engine, model the shared account exposure ceiling, or simulate
the account execution lock: those are runtime properties of a live system, and
a research replay that pretended to model them would produce a number that
looks like a paper-trading forecast and is not. It also makes no claim that any
strategy evaluated through it is profitable.
"""

from autotrader.research.costs import (
    CRYPTO_COST,
    EQUITY_COST,
    STRESS_COST,
    ZERO_COST,
    CostModel,
    cost_model_for,
)
from autotrader.research.engines import (
    Action,
    BuyAndHoldEngine,
    DecisionEngine,
    EmaCrossEngine,
    ParametricEmaCross,
    ResearchSignal,
    describe,
)
from autotrader.research.experiments import (
    MAX_SWEEP_EXPERIMENTS,
    ExperimentRecord,
    ParameterSpace,
    SelectionRecord,
    StudyConfig,
    SweepError,
    SweepResult,
    evaluate_holdout,
    run_sweep,
    select_best,
)
from autotrader.research.leakage import (
    LeakageError,
    LeakageFinding,
    LeakageReport,
    audit_bar_completeness,
    audit_engine_causality,
    audit_feature_causality,
    audit_holdout,
    audit_splits,
    audit_study,
    require_causal_engine,
)
from autotrader.research.metrics import (
    CRYPTO_15M,
    EQUITY_15M,
    BarClock,
    PerformanceMetrics,
    bar_clock_for,
    compute_metrics,
    metrics_for_replay,
)
from autotrader.research.replay import (
    PortfolioResult,
    ReplayConfig,
    ReplayInputError,
    ReplayResult,
    replay,
    replay_portfolio,
)
from autotrader.research.reproducibility import (
    DatasetFingerprint,
    ReproducibilityMetadata,
    dataset_digest,
    fingerprint_dataset,
)
from autotrader.research.splits import (
    HoldoutSplit,
    SplitError,
    SplitScheme,
    TimeSplit,
    holdout_split,
    walk_forward_splits,
)
from autotrader.research.storage import (
    ResearchStorageError,
    resolve_datasets_root,
    resolve_reports_root,
)
from autotrader.research.trades import Fill, FillSide, OpenPosition, Trade, build_trades
from autotrader.research.walkforward import (
    WalkForwardResult,
    WindowResult,
    run_walk_forward,
)

__all__ = [
    "CRYPTO_15M",
    "CRYPTO_COST",
    "EQUITY_15M",
    "EQUITY_COST",
    "MAX_SWEEP_EXPERIMENTS",
    "STRESS_COST",
    "ZERO_COST",
    "Action",
    "BarClock",
    "BuyAndHoldEngine",
    "CostModel",
    "DatasetFingerprint",
    "DecisionEngine",
    "EmaCrossEngine",
    "ExperimentRecord",
    "Fill",
    "FillSide",
    "HoldoutSplit",
    "LeakageError",
    "LeakageFinding",
    "LeakageReport",
    "OpenPosition",
    "ParameterSpace",
    "ParametricEmaCross",
    "PerformanceMetrics",
    "PortfolioResult",
    "ReplayConfig",
    "ReplayInputError",
    "ReplayResult",
    "ReproducibilityMetadata",
    "ResearchSignal",
    "ResearchStorageError",
    "SelectionRecord",
    "SplitError",
    "SplitScheme",
    "StudyConfig",
    "SweepError",
    "SweepResult",
    "TimeSplit",
    "Trade",
    "WalkForwardResult",
    "WindowResult",
    "audit_bar_completeness",
    "audit_engine_causality",
    "audit_feature_causality",
    "audit_holdout",
    "audit_splits",
    "audit_study",
    "bar_clock_for",
    "build_trades",
    "compute_metrics",
    "cost_model_for",
    "dataset_digest",
    "describe",
    "evaluate_holdout",
    "fingerprint_dataset",
    "holdout_split",
    "metrics_for_replay",
    "replay",
    "replay_portfolio",
    "require_causal_engine",
    "resolve_datasets_root",
    "resolve_reports_root",
    "run_sweep",
    "run_walk_forward",
    "select_best",
    "walk_forward_splits",
]
