"""Shadow mode: observe every decision engine version, execute exactly one.

    Decision Engine -> DecisionResult -> Risk Engine -> Order Intent -> Execution

This package sits beside the leftmost box and can reach none of the others. It
runs V1 through V5 over one completed bar, writes down what each of them
decided, and hands back **at most one** execution candidate - the one belonging
to the version a caller explicitly configured. The other four are observational,
permanently, and not as a matter of policy: there is no code path that turns one
of them into a candidate.

**What it imports, which is the argument.** `autotrader.decision` for the
engines, `autotrader.state` for the table, pandas, and the standard library.
Nothing here imports the execution layer, the risk engine, the reconciliation
layer, the account layer, a runtime that holds a gateway, or a provider SDK - so
the sentence "the shadow recorder has no path to the broker" is a statement
about the import graph rather than about anyone's intentions. The processed-bar
checkpoint arrives as a structural protocol (`cycle.BarClaim`) for exactly this
reason: the module that owns the production checkpoint also owns the production
execution gateway.

**The modules.**

``panel``    The shape. Runs every version over one frame and sorts the answers
             into observations and at most one `ExecutionCandidate`, which is a
             type that refuses to exist for a version other than the configured
             one.

``recorder`` The persistence. Writes one row per version into `shadow_decisions`
             (schema v7), five rows or none, holding a connection and nothing
             that could act.

``cycle``    The composition. Claims the bar durably before deciding, records
             every version's answer, and releases the candidate only after the
             record commits - so the storage layer's "at most one execution per
             bar" is a durable guard rather than an audit note.

``versions`` The shipped five, built for one symbol, with no default about which
             of them executes.

**Nothing here is activated.** No runtime constructs a panel, no gate consults a
recorded decision, no default execution version exists, and the crypto and
equity runtimes are untouched - they still evaluate the C3 crossover and still
hand it to the same risk engine. Adding this package changes what this system
trades by nothing at all. Wiring it into a runtime, and choosing which version
that runtime executes, are separate deliberate acts that nothing in this package
performs on anyone's behalf.

**What the record is for.** Every stored decision carries the bar it was made on
and the symbol it was about, which is enough to score any of them - executed or
not - against the price action that followed. The one that was executed
additionally carries the `client_order_id` of the intent it produced, so it can
be scored against what actually happened rather than only against what the
market did. That asymmetry is honest: an observational decision has no order to
be compared with, because it never got one.
"""

from autotrader.shadow.cycle import (
    SKIPPED_ALREADY_PROCESSED,
    BarClaim,
    BarOutcome,
    ShadowClaimError,
    ShadowCycle,
)
from autotrader.shadow.panel import (
    EnginePanel,
    ExecutionCandidate,
    PanelEvaluation,
    ShadowConfigError,
    ShadowError,
    ShadowEvaluationError,
    ShadowFailure,
    ShadowObservation,
    feature_version_of,
    model_version_of,
)
from autotrader.shadow.recorder import ShadowRecorder
from autotrader.shadow.versions import PANEL_VERSIONS, panel_for_symbol

__all__ = [
    "PANEL_VERSIONS",
    "SKIPPED_ALREADY_PROCESSED",
    "BarClaim",
    "BarOutcome",
    "EnginePanel",
    "ExecutionCandidate",
    "PanelEvaluation",
    "ShadowClaimError",
    "ShadowConfigError",
    "ShadowCycle",
    "ShadowError",
    "ShadowEvaluationError",
    "ShadowFailure",
    "ShadowObservation",
    "ShadowRecorder",
    "feature_version_of",
    "model_version_of",
    "panel_for_symbol",
]
