"""Decision Engine tests: higher-timeframe aggregation and the alignment rule.

Two properties carry V3 and both are tested here directly.

*Buckets are anchored to the UTC epoch, not to the window.* A 4-hour candle
must be the same candle whether it was built from two hundred bars or two
thousand, or a replay cannot reproduce a live decision.

*A bucket is complete or it does not exist.* Counting constituents is the only
completeness evidence available, and it is what keeps a session-traded symbol
from ever growing a candle that spans an overnight gap, a weekend, or a
holiday - without this module knowing that any of those things exist.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from autotrader.decision.config import (
    CRYPTO_BASE_BARS_PER_COMPLETE_BAR,
    EQUITY_BASE_BARS_PER_COMPLETE_BAR,
    REGULAR_SESSION_BASE_BARS,
)
from autotrader.decision.contract import DecisionConfigError, DecisionInputError
from autotrader.decision.timeframes import (
    BASE_TIMEFRAME,
    FOUR_HOUR_TIMEFRAME,
    HOUR_TIMEFRAME,
    V3_TIMEFRAMES,
    TimeframeSpec,
    aggregate_bars,
    align_timeframes,
    timeframe_for,
    usable_history,
)

STEP = timedelta(minutes=15)
EPOCH_START = datetime(2025, 1, 1, tzinfo=UTC)

#: A January regular session in UTC. 09:30-16:00 Eastern is 14:30-21:00 UTC
#: while the United States is on standard time, so the session's twenty-six
#: 15-minute bars start at 14:30 and the last one starts at 20:45.
SESSION_OPEN_UTC = timedelta(hours=14, minutes=30)


def bars_from(timestamps: list[datetime], symbol: str = "BTC/USD") -> pd.DataFrame:
    """A canonical frame over exactly `timestamps`, with distinguishable prices."""
    prices = [100.0 + index for index in range(len(timestamps))]
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "symbol": [symbol] * len(timestamps),
            "open": prices,
            "high": [price + 2.0 for price in prices],
            "low": [price - 3.0 for price in prices],
            "close": [price + 0.5 for price in prices],
            "volume": [10.0 + index for index in range(len(timestamps))],
            "trade_count": [2 + index for index in range(len(timestamps))],
            "vwap": [price + 0.25 for price in prices],
        }
    )


def continuous_bars(count: int, start: datetime = EPOCH_START) -> pd.DataFrame:
    """Crypto-shaped bars: one every fifteen minutes, forever, no gaps."""
    return bars_from([start + STEP * index for index in range(count)])


def session_timestamps(sessions: int, first: date = date(2025, 1, 6)) -> list[datetime]:
    """Regular-session 15-minute bar starts for `sessions` consecutive weekdays."""
    stamps: list[datetime] = []
    day = first
    remaining = sessions
    while remaining:
        if day.weekday() < 5:
            midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
            open_utc = midnight + SESSION_OPEN_UTC
            stamps.extend(open_utc + STEP * index for index in range(REGULAR_SESSION_BASE_BARS))
            remaining -= 1
        day += timedelta(days=1)
    return stamps


# --------------------------------------------------------------------------
# Specs
# --------------------------------------------------------------------------


def test_the_three_timeframes_are_whole_multiples_of_the_base() -> None:
    assert BASE_TIMEFRAME.constituents() == 1
    assert HOUR_TIMEFRAME.constituents() == 4
    assert FOUR_HOUR_TIMEFRAME.constituents() == 16


def test_v3_reports_its_timeframes_tactical_first() -> None:
    assert [spec.label for spec in V3_TIMEFRAMES] == ["15m", "1h", "4h"]


def test_a_timeframe_that_is_not_a_whole_multiple_of_the_base_is_refused() -> None:
    """A bucket that cannot be filled by whole base bars can never be shown complete."""
    ragged = TimeframeSpec(label="1h", interval=timedelta(minutes=22))

    with pytest.raises(DecisionConfigError, match="whole multiple"):
        ragged.constituents()


def test_an_unknown_timeframe_label_is_refused() -> None:
    with pytest.raises(DecisionConfigError, match="Unknown timeframe"):
        timeframe_for("30m")


def test_reason_tokens_are_the_uppercased_labels() -> None:
    assert [spec.reason_token for spec in V3_TIMEFRAMES] == ["15M", "1H", "4H"]


# --------------------------------------------------------------------------
# Aggregation arithmetic
# --------------------------------------------------------------------------


def test_an_hour_is_built_from_its_four_constituent_bars() -> None:
    bars = continuous_bars(8)
    hourly = aggregate_bars(bars, HOUR_TIMEFRAME)

    assert len(hourly) == 2
    first = hourly.iloc[0]
    source = bars.iloc[:4]
    assert first["timestamp"] == bars["timestamp"].iloc[0]
    assert first["open"] == source["open"].iloc[0]
    assert first["high"] == source["high"].max()
    assert first["low"] == source["low"].min()
    assert first["close"] == source["close"].iloc[-1]
    assert first["volume"] == source["volume"].sum()
    assert first["trade_count"] == source["trade_count"].sum()


def test_the_aggregated_close_is_the_last_constituent_close() -> None:
    """Derived bars cannot disagree with the bars they are derived from."""
    bars = continuous_bars(64)
    four_hourly = aggregate_bars(bars, FOUR_HOUR_TIMEFRAME)

    for _, row in four_hourly.iterrows():
        window = bars[
            (bars["timestamp"] >= row["timestamp"])
            & (bars["timestamp"] < row["timestamp"] + pd.Timedelta(hours=4))
        ]
        assert row["close"] == window["close"].iloc[-1]
        assert row["open"] == window["open"].iloc[0]


def test_vwap_is_volume_weighted_rather_than_a_plain_mean() -> None:
    """A thin bar must not count as much as a heavy one."""
    bars = continuous_bars(4)
    bars.loc[3, "volume"] = 1000.0
    hourly = aggregate_bars(bars, HOUR_TIMEFRAME)

    expected = (bars["vwap"] * bars["volume"]).sum() / bars["volume"].sum()
    assert float(hourly["vwap"].iloc[0]) == pytest.approx(expected)
    assert float(hourly["vwap"].iloc[0]) != pytest.approx(bars["vwap"].mean())


def test_a_zero_volume_bucket_falls_back_to_the_unweighted_mean() -> None:
    bars = continuous_bars(4)
    bars["volume"] = 0.0
    hourly = aggregate_bars(bars, HOUR_TIMEFRAME)

    assert float(hourly["vwap"].iloc[0]) == pytest.approx(bars["vwap"].mean())


def test_aggregating_the_base_timeframe_onto_itself_is_the_identity() -> None:
    bars = continuous_bars(40)
    same = aggregate_bars(bars, BASE_TIMEFRAME)

    assert same["timestamp"].tolist() == bars["timestamp"].tolist()
    assert same["close"].tolist() == bars["close"].tolist()
    assert same["volume"].tolist() == bars["volume"].tolist()


def test_aggregation_does_not_modify_the_supplied_frame() -> None:
    bars = continuous_bars(40)
    before = bars.copy(deep=True)

    aggregate_bars(bars, FOUR_HOUR_TIMEFRAME)

    assert bars.equals(before)


# --------------------------------------------------------------------------
# Completeness
# --------------------------------------------------------------------------


def test_a_partly_observed_bucket_is_dropped_rather_than_emitted_short() -> None:
    bars = continuous_bars(6)  # One whole hour, then two bars of the next.
    hourly = aggregate_bars(bars, HOUR_TIMEFRAME)

    assert len(hourly) == 1
    assert hourly["timestamp"].iloc[0] == EPOCH_START


def test_a_bucket_missing_an_interior_bar_is_dropped() -> None:
    """A gap inside an hour makes that hour unknowable, not approximable."""
    bars = continuous_bars(8).drop(index=2).reset_index(drop=True)
    hourly = aggregate_bars(bars, HOUR_TIMEFRAME)

    assert hourly["timestamp"].tolist() == [EPOCH_START + timedelta(hours=1)]


def test_a_weekend_gap_cannot_produce_a_bucket_spanning_it() -> None:
    """No calendar was consulted; the count alone refuses the fabrication."""
    friday = [datetime(2025, 1, 3, 20, 0, tzinfo=UTC) + STEP * index for index in range(2)]
    monday = [datetime(2025, 1, 6, 14, 30, tzinfo=UTC) + STEP * index for index in range(2)]
    hourly = aggregate_bars(bars_from(friday + monday), HOUR_TIMEFRAME)

    assert hourly.empty


# --------------------------------------------------------------------------
# Epoch anchoring
# --------------------------------------------------------------------------


@pytest.mark.parametrize("offset", [1, 3, 7, 13, 17])
def test_a_bucket_is_the_same_bucket_whatever_window_it_was_built_from(offset: int) -> None:
    """CRITICAL. Otherwise a replay produces different candles from the live run."""
    bars = continuous_bars(400)
    whole = aggregate_bars(bars, FOUR_HOUR_TIMEFRAME)
    partial = aggregate_bars(bars.iloc[offset:].reset_index(drop=True), FOUR_HOUR_TIMEFRAME)

    shared = set(whole["timestamp"]) & set(partial["timestamp"])
    assert shared
    for stamp in sorted(shared):
        left = whole.loc[whole["timestamp"] == stamp].reset_index(drop=True)
        right = partial.loc[partial["timestamp"] == stamp].reset_index(drop=True)
        assert left.equals(right), stamp


def test_bucket_starts_land_on_the_interval_grid_anchored_at_midnight_utc() -> None:
    four_hourly = aggregate_bars(continuous_bars(200), FOUR_HOUR_TIMEFRAME)

    for stamp in four_hourly["timestamp"]:
        assert stamp.hour % 4 == 0
        assert stamp.minute == 0 and stamp.second == 0


# --------------------------------------------------------------------------
# Alignment
# --------------------------------------------------------------------------


def test_a_higher_timeframe_bar_is_unusable_until_it_has_fully_closed() -> None:
    """CRITICAL. The one inequality that keeps V3 off an unfinished candle."""
    bars = continuous_bars(64)
    four_hourly = aggregate_bars(bars, FOUR_HOUR_TIMEFRAME)

    # The base bar starting 15:30 closes at 15:45; the 12:00 bucket closes at
    # 16:00 and is therefore fifteen minutes short of being knowable.
    usable = usable_history(
        four_hourly,
        FOUR_HOUR_TIMEFRAME,
        base_bar_start=EPOCH_START + timedelta(hours=15, minutes=30),
    )
    assert usable["timestamp"].iloc[-1] == EPOCH_START + timedelta(hours=8)

    # One bar later it has closed, at exactly the same instant, and is usable.
    usable = usable_history(
        four_hourly,
        FOUR_HOUR_TIMEFRAME,
        base_bar_start=EPOCH_START + timedelta(hours=15, minutes=45),
    )
    assert usable["timestamp"].iloc[-1] == EPOCH_START + timedelta(hours=12)


def test_alignment_admits_a_bar_that_closes_exactly_with_the_base_bar() -> None:
    """The inequality is inclusive: closing together means it is knowable."""
    bars = continuous_bars(16)
    hourly = aggregate_bars(bars, HOUR_TIMEFRAME)
    usable = usable_history(
        hourly, HOUR_TIMEFRAME, base_bar_start=EPOCH_START + timedelta(minutes=45)
    )

    assert usable["timestamp"].tolist() == [EPOCH_START]


def test_align_timeframes_trims_every_timeframe_to_the_same_instant() -> None:
    bars = continuous_bars(200)
    aligned = align_timeframes(bars)
    base_close = bars["timestamp"].iloc[-1] + pd.Timedelta(minutes=15)

    for spec in V3_TIMEFRAMES:
        frame = aligned[spec.label]
        assert not frame.empty
        assert frame["timestamp"].iloc[-1] + pd.Timedelta(spec.interval) <= base_close


def test_align_timeframes_accepts_an_earlier_anchor_for_replay() -> None:
    """Scoring an old bar must see only what that bar could have seen."""
    bars = continuous_bars(200)
    anchor = bars["timestamp"].iloc[100]
    aligned = align_timeframes(bars, base_bar_start=anchor)

    for spec in V3_TIMEFRAMES:
        frame = aligned[spec.label]
        latest_close = frame["timestamp"].iloc[-1] + pd.Timedelta(spec.interval)
        assert latest_close <= anchor + pd.Timedelta(minutes=15)


def test_a_naive_anchor_is_refused() -> None:
    with pytest.raises(DecisionInputError, match="timezone-aware"):
        align_timeframes(continuous_bars(64), base_bar_start=datetime(2025, 1, 1, 8, 0))


# --------------------------------------------------------------------------
# Equity sessions, without a calendar
# --------------------------------------------------------------------------


def test_a_regular_session_yields_six_complete_hours_and_one_complete_four_hour_bar() -> None:
    """The count rule alone produces the right answer for a session-traded symbol."""
    bars = bars_from(session_timestamps(1), symbol="SPY")

    assert len(bars) == REGULAR_SESSION_BASE_BARS
    assert len(aggregate_bars(bars, HOUR_TIMEFRAME)) == 6
    assert len(aggregate_bars(bars, FOUR_HOUR_TIMEFRAME)) == 1


def test_no_equity_bucket_spans_two_sessions() -> None:
    """An overnight gap inside a candle would be a fabricated bar."""
    bars = bars_from(session_timestamps(5), symbol="SPY")

    for spec in (HOUR_TIMEFRAME, FOUR_HOUR_TIMEFRAME):
        for _, row in aggregate_bars(bars, spec).iterrows():
            start = row["timestamp"]
            end = start + pd.Timedelta(spec.interval)
            covered = bars[(bars["timestamp"] >= start) & (bars["timestamp"] < end)]
            assert len(covered) == spec.constituents()
            assert covered["timestamp"].iloc[0].date() == covered["timestamp"].iloc[-1].date()


def test_the_declared_equity_bar_yields_match_what_aggregation_actually_produces() -> None:
    """The sizing constants in the policy are pinned to observed behaviour."""
    sessions = 20
    bars = bars_from(session_timestamps(sessions), symbol="SPY")
    base_bars = len(bars)

    for spec in V3_TIMEFRAMES:
        produced = len(aggregate_bars(bars, spec))
        declared = EQUITY_BASE_BARS_PER_COMPLETE_BAR[spec.label]
        assert produced >= 1, spec.label
        # The declared cost must not understate reality, or a caller sizing a
        # window from it would sit in a permanent insufficient-history HOLD.
        assert declared >= base_bars / produced - 1e-9, spec.label


def test_the_declared_crypto_bar_yields_are_exact() -> None:
    bars = continuous_bars(1600)

    for spec in V3_TIMEFRAMES:
        produced = len(aggregate_bars(bars, spec))
        declared = CRYPTO_BASE_BARS_PER_COMPLETE_BAR[spec.label]
        assert declared == spec.constituents()
        assert produced == len(bars) // spec.constituents()


def test_an_early_close_session_completes_no_four_hour_bucket() -> None:
    """A half day is fourteen bars, and sixteen consecutive ones do not exist in it."""
    midnight = datetime(2025, 11, 28, tzinfo=UTC)
    open_utc = midnight + SESSION_OPEN_UTC
    half_day = [open_utc + STEP * index for index in range(14)]

    assert aggregate_bars(bars_from(half_day, symbol="SPY"), FOUR_HOUR_TIMEFRAME).empty


# --------------------------------------------------------------------------
# Input contract
# --------------------------------------------------------------------------


def test_a_duplicate_bar_cannot_make_a_bucket_look_complete() -> None:
    """The reason the bar contract is stricter here than C3's was."""
    three = continuous_bars(3)
    duplicated = pd.concat([three, three.iloc[[2]]], ignore_index=True)

    with pytest.raises(DecisionInputError, match="must not repeat a timestamp"):
        aggregate_bars(duplicated, HOUR_TIMEFRAME)


def test_unsorted_bars_are_refused_rather_than_sorted() -> None:
    bars = continuous_bars(8).iloc[::-1].reset_index(drop=True)

    with pytest.raises(DecisionInputError, match="ordered ascending"):
        aggregate_bars(bars, HOUR_TIMEFRAME)
