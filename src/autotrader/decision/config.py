"""Every number a decision engine uses, in one place, with the reason it is that number.

Nothing in the feature or scoring layers holds a threshold, a period, or a
weight. They are all here, they are all named, and each carries the argument
for its value - because a constant with no argument behind it is indistinguishable
from a constant that was fitted, and this project does not fit constants
(docs/SPEC.md section 2: the strategy is a pipeline test fixture, not an edge).

**The indicator periods are the textbook defaults, chosen for being conventional.**
RSI 14, MACD 12/26/9, ATR 14 are what every charting package ships. EMA 20/50
is what V1 has used since C3. Their merit here is that nobody chose them by
looking at this system's data, so no amount of backtesting was involved in
arriving at them and none can be read into them.

**The magnitude scales are not constants at all.** Rather than dividing each
raw factor by a tuned number, every directional feature is expressed in units
of ATR and then standardized by its *own* trailing standard deviation. The
result is unit-free, self-scaling across symbols and volatility regimes, and
carries no fitted parameter: a factor two standard deviations from flat means
the same thing on BTC/USD as on SPY.

**One window governs everything.** The standardization window is the slow EMA
period. A shorter window would rescale faster than the features it is scaling
can move; a longer one would carry a regime the indicators themselves have
already forgotten. Tying it to `ema_slow` means the engine has exactly one
binding history requirement, `required_bars`, instead of two that can drift
apart.

**What actually differs between asset classes.** Not the periods, and not the
windows - a 14-bar ATR is a 14-bar ATR. Only the *thresholds*: how far past its
own baseline a market's range has to stretch before entry is refused, how thin
participation has to get before confidence is discounted, and how much
confidence an entry needs. Crypto sustains volatility expansions that would be
a session-long anomaly in an index ETF, and 24/7 volume has no session shape to
be measured against, so the same threshold would mean two different things.
Equal-looking policy across two asset classes with different data semantics is
not neutrality; it is an unstated assumption.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType

from autotrader.decision.contract import AssetClass, DecisionConfigError, resolve_asset_class
from autotrader.decision.timeframes import (
    BASE_TIMEFRAME,
    FOUR_HOUR_TIMEFRAME,
    HOUR_TIMEFRAME,
    timeframe_for,
)
from autotrader.runtime.schedule import BAR_INTERVAL

#: Tolerance for "these weights sum to one". Floating-point addition of five
#: two-decimal literals is not exact, and refusing a set that sums to
#: 0.9999999999999999 would be pedantry rather than a check.
WEIGHT_SUM_TOLERANCE = 1e-9

#: The names of the directional factors, in report order. The composite score
#: is a weighted mean over exactly these, so this tuple, `FactorWeights`, and
#: the scoring layer are three views of one list and a test pins them together.
DIRECTIONAL_FACTORS: tuple[str, ...] = (
    "trend_ema",
    "trend_slope",
    "momentum_rsi",
    "momentum_macd",
    "momentum_return",
)


#: 15-minute bars in one full US regular session: 09:30-16:00 is six and a half
#: hours, which is twenty-six whole intervals. An early close contributes
#: fourteen (`autotrader.equity.session.MIN_REGULAR_BARS_PER_SESSION`), and that
#: is why the equity yields below carry margin rather than being exact.
REGULAR_SESSION_BASE_BARS = 26

#: How many base bars must pass, on average, to gain one *complete* bar of each
#: timeframe. Exact for a continuously traded market and an estimate for a
#: session-traded one, so it is declared per asset class rather than derived
#: from the interval alone - and a test pins every value here against what the
#: aggregator actually produces from synthetic bars.
#:
#: Crypto trades without a gap, so every sixteen base bars complete one 4-hour
#: bucket and the numbers are simply the constituent counts.
CRYPTO_BASE_BARS_PER_COMPLETE_BAR: Mapping[str, int] = MappingProxyType(
    {"15m": 1, "1h": 4, "4h": 16}
)

#: Equities do not, and the difference is not small. A regular session's
#: twenty-six base bars complete six 1-hour buckets - the two bars either side
#: of the session's edges fall in buckets that never fill - and exactly one
#: 4-hour bucket, because a 4-hour bucket needs sixteen consecutive in-session
#: bars and a six-and-a-half-hour session offers only one such run. So an hour
#: of equity context costs about five base bars rather than four, and four
#: hours of it costs a whole session.
EQUITY_BASE_BARS_PER_COMPLETE_BAR: Mapping[str, int] = MappingProxyType(
    {"15m": 1, "1h": 5, "4h": REGULAR_SESSION_BASE_BARS}
)


def _require_positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DecisionConfigError(f"{field_name} must be an int, got {type(value).__name__}.")
    if value < 1:
        raise DecisionConfigError(f"{field_name} must be at least 1, got {value}.")
    return value


def _require_within(value: object, lower: float, upper: float, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionConfigError(
            f"{field_name} must be a real number, got {type(value).__name__}."
        )
    numeric = float(value)
    if numeric != numeric:
        raise DecisionConfigError(f"{field_name} must not be NaN.")
    if not lower <= numeric <= upper:
        raise DecisionConfigError(f"{field_name} must be within [{lower}, {upper}], got {numeric}.")
    return numeric


@dataclass(frozen=True)
class IndicatorPeriods:
    """The lookbacks every feature is computed over, and the history they imply.

    `required_bars` is derived rather than written down. It is the number of
    completed bars after which *every* scored feature is defined, and it is the
    single number an integrator needs in order to size a fetch window: below it
    the engine returns HOLD with an insufficient-history reason and nothing
    else, at or above it a direction becomes possible.
    """

    ema_fast: int = 20
    ema_slow: int = 50
    slope_lookback: int = 10
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_period: int = 14
    return_lookback: int = 10

    def __post_init__(self) -> None:
        for name in (
            "ema_fast",
            "ema_slow",
            "slope_lookback",
            "rsi_period",
            "macd_fast",
            "macd_slow",
            "macd_signal",
            "atr_period",
            "return_lookback",
        ):
            _require_positive_int(getattr(self, name), name)
        if self.ema_fast >= self.ema_slow:
            raise DecisionConfigError(
                f"ema_fast ({self.ema_fast}) must be shorter than ema_slow ({self.ema_slow}); "
                "a fast average that is not faster cannot cross anything."
            )
        if self.macd_fast >= self.macd_slow:
            raise DecisionConfigError(
                f"macd_fast ({self.macd_fast}) must be shorter than macd_slow ({self.macd_slow})."
            )

    @property
    def standardization_bars(self) -> int:
        """The trailing window each directional feature is scaled by its own spread over."""
        return self.ema_slow

    @property
    def baseline_bars(self) -> int:
        """The trailing window volume and volatility are compared to their median over.

        The same window as the standardization one, deliberately. Two windows
        would be two history requirements, and there is no argument for the
        volume baseline having a different memory from the trend it confirms.
        """
        return self.standardization_bars

    @property
    def trend_warmup(self) -> int:
        """Bars until the slow-EMA slope exists: the EMA, then the slope over it."""
        return self.ema_slow + self.slope_lookback

    @property
    def macd_warmup(self) -> int:
        """Bars until the MACD histogram exists: the slow EMA, then its signal line."""
        return self.macd_slow + self.macd_signal - 1

    @property
    def rsi_warmup(self) -> int:
        """Bars until RSI exists: one bar is spent on the first difference."""
        return self.rsi_period + 1

    @property
    def atr_warmup(self) -> int:
        """Bars until ATR exists: one bar is spent on the first previous close."""
        return self.atr_period + 1

    @property
    def return_warmup(self) -> int:
        """Bars until the lookback return exists."""
        return self.return_lookback + 1

    @property
    def feature_warmup_bars(self) -> int:
        """Bars until every raw feature is defined. The slow-EMA slope governs."""
        return max(
            self.trend_warmup,
            self.macd_warmup,
            self.rsi_warmup,
            self.atr_warmup,
            self.return_warmup,
        )

    @property
    def required_bars(self) -> int:
        """Completed bars of this timeframe before a direction is possible.

        The warm-up of the slowest raw feature, plus the standardization window
        that is then measured over it, less the bar the two share. Nothing is
        rounded up for comfort: this is the exact index at which the last
        scored feature stops being NaN.
        """
        return self.feature_warmup_bars + self.standardization_bars - 1

    def describe(self) -> Mapping[str, int]:
        """The periods as serializable values, for the audit record."""
        return MappingProxyType(
            {
                "ema_fast": self.ema_fast,
                "ema_slow": self.ema_slow,
                "slope_lookback": self.slope_lookback,
                "rsi_period": self.rsi_period,
                "macd_fast": self.macd_fast,
                "macd_slow": self.macd_slow,
                "macd_signal": self.macd_signal,
                "atr_period": self.atr_period,
                "return_lookback": self.return_lookback,
                "standardization_bars": self.standardization_bars,
                "required_bars": self.required_bars,
            }
        )


@dataclass(frozen=True)
class FactorWeights:
    """How much each directional factor contributes to the composite score.

    They sum to one, which is what keeps the composite inside ``[-1, +1]``
    given that every factor is: a weighted mean of bounded terms cannot leave
    the bound. That is a structural guarantee rather than a clip applied
    afterwards, and it is why the sum is validated instead of normalized.

    Trend carries 0.40 across two factors and momentum 0.60 across three. The
    split is deliberate and not fitted: trend is measured twice from one pair of
    averages and would otherwise be counted twice at full weight, while the
    three momentum factors read genuinely different things - position within
    the recent range, convergence of two averages, and realized displacement.
    """

    trend_ema: float = 0.25
    trend_slope: float = 0.15
    momentum_rsi: float = 0.20
    momentum_macd: float = 0.20
    momentum_return: float = 0.20

    def __post_init__(self) -> None:
        for name in DIRECTIONAL_FACTORS:
            _require_within(getattr(self, name), 0.0, 1.0, name)
        total = sum(getattr(self, name) for name in DIRECTIONAL_FACTORS)
        if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
            raise DecisionConfigError(
                f"Factor weights must sum to 1.0, got {total}. The composite score is a "
                "weighted mean of bounded factors, and only a unit weight sum keeps it "
                "inside [-1, +1] by construction."
            )

    def as_mapping(self) -> Mapping[str, float]:
        """The weights keyed by factor name, in report order."""
        return MappingProxyType({name: float(getattr(self, name)) for name in DIRECTIONAL_FACTORS})


@dataclass(frozen=True)
class DecisionThresholds:
    """Where a score becomes a direction, and what discounts confidence.

    The hold band between `sell_score` and `buy_score` is explicit and
    symmetric. A continuous score with no band would emit a direction on every
    bar including a score of 0.001, which is not an opinion; the band is the
    difference between "the engine is confident enough to name a side" and "the
    engine computed a number".
    """

    buy_score: float
    sell_score: float
    min_confidence: float
    high_volatility_ratio: float
    low_participation_ratio: float

    def __post_init__(self) -> None:
        _require_within(self.buy_score, 0.0, 1.0, "buy_score")
        _require_within(self.sell_score, -1.0, 0.0, "sell_score")
        _require_within(self.min_confidence, 0.0, 1.0, "min_confidence")
        if self.sell_score >= self.buy_score:
            raise DecisionConfigError(
                f"sell_score ({self.sell_score}) must be below buy_score ({self.buy_score}); "
                "an inverted or empty hold band would make one score mean both directions."
            )
        if self.high_volatility_ratio <= 1.0:
            raise DecisionConfigError(
                f"high_volatility_ratio must exceed 1.0, got {self.high_volatility_ratio}. "
                "It is a multiple of the market's own median volatility, so a value at or "
                "below 1.0 would classify a typical bar as an expansion."
            )
        _require_within(self.low_participation_ratio, 0.0, 1.0, "low_participation_ratio")

    def describe(self) -> Mapping[str, float]:
        """The thresholds as serializable values, for the audit record."""
        return MappingProxyType(
            {
                "buy_score": self.buy_score,
                "sell_score": self.sell_score,
                "min_confidence": self.min_confidence,
                "high_volatility_ratio": self.high_volatility_ratio,
                "low_participation_ratio": self.low_participation_ratio,
            }
        )


@dataclass(frozen=True)
class MultiTimeframeGates:
    """The per-timeframe bars a V3 entry has to clear, tactical to contextual.

    Descending, and that ordering is the whole idea. The 15-minute timeframe
    has to actually point somewhere - it is what triggers - while the hourly and
    four-hourly timeframes are asked only to *not disagree*. Requiring a full
    trigger-strength score on the 4-hour context would mean entering only once
    a multi-day move was already obvious on every scale, which is a different
    strategy and a later one.
    """

    trigger_min: float = 0.25
    confirm_min: float = 0.15
    context_min: float = 0.10

    def __post_init__(self) -> None:
        _require_within(self.trigger_min, 0.0, 1.0, "trigger_min")
        _require_within(self.confirm_min, 0.0, 1.0, "confirm_min")
        _require_within(self.context_min, 0.0, 1.0, "context_min")
        if not self.trigger_min >= self.confirm_min >= self.context_min:
            raise DecisionConfigError(
                "Multi-timeframe gates must not increase with timeframe: "
                f"trigger_min ({self.trigger_min}) >= confirm_min ({self.confirm_min}) "
                f">= context_min ({self.context_min}). Higher timeframes confirm a "
                "direction; they do not have to lead it."
            )

    def describe(self) -> Mapping[str, float]:
        """The gates as serializable values, for the audit record."""
        return MappingProxyType(
            {
                "trigger_min": self.trigger_min,
                "confirm_min": self.confirm_min,
                "context_min": self.context_min,
            }
        )


@dataclass(frozen=True)
class TimeframePolicy:
    """The periods and factor weights one timeframe is scored under.

    Per timeframe so a deployment *can* run cheaper indicators on the 4-hour
    context than on the 15-minute trigger. The shipped policies deliberately do
    not: identical periods on every timeframe is the choice that involves no
    per-timeframe tuning, and the cost of that honesty is history, which
    `required_bars` states plainly rather than hides.
    """

    label: str
    periods: IndicatorPeriods = field(default_factory=IndicatorPeriods)
    weights: FactorWeights = field(default_factory=FactorWeights)

    def __post_init__(self) -> None:
        timeframe_for(self.label)


def _default_timeframe_policies() -> Mapping[str, TimeframePolicy]:
    return MappingProxyType(
        {
            spec.label: TimeframePolicy(label=spec.label)
            for spec in (BASE_TIMEFRAME, HOUR_TIMEFRAME, FOUR_HOUR_TIMEFRAME)
        }
    )


def _default_timeframe_weights() -> Mapping[str, float]:
    """How V3 blends the three timeframe scores into one composite.

    Weighted towards context rather than towards the trigger. The 15-minute
    score is the noisiest of the three and the one most likely to be a single
    bar's accident; the 4-hour score is the slowest to change and the least
    likely to be wrong about which way the market has actually been going.
    """
    return MappingProxyType({"15m": 0.25, "1h": 0.35, "4h": 0.40})


@dataclass(frozen=True)
class AssetClassPolicy:
    """The complete configuration one asset class is decided under."""

    asset_class: AssetClass
    name: str
    thresholds: DecisionThresholds
    base_bars_per_complete_bar: Mapping[str, int]
    gates: MultiTimeframeGates = field(default_factory=MultiTimeframeGates)
    timeframes: Mapping[str, TimeframePolicy] = field(default_factory=_default_timeframe_policies)
    timeframe_weights: Mapping[str, float] = field(default_factory=_default_timeframe_weights)

    def __post_init__(self) -> None:
        if not self.timeframes:
            raise DecisionConfigError(f"Policy {self.name!r} configures no timeframe.")
        for label in self.timeframes:
            timeframe_for(label)
        total = sum(self.timeframe_weights.values())
        if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
            raise DecisionConfigError(
                f"Timeframe weights must sum to 1.0, got {total}. The V3 composite is a "
                "weighted mean of bounded per-timeframe scores, and only a unit weight sum "
                "keeps it inside [-1, +1] by construction."
            )
        for label, weight in self.timeframe_weights.items():
            timeframe_for(label)
            _require_within(weight, 0.0, 1.0, f"timeframe_weights[{label!r}]")
            if label not in self.timeframes:
                raise DecisionConfigError(
                    f"Policy {self.name!r} weights timeframe {label!r} but does not configure "
                    "it. A weighted timeframe with no periods cannot be scored."
                )
        for label in self.timeframes:
            if label not in self.base_bars_per_complete_bar:
                raise DecisionConfigError(
                    f"Policy {self.name!r} configures timeframe {label!r} but states no "
                    "base-bar yield for it, so a fetch window for it cannot be sized."
                )
            _require_positive_int(
                self.base_bars_per_complete_bar[label],
                f"base_bars_per_complete_bar[{label!r}]",
            )
        object.__setattr__(
            self,
            "base_bars_per_complete_bar",
            MappingProxyType(dict(self.base_bars_per_complete_bar)),
        )
        object.__setattr__(self, "timeframes", MappingProxyType(dict(self.timeframes)))
        object.__setattr__(
            self, "timeframe_weights", MappingProxyType(dict(self.timeframe_weights))
        )

    def timeframe(self, label: str) -> TimeframePolicy:
        """The policy for one timeframe, refusing one this policy does not configure."""
        try:
            return self.timeframes[label]
        except KeyError:
            raise DecisionConfigError(
                f"Policy {self.name!r} does not configure timeframe {label!r}. Configured "
                f"timeframes are: {', '.join(self.timeframes)}."
            ) from None

    def required_base_bars(
        self,
        labels: tuple[str, ...] = (BASE_TIMEFRAME.label,),
        *,
        base_interval: timedelta = BAR_INTERVAL,
    ) -> int:
        """Base-timeframe bars needed to score every timeframe in `labels`.

        The number an integrator sizes a fetch window from, and the reason this
        is asset-class state rather than interval arithmetic. Sixteen base bars
        go into one 4-hour bar, so a continuously traded pair needs sixteen
        hundred base bars to score a 4-hour indicator that wants a hundred of
        its own. A session-traded symbol needs far more: a regular session
        completes exactly one 4-hour bucket, so the same indicator costs a
        hundred *sessions*. That is the true price of the context, stated up
        front rather than discovered as a permanent HOLD.
        """
        del base_interval  # The yields below already express the base-bar cost.
        return max(
            self.timeframe(label).periods.required_bars * self.base_bars_per_complete_bar[label]
            for label in labels
        )

    def describe(self) -> Mapping[str, object]:
        """The whole policy as serializable values, for the audit record."""
        return MappingProxyType(
            {
                "policy_name": self.name,
                "asset_class": self.asset_class.value,
                "thresholds": dict(self.thresholds.describe()),
                "gates": dict(self.gates.describe()),
                "timeframe_weights": dict(self.timeframe_weights),
                "base_bars_per_complete_bar": dict(self.base_bars_per_complete_bar),
                # Keyed "timeframe_policies" rather than "timeframes" because an
                # engine's metadata already carries a "timeframes" list of the
                # labels it reads, and a collision there would silently replace
                # that list with this mapping.
                "timeframe_policies": {
                    label: {
                        "periods": dict(policy.periods.describe()),
                        "weights": dict(policy.weights.as_mapping()),
                    }
                    for label, policy in self.timeframes.items()
                },
            }
        )


#: Crypto policy. Wider volatility tolerance and a lower participation floor.
#:
#: A pair whose range triples against its own recent median is having an
#: ordinary week, so the expansion gate sits at 2.5x rather than somewhere an
#: ordinary week would trip it. Volume has no session shape to compare against -
#: 03:00 UTC on a Sunday is a real trading hour - so a bar at half the rolling
#: median is thin but not anomalous, and the floor sits there.
CRYPTO_POLICY = AssetClassPolicy(
    asset_class=AssetClass.CRYPTO,
    name="crypto-v2-default",
    thresholds=DecisionThresholds(
        buy_score=0.25,
        sell_score=-0.25,
        min_confidence=0.35,
        high_volatility_ratio=2.50,
        low_participation_ratio=0.50,
    ),
    base_bars_per_complete_bar=CRYPTO_BASE_BARS_PER_COMPLETE_BAR,
)

#: Equity policy. Tighter volatility tolerance, higher participation floor,
#: more confidence required.
#:
#: Regular-session equity ranges expand less and revert faster, so a 2.0x
#: expansion is already unusual and is treated as one. Session volume has a
#: pronounced shape - heavy at the open and the close, thin in the middle - so
#: a rolling median mixes both and the floor is set higher to keep a midday
#: lull from reading as ordinary participation. The confidence floor is higher
#: because an equity position, unlike a crypto one, cannot be adjusted overnight.
EQUITY_POLICY = AssetClassPolicy(
    asset_class=AssetClass.EQUITY,
    name="equity-v2-default",
    thresholds=DecisionThresholds(
        buy_score=0.25,
        sell_score=-0.25,
        min_confidence=0.40,
        high_volatility_ratio=2.00,
        low_participation_ratio=0.60,
    ),
    base_bars_per_complete_bar=EQUITY_BASE_BARS_PER_COMPLETE_BAR,
)

POLICIES: Mapping[AssetClass, AssetClassPolicy] = MappingProxyType(
    {
        AssetClass.CRYPTO: CRYPTO_POLICY,
        AssetClass.EQUITY: EQUITY_POLICY,
    }
)


def policy_for(asset_class: AssetClass) -> AssetClassPolicy:
    """The shipped policy for one asset class."""
    try:
        return POLICIES[asset_class]
    except KeyError:
        raise DecisionConfigError(f"No policy is configured for {asset_class!r}.") from None


def policy_for_symbol(symbol: str) -> AssetClassPolicy:
    """The shipped policy for `symbol`, refusing a symbol outside both universes."""
    return policy_for(resolve_asset_class(symbol))


__all__ = [
    "CRYPTO_BASE_BARS_PER_COMPLETE_BAR",
    "CRYPTO_POLICY",
    "DIRECTIONAL_FACTORS",
    "EQUITY_BASE_BARS_PER_COMPLETE_BAR",
    "EQUITY_POLICY",
    "POLICIES",
    "REGULAR_SESSION_BASE_BARS",
    "WEIGHT_SUM_TOLERANCE",
    "AssetClassPolicy",
    "DecisionThresholds",
    "FactorWeights",
    "IndicatorPeriods",
    "MultiTimeframeGates",
    "TimeframePolicy",
    "policy_for",
    "policy_for_symbol",
]
