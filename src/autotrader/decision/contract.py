"""The versioned Decision Engine contract. One result shape for every version.

A decision engine reads completed bars and returns a `DecisionResult`. That is
the whole surface. Nothing in this package sizes a position, reads an account,
holds a broker client, or reaches a network, and nothing here may ever do so:
per docs/SPEC.md section 7A the pipeline is

    Decision Engine -> DecisionResult -> Risk Engine -> Order Intent -> Execution

and a decision engine sits at the far left of it. It produces *candidates*. The
existing execution, risk and reconciliation layers remain the authority on
whether a candidate ever becomes an order, and this package cannot reach them.

**Why a versioned contract at all.** V1 is the EMA 20 / EMA 50 crossover that
has been in the system since C3. V2 is a deterministic multi-factor score, V3
combines three timeframes, and V4 turns the same measurements into a calibrated
probability; a V5 ensemble is anticipated and deliberately not built here. Those
five things disagree about almost everything internally, and agree about exactly
one thing: on a given completed bar, for a given symbol, they emit a direction, a
bounded score, a confidence, and an audit trail explaining both. That agreement
is this module.

**The result is an audit record, not a suggestion.** docs/SPEC.md section 7D
requires that any order be reconstructible from its inputs. `reasons` carries
stable machine tokens rather than prose, `features` carries the measured values
the score was computed from, and `policy` carries the configuration that was in
force. A `DecisionResult` plus the bars that produced it is enough to replay
the decision exactly, which is what makes `to_dict` worth having.

**Bounds are part of the contract, not a convention.** `score` is in
``[-1, +1]`` and `confidence` is in ``[0, 1]`` for every version, checked on
construction. An ensemble that averages V2 and V4 can only be written if both
operands are known to live on the same scale, and the cheapest place to
guarantee that is here.

**HOLD is a real answer.** V1 had two signal types because a crossover either
happened or did not. Every later version scores continuously, so the absence of
a trade has to be expressible, and it has to carry its reason: too little
history, an unavailable feature, a score inside the hold band, or a regime that
blocks entry are four different facts and an integrator can tell them apart.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

import pandas as pd

from autotrader.equity import EQUITY_SYMBOLS

#: The crypto pair universe, declared here so that this package imports no
#: module that carries a provider SDK. `autotrader.execution.models` owns the
#: authoritative tuple and a test pins these two to it, which is the same
#: declare-and-pin arrangement the runtime already uses for its processing
#: order. Duplicating two strings is cheaper than making a research backtest or
#: a model-training run depend on a broker client library.
CRYPTO_SYMBOLS: tuple[str, ...] = ("BTC/USD", "ETH/USD")

#: Engine version identifiers. Strings rather than an enum because V5 is
#: anticipated but unwritten, and an enum member for a version that does not
#: exist is a promise this branch has no business making.
VERSION_V1 = "v1"
VERSION_V2 = "v2"
VERSION_V3 = "v3"
VERSION_V4 = "v4"

#: Inclusive bounds every version's outputs are checked against.
SCORE_MIN = -1.0
SCORE_MAX = 1.0
CONFIDENCE_MIN = 0.0
CONFIDENCE_MAX = 1.0


class DecisionError(Exception):
    """An expected, user-facing decision-engine failure."""


class DecisionInputError(DecisionError):
    """The supplied bars violate a decision engine's input contract."""


class DecisionConfigError(DecisionError):
    """A policy or configuration value cannot describe a usable decision."""


class DecisionSignal(Enum):
    """The three directions a decision can point.

    `SELL` is the general form of V1's `EXIT`. The two are the same instruction
    to the layers downstream - reduce or close the long - and `v1.to_legacy_signal`
    converts back to `EXIT` for callers that still speak the C3 vocabulary.
    Nothing here implies a short: opening one is a scope change requiring an
    edit to docs/SPEC.md, not a signal value.
    """

    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"


