"""Decision Engine tests: V1 behind the shared contract.

The point of the adapter is that it changes nothing, so these tests are mostly
equalities against `autotrader.strategies.ema_cross` itself. If the adapter ever
starts computing its own crossovers, `test_the_adapter_agrees_with_the_strategy_on_every_bar`
is what notices - it sweeps a bar at a time across several deliberately
crossover-rich series and demands the same answer at every one of them.

The runtime's own rule is restated here too. `_latest_bar_signal` in both
runtimes acts only on a crossover landing on the newest completed bar, and the
adapter has to agree with that or a stored V1 decision would not describe what
the running system would have done.
"""

from __future__ import annotations

import functools
import math
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from autotrader.decision.contract import (
    VERSION_V1,
    DecisionInputError,
    DecisionSignal,
    MarketRegime,
)
from autotrader.decision.v1 import (
    REASON_NO_CROSSOVER,
    REQUIRED_BARS,
    EmaCrossV1Engine,
    to_legacy_signal,
)
from autotrader.strategies.ema_cross import (
    BUY_REASON,
    EXIT_REASON,
    SLOW_PERIOD,
    Signal,
    SignalType,
    add_ema_columns,
    generate_ema_cross_signals,
)

FIRST_BAR = datetime(2025, 1, 1, tzinfo=UTC)
STEP = timedelta(minutes=15)
ENGINE = EmaCrossV1Engine()


def make_bars(closes: list[float], symbol: str = "BTC/USD") -> pd.DataFrame:
    prices = [float(close) for close in closes]
    return pd.DataFrame(
        {
            "timestamp": [FIRST_BAR + STEP * index for index in range(len(prices))],
            "symbol": [symbol] * len(prices),
            "open": prices,
            "high": [price + 0.5 for price in prices],
            "low": [price - 0.5 for price in prices],
            "close": prices,
            "volume": [100.0] * len(prices),
            "trade_count": [10] * len(prices),
            "vwap": prices,
        }
    )


def crossing_closes(count: int, period: float) -> list[float]:
    """An oscillation slow enough for EMA 20 and EMA 50 to cross repeatedly."""
    return [100.0 + 12.0 * math.sin(index / period) for index in range(count)]


@functools.lru_cache(maxsize=4)
def sweep(count: int = 320, period: float = 17.0) -> tuple[tuple[int, object], ...]:
    """Every decision over growing prefixes of one crossover-rich series.

    Cached because several tests below want the same sweep and the crossover
    scan is quadratic in the prefix length: computing it once is the difference
    between this file taking seconds and taking most of a minute.
    """
    closes = crossing_closes(count, period)
    return tuple(
        (end, ENGINE.decide(make_bars(closes[:end]))) for end in range(REQUIRED_BARS, count + 1)
    )


def expected_signal(bars: pd.DataFrame) -> Signal | None:
    """The runtime's rule, restated independently of the adapter.

    Mirrors `_latest_bar_signal` in both `autotrader.runtime.runner` and
    `autotrader.equity.runtime`: the newest crossover counts only if it landed
    on the newest completed bar.
    """
    signals = generate_ema_cross_signals(bars)
    if not signals:
        return None
    newest = signals[-1]
    if pd.Timestamp(newest.timestamp) != pd.Timestamp(bars["timestamp"].iloc[-1]):
        return None
    return newest


# --------------------------------------------------------------------------
# Agreement with the strategy
# --------------------------------------------------------------------------


@pytest.mark.parametrize("period", [9.0, 17.0, 31.0])
def test_the_adapter_agrees_with_the_strategy_on_every_bar(period: float) -> None:
    """CRITICAL. Swept a bar at a time, so a single disagreement fails this."""
    crossovers = 0

    for end, result in sweep(240, period):
        expected = expected_signal(make_bars(crossing_closes(240, period)[:end]))
        assert to_legacy_signal(result) == expected, f"disagreement at bar {end}"
        crossovers += expected is not None

    assert crossovers >= 2, "the fixture produced too few crossovers to be a real test"


def test_a_crossover_on_an_older_bar_is_not_re_emitted() -> None:
    """A restart must not replay a crossover that was already acted on or missed."""
    results = dict(sweep())
    with_crossover = next(end for end, result in sweep() if result.is_actionable)

    on_the_bar = results[with_crossover]
    one_bar_later = results[with_crossover + 1]

    assert on_the_bar.is_actionable
    assert one_bar_later.signal is DecisionSignal.HOLD
    assert one_bar_later.reasons == (REASON_NO_CROSSOVER,)


