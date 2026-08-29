"""The five versions a shadow panel observes, and what building them costs.

`autotrader.decision` ships V1 through V5 and states that none of them is
preferred, defaulted to, or wired into anything. This module builds all five for
one symbol without changing that: a panel observes every version and executes
whichever one the caller names, and naming one is a decision this module refuses
to make on anyone's behalf.

**There is no default execution version.** `execution_version` is a required
keyword everywhere it appears. Which engine trades is precisely the question
shadow mode exists to answer with evidence, and a default here would answer it
by omission - for whoever forgot to pass one, silently, in production.

**V1 is the only version this system has ever executed**, and that is a fact
about history rather than a recommendation encoded here. A caller wiring shadow
mode into a live runtime today passes `VERSION_V1` because changing what trades
is a separate, deliberate act; a caller comparing engines offline passes
whatever it is comparing.

**The history cost is the panel's, not the cheapest member's.** V5 needs the
4-hour context V3 needs, which on a session-traded symbol is a hundred sessions,
and a panel fetched for V1's fifty bars would record four HOLDs about
insufficient history and look like a comparison. `EnginePanel.required_base_bars`
reports the real number and `panel_for_symbol` inherits it.
"""

from __future__ import annotations

from autotrader.decision.contract import VERSION_V1, VERSION_V2, VERSION_V3, VERSION_V4, VERSION_V5
from autotrader.decision.ensemble import BALANCED_ENSEMBLE, EnsembleSpec
from autotrader.decision.probability import ProbabilityArtifact
from autotrader.decision.v1 import EmaCrossV1Engine
from autotrader.decision.v2 import MultiFactorV2Engine
from autotrader.decision.v3 import MultiTimeframeV3Engine
from autotrader.decision.v4 import ProbabilityV4Engine
from autotrader.decision.v5 import EnsembleV5Engine
from autotrader.shadow.panel import EnginePanel

#: Every version a full panel observes, in evaluation order. Oldest first, which
#: is also cheapest first, so a reader of a stored comparison sees the versions
#: in the order they were built and can tell at a glance which answers came from
#: a model and which from arithmetic.
PANEL_VERSIONS: tuple[str, ...] = (
    VERSION_V1,
    VERSION_V2,
    VERSION_V3,
    VERSION_V4,
    VERSION_V5,
)


def panel_for_symbol(
    symbol: str,
    *,
    artifact: ProbabilityArtifact,
    execution_version: str,
    spec: EnsembleSpec = BALANCED_ENSEMBLE,
) -> EnginePanel:
    """Build all five versions for `symbol` with one of them allowed to execute.

    `artifact` is passed in rather than loaded. The decision package may not read
    a file and neither may this one, so whoever wants a panel supplies the
    trained model - `autotrader.ml.v4` is what reads one off disk - and a caller
    with no model gets an error here rather than a panel that quietly observes
    three versions and calls it five.

    `execution_version` must be one of `PANEL_VERSIONS`; `EnginePanel` refuses a
    version it does not hold, so a typo produces a construction failure rather
    than a panel that observes five engines and trades on none of them.
    """
    engines = (
        EmaCrossV1Engine(),
        MultiFactorV2Engine.for_symbol(symbol),
        MultiTimeframeV3Engine.for_symbol(symbol),
        ProbabilityV4Engine.for_symbol(symbol, artifact),
        EnsembleV5Engine.for_symbol(symbol, artifact, spec=spec),
    )
    return EnginePanel(engines, execution_version=execution_version)


__all__ = ["PANEL_VERSIONS", "panel_for_symbol"]