class MarketRegime(Enum):
    """The coarse state of the market on the evaluated bar.

    Four states, classified deterministically from measured features rather
    than fitted. `HIGH_VOLATILITY` is checked first and outranks direction: a
    market whose range has expanded far past its own recent baseline is not
    described usefully as trending, and treating it as such is how a
    trend-follower opens its worst position.
    """

    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    UNKNOWN = "UNKNOWN"


class AssetClass(Enum):
    """Which policy family a symbol is priced and scored under."""

    CRYPTO = "crypto"
    EQUITY = "equity"


def resolve_asset_class(symbol: str) -> AssetClass:
    """Return the asset class of `symbol`, refusing anything outside both universes.

    Membership in a frozen universe, never a shape heuristic. A slash is a
    property of how this provider writes a crypto pair, not a definition of one,
    and classifying by punctuation is exactly how a slashless crypto symbol ends
    up being scored - or counted, or sized - as an equity.

    Stripped and uppercased first, matching `normalize_symbol` at both existing
    boundaries. `BTCUSD` is still not silently reinterpreted as `BTC/USD`: the
    slash is part of the symbol, not formatting.
    """
    if not isinstance(symbol, str):
        raise DecisionInputError(f"symbol must be a string, got {type(symbol).__name__}.")
    normalized = symbol.strip().upper()
    if normalized in CRYPTO_SYMBOLS:
        return AssetClass.CRYPTO
    if normalized in EQUITY_SYMBOLS:
        return AssetClass.EQUITY
    raise DecisionInputError(
        f"Unsupported symbol: {symbol!r}. Supported symbols are: "
        f"{', '.join((*CRYPTO_SYMBOLS, *EQUITY_SYMBOLS))}."
    )


def _freeze(values: Mapping[str, object]) -> Mapping[str, object]:
    """A read-only, key-sorted view of `values`.

    Sorted because a result is compared, logged, and serialized, and an audit
    record whose key order depends on insertion order is a diff waiting to
    happen. Read-only because a caller mutating the features of a result it was
    handed would falsify the record of a decision that already happened.
    """
    return MappingProxyType({key: values[key] for key in sorted(values)})


@dataclass(frozen=True)
class DecisionResult:
    """One engine's complete answer for one symbol on one completed bar.

    `timestamp` is the *start* of the newest completed bar the decision was
    made on, matching the convention every other module in this system uses for
    a bar timestamp, and matching C3's rule that a signal carries the bar whose
    close made it knowable. It is not an execution time and there is no price
    field: when and at what price a candidate could be acted on belongs to the
    layers downstream.
    """

    version: str
    symbol: str
    timestamp: pd.Timestamp
    signal: DecisionSignal
    score: float
    confidence: float
    reasons: tuple[str, ...]
    features: Mapping[str, float]
    policy: Mapping[str, object]
    regime: MarketRegime = MarketRegime.UNKNOWN

    def __post_init__(self) -> None:
        if not self.version:
            raise DecisionConfigError("version must be a non-empty identifier.")
        if not isinstance(self.signal, DecisionSignal):
            raise DecisionConfigError(f"signal must be a DecisionSignal, got {self.signal!r}.")
        if not isinstance(self.regime, MarketRegime):
            raise DecisionConfigError(f"regime must be a MarketRegime, got {self.regime!r}.")
        _require_finite_within(self.score, SCORE_MIN, SCORE_MAX, "score")
        _require_finite_within(self.confidence, CONFIDENCE_MIN, CONFIDENCE_MAX, "confidence")
        if not self.reasons:
            raise DecisionConfigError(
                "reasons must not be empty: a decision that cannot say why it was reached "
                "is not auditable, and HOLD needs a reason more than BUY does."
            )
        timestamp = pd.Timestamp(self.timestamp)
        if timestamp.tzinfo is None:
            raise DecisionConfigError(
                "timestamp must be timezone-aware; a naive bar timestamp would be read as "
                "UTC and silently misdate the decision."
            )
        object.__setattr__(self, "timestamp", timestamp.tz_convert("UTC"))
        object.__setattr__(self, "reasons", tuple(self.reasons))
        object.__setattr__(self, "features", _freeze(dict(self.features)))
        object.__setattr__(self, "policy", _freeze(dict(self.policy)))

    @property
    def is_actionable(self) -> bool:
        """Whether this result names a direction at all.

        Convenience for an integrator, not a permission: a `True` here means
        the engine produced a candidate, and says nothing about whether risk,
        account safety, or reconciliation will let it become an order.
        """
        return self.signal is not DecisionSignal.HOLD

    def to_dict(self) -> dict[str, object]:
        """A JSON-serializable record of this decision, for audit and replay.

        Timestamps become ISO-8601 UTC strings and enums become their values,
        so the output can be written to a log line or a state row without a
        custom encoder. Key order is deterministic.
        """
        return {
            "version": self.version,
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "signal": self.signal.value,
            "regime": self.regime.value,
            "score": self.score,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "features": dict(self.features),
            "policy": dict(self.policy),
        }