def test_the_original_reason_tokens_are_carried_through_unchanged() -> None:
    """A stored V1 decision still says exactly what C3 said."""
    seen = {result.reasons[0] for _, result in sweep() if result.is_actionable}

    assert seen == {BUY_REASON, EXIT_REASON}


def test_the_reported_averages_are_the_strategys_own() -> None:
    bars = make_bars(crossing_closes(200, 17.0))
    result = ENGINE.decide(bars)
    enriched = add_ema_columns(bars).iloc[-1]

    assert result.features["ema_fast"] == pytest.approx(float(enriched["ema_20"]))
    assert result.features["ema_slow"] == pytest.approx(float(enriched["ema_50"]))


# --------------------------------------------------------------------------
# The mapping into the shared contract
# --------------------------------------------------------------------------


def test_exit_becomes_sell_and_converts_back_to_exit() -> None:
    """The same instruction under the general name, and the way back is exact."""
    exits = [result for _, result in sweep() if result.reasons[0] == EXIT_REASON]
    assert exits, "the fixture produced no exit crossover"

    for result in exits:
        assert result.signal is DecisionSignal.SELL
        legacy = to_legacy_signal(result)
        assert legacy is not None
        assert legacy.type is SignalType.EXIT
        assert legacy.reason == EXIT_REASON
        assert legacy.timestamp == result.timestamp
        assert legacy.symbol == result.symbol


def test_hold_converts_to_no_legacy_signal_at_all() -> None:
    """C3 has no way to say "no action" other than emitting nothing."""
    result = ENGINE.decide(make_bars([100.0] * 200))

    assert result.signal is DecisionSignal.HOLD
    assert to_legacy_signal(result) is None


@pytest.mark.parametrize(
    ("signal", "score", "confidence"),
    [(DecisionSignal.BUY, 1.0, 1.0), (DecisionSignal.SELL, -1.0, 1.0)],
)
def test_a_crossover_scores_at_the_bound_because_v1_has_no_gradation(
    signal: DecisionSignal, score: float, confidence: float
) -> None:
    matching = [result for _, result in sweep() if result.signal is signal]
    assert matching

    for result in matching:
        assert result.score == score
        assert result.confidence == confidence


def test_a_hold_scores_zero_with_no_confidence() -> None:
    result = ENGINE.decide(make_bars([100.0] * 200))

    assert result.score == 0.0
    assert result.confidence == 0.0


def test_v1_classifies_no_regime_because_it_measures_none() -> None:
    """Reporting a regime here would put a judgement in V1 that V1 never made."""
    for closes in ([100.0] * 200, crossing_closes(200, 17.0)):
        assert ENGINE.decide(make_bars(closes)).regime is MarketRegime.UNKNOWN


# --------------------------------------------------------------------------
# The contract surface
# --------------------------------------------------------------------------


def test_the_history_requirement_is_the_arithmetic_floor() -> None:
    assert REQUIRED_BARS == SLOW_PERIOD + 1
    assert ENGINE.required_base_bars == REQUIRED_BARS


def test_every_decision_is_stamped_with_the_v1_version() -> None:
    result = ENGINE.decide(make_bars(crossing_closes(200, 17.0)))

    assert result.version == VERSION_V1
    assert result.policy["engine_version"] == VERSION_V1
    assert result.policy["periods"] == {"ema_fast": 20, "ema_slow": 50}


def test_the_decision_carries_the_newest_bar_timestamp() -> None:
    bars = make_bars(crossing_closes(200, 17.0))

    assert ENGINE.decide(bars).timestamp == bars["timestamp"].iloc[-1]


def test_deciding_does_not_modify_the_supplied_frame() -> None:
    bars = make_bars(crossing_closes(200, 17.0))
    before = bars.copy(deep=True)

    ENGINE.decide(bars)

    assert bars.equals(before)


def test_the_same_bars_decide_identically_on_every_call() -> None:
    bars = make_bars(crossing_closes(200, 17.0))

    assert ENGINE.decide(bars).to_dict() == ENGINE.decide(bars).to_dict()


def test_the_adapter_applies_the_shared_bar_contract() -> None:
    """Stricter than C3 about duplicates, and only ever about malformed data."""
    bars = make_bars(crossing_closes(120, 17.0))
    duplicated = pd.concat([bars, bars.iloc[[-1]]], ignore_index=True)

    with pytest.raises(DecisionInputError, match="must not repeat a timestamp"):
        ENGINE.decide(duplicated)


def test_an_equity_symbol_needs_no_policy_because_v1_has_none() -> None:
    """V1's periods are fixed, so there is nothing for an asset class to vary."""
    result = ENGINE.decide(make_bars(crossing_closes(200, 17.0), symbol="SPY"))

    assert result.symbol == "SPY"
    assert result.version == VERSION_V1