def _require_finite_within(value: object, lower: float, upper: float, field: str) -> None:
    """Reject a non-numeric, NaN, infinite, or out-of-range bounded output."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionConfigError(f"{field} must be a real number, got {type(value).__name__}.")
    numeric = float(value)
    if numeric != numeric:  # NaN, which compares unequal to itself.
        raise DecisionConfigError(
            f"{field} must not be NaN: an unmeasurable {field} is a HOLD with a reason, "
            "never a number that silently propagates."
        )
    if not lower <= numeric <= upper:
        raise DecisionConfigError(f"{field} must be within [{lower}, {upper}], got {numeric}.")


@runtime_checkable
class DecisionEngine(Protocol):
    """What every version - V1, V2, V3, and the anticipated V4 and V5 - provides.

    Deliberately four members. `decide` is the work; `version` identifies it in
    a stored record; `required_base_bars` lets an integrator size its fetch
    window *before* spending a provider call, rather than discovering the
    window was too short from a HOLD; `describe` exposes the configuration in
    force without running anything.

    `decide` evaluates the **newest completed bar** in `bars` and nothing else.
    Older bars are the indicator state that bar needs, not a backlog to replay:
    re-emitting a candidate from an hour ago on every restart is how a restart
    becomes a burst of stale orders. A research harness that wants every bar
    scored should call the vectorized feature layer directly rather than
    sliding this method over a window.
    """

    @property
    def version(self) -> str:
        """The engine version identifier stored with every decision."""

    @property
    def required_base_bars(self) -> int:
        """Completed base-timeframe bars needed before a direction is possible."""

    def decide(self, bars: pd.DataFrame) -> DecisionResult:
        """Evaluate the newest completed bar in `bars`."""

    def describe(self) -> Mapping[str, object]:
        """The configuration in force, as serializable values."""


def require_utc_timestamp(moment: datetime | pd.Timestamp, field: str) -> pd.Timestamp:
    """Return `moment` as a UTC `pd.Timestamp`, refusing a naive one."""
    timestamp = pd.Timestamp(moment)
    if timestamp.tzinfo is None:
        raise DecisionInputError(
            f"{field} must be timezone-aware; a naive datetime would be read as UTC and "
            "silently misdate every bar boundary."
        )
    return timestamp.tz_convert("UTC")


__all__ = [
    "CONFIDENCE_MAX",
    "CONFIDENCE_MIN",
    "CRYPTO_SYMBOLS",
    "SCORE_MAX",
    "SCORE_MIN",
    "VERSION_V1",
    "VERSION_V2",
    "VERSION_V3",
    "VERSION_V4",
    "AssetClass",
    "DecisionConfigError",
    "DecisionEngine",
    "DecisionError",
    "DecisionInputError",
    "DecisionResult",
    "DecisionSignal",
    "MarketRegime",
    "require_utc_timestamp",
    "resolve_asset_class",
]
