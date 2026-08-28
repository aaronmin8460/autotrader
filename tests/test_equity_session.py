"""Equity V0.2: the market-session contract.

The session rules are the whole reason equities need a runtime of their own, so
they are tested here as pure arithmetic - no network, no clock, no waiting. The
literal sessions below are the real ones Alpaca reports: an ordinary
09:30-16:00 day, the 13:00 early close after Thanksgiving, the holiday that is
simply absent from the calendar, and a winter day on the other side of a
daylight-saving change.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from autotrader.equity import (
    EQUITY_SYMBOLS,
    EQUITY_TIMEFRAME,
    EQUITY_UNIVERSE_SIZE,
    MARKET_TIMEZONE,
    MARKET_TIMEZONE_NAME,
    EquityError,
    normalize_symbol,
    normalize_timeframe,
)
from autotrader.equity.session import (
    MIN_REGULAR_BARS_PER_SESSION,
    MarketSession,
    SessionError,
    calendar_range_for_sessions,
    is_market_open,
    is_regular_session_bar,
    latest_completed_session_bar,
    lookback_window,
    market_date,
    next_wake_time,
    recent_sessions,
    regular_session_bar_starts,
    session_bar_mask,
    session_from_local,
    session_wake_times,
    sessions_needed,
)

# --------------------------------------------------------------------------
# Real sessions, built the way the broker reports them: naive Eastern.
# --------------------------------------------------------------------------

#: An ordinary summer session. 09:30-16:00 EDT is 13:30-20:00 UTC.
ORDINARY = session_from_local(
    date(2026, 8, 26),
    datetime(2026, 8, 26, 9, 30),
    datetime(2026, 8, 26, 16, 0),
)

#: The half day after Thanksgiving 2025. 09:30-13:00 EST is 14:30-18:00 UTC.
EARLY_CLOSE = session_from_local(
    date(2025, 11, 28),
    datetime(2025, 11, 28, 9, 30),
    datetime(2025, 11, 28, 13, 0),
)

#: A winter session, on the other side of the daylight-saving change.
WINTER = session_from_local(
    date(2026, 1, 5),
    datetime(2026, 1, 5, 9, 30),
    datetime(2026, 1, 5, 16, 0),
)


class FakeCalendar:
    """A `MarketCalendar` over a literal list of sessions.

    Holidays and weekends are represented the way the broker represents them:
    by simply not being in the list.
    """

    def __init__(self, sessions: tuple[MarketSession, ...]) -> None:
        self._by_date = {session.session_date: session for session in sessions}
        self.calls = 0

    def session_for(self, day: date) -> MarketSession | None:
        self.calls += 1
        return self._by_date.get(day)

    def sessions_between(self, start: date, end: date) -> tuple[MarketSession, ...]:
        self.calls += 1
        return tuple(
            session for day, session in sorted(self._by_date.items()) if start <= day <= end
        )


def consecutive_sessions(first: date, count: int) -> tuple[MarketSession, ...]:
    """`count` ordinary weekday sessions starting at `first`, skipping weekends."""
    sessions: list[MarketSession] = []
    day = first
    while len(sessions) < count:
        if day.weekday() < 5:
            sessions.append(
                session_from_local(
                    day,
                    datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30),
                    datetime.combine(day, datetime.min.time()).replace(hour=16, minute=0),
                )
            )
        day += timedelta(days=1)
    return tuple(sessions)


# ==========================================================================
# The universe
# ==========================================================================


def test_the_universe_is_exactly_the_ten_configured_symbols() -> None:
    """CRITICAL: no arbitrary symbol creep, in either direction."""
    assert EQUITY_SYMBOLS == (
        "SPY",
        "QQQ",
        "IWM",
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "GOOGL",
        "META",
        "TSLA",
    )
    assert len(EQUITY_SYMBOLS) == EQUITY_UNIVERSE_SIZE == 10
    assert len(set(EQUITY_SYMBOLS)) == 10


def test_the_universe_order_is_the_contract() -> None:
    """Deterministic processing order, not an alphabetical accident."""
    assert EQUITY_SYMBOLS[0] == "SPY"
    assert EQUITY_SYMBOLS[-1] == "TSLA"
    assert list(EQUITY_SYMBOLS) != sorted(EQUITY_SYMBOLS)


@pytest.mark.parametrize("symbol", ["spy", " SPY ", "Tsla"])
def test_a_supported_symbol_is_normalized(symbol: str) -> None:
    assert normalize_symbol(symbol) == symbol.strip().upper()


@pytest.mark.parametrize("symbol", ["BTC/USD", "ETH/USD", "GOOG", "BRK.B", "VOO", "", "SPY.US", 5])
def test_a_symbol_outside_the_universe_is_refused(symbol: object) -> None:
    with pytest.raises(EquityError):
        normalize_symbol(symbol)  # type: ignore[arg-type]


def test_the_timeframe_is_exactly_fifteen_minutes() -> None:
    assert EQUITY_TIMEFRAME == "15m"
    assert normalize_timeframe("15M") == "15m"
    for rejected in ("1m", "5m", "1h", "1d"):
        with pytest.raises(EquityError):
            normalize_timeframe(rejected)


def test_the_market_timezone_is_new_york_and_is_stated() -> None:
    assert MARKET_TIMEZONE_NAME == "America/New_York"
    assert MARKET_TIMEZONE.key == "America/New_York"


# ==========================================================================
# Timezone semantics
# ==========================================================================


def test_naive_broker_times_are_read_as_eastern_and_stored_as_utc() -> None:
    """The single most important conversion: 09:30 is Eastern, not UTC."""
    assert ORDINARY.open_utc == datetime(2026, 8, 26, 13, 30, tzinfo=UTC)
    assert ORDINARY.close_utc == datetime(2026, 8, 26, 20, 0, tzinfo=UTC)
    assert ORDINARY.open_utc.tzinfo is UTC


def test_a_winter_session_shifts_by_the_daylight_saving_offset() -> None:
    """09:30 Eastern is 13:30 UTC in summer and 14:30 UTC in winter."""
    assert WINTER.open_utc == datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    assert WINTER.close_utc == datetime(2026, 1, 5, 21, 0, tzinfo=UTC)


def test_an_early_close_is_a_real_close_not_a_shorter_day() -> None:
    assert EARLY_CLOSE.close_utc == datetime(2025, 11, 28, 18, 0, tzinfo=UTC)
    assert EARLY_CLOSE.duration == timedelta(hours=3, minutes=30)


def test_market_date_is_answered_in_the_exchange_zone() -> None:
    """01:00 UTC on a Tuesday is still Monday evening in New York."""
    assert market_date(datetime(2026, 8, 26, 1, 0, tzinfo=UTC)) == date(2026, 8, 25)
    assert market_date(datetime(2026, 8, 26, 13, 30, tzinfo=UTC)) == date(2026, 8, 26)


def test_a_session_that_closes_before_it_opens_cannot_exist() -> None:
    with pytest.raises(SessionError):
        MarketSession(
            session_date=date(2026, 8, 26),
            open_utc=datetime(2026, 8, 26, 20, 0, tzinfo=UTC),
            close_utc=datetime(2026, 8, 26, 13, 30, tzinfo=UTC),
        )


def test_a_naive_session_boundary_is_refused_at_construction() -> None:
    with pytest.raises(SessionError):
        MarketSession(
            session_date=date(2026, 8, 26),
            open_utc=datetime(2026, 8, 26, 13, 30),
            close_utc=datetime(2026, 8, 26, 20, 0, tzinfo=UTC),
        )


# ==========================================================================
# Regular-session bars
# ==========================================================================


def test_an_ordinary_session_has_twenty_six_regular_bars() -> None:
    starts = regular_session_bar_starts(ORDINARY)

    assert len(starts) == 26
    assert starts[0] == datetime(2026, 8, 26, 13, 30, tzinfo=UTC)
    assert starts[-1] == datetime(2026, 8, 26, 19, 45, tzinfo=UTC)
    assert starts[-1] + timedelta(minutes=15) == ORDINARY.close_utc


def test_an_early_close_session_has_fourteen_regular_bars() -> None:
    """CRITICAL: an early close really is a shorter bar grid, not a full one."""
    starts = regular_session_bar_starts(EARLY_CLOSE)

    assert len(starts) == 14
    assert starts[0] == datetime(2025, 11, 28, 14, 30, tzinfo=UTC)
    assert starts[-1] == datetime(2025, 11, 28, 17, 45, tzinfo=UTC)
    assert starts[-1] + timedelta(minutes=15) == EARLY_CLOSE.close_utc
    assert len(starts) >= MIN_REGULAR_BARS_PER_SESSION


def test_extended_hours_boundaries_are_not_regular_session_bars() -> None:
    """Pre-market and post-market candles exist on IEX and are not tradable."""
    premarket = datetime(2026, 8, 26, 12, 30, tzinfo=UTC)  # 08:30 Eastern
    postmarket = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)  # 16:00 Eastern

    assert is_regular_session_bar(ORDINARY, premarket) is False
    assert is_regular_session_bar(ORDINARY, postmarket) is False
    assert is_regular_session_bar(ORDINARY, datetime(2026, 8, 26, 13, 30, tzinfo=UTC)) is True


def test_a_bar_straddling_the_close_is_not_a_regular_session_bar() -> None:
    """19:45 is in; 19:50 is not a boundary and 20:00 starts after the bell."""
    assert is_regular_session_bar(ORDINARY, datetime(2026, 8, 26, 19, 45, tzinfo=UTC)) is True
    assert is_regular_session_bar(ORDINARY, datetime(2026, 8, 26, 19, 50, tzinfo=UTC)) is False


def test_a_post_early_close_bar_is_out_even_though_the_market_is_normally_open() -> None:
    """13:15 Eastern is a regular bar on an ordinary day and not on a half day."""
    after_early_close = datetime(2025, 11, 28, 18, 15, tzinfo=UTC)

    assert is_regular_session_bar(EARLY_CLOSE, after_early_close) is False


# ==========================================================================
# The completed-bar rule
# ==========================================================================


def test_the_in_progress_bar_is_never_the_latest_completed_one() -> None:
    """CRITICAL: at 13:46 the 13:45 bar is still forming."""
    latest = latest_completed_session_bar(
        ORDINARY, now=datetime(2026, 8, 26, 13, 46, 17, tzinfo=UTC)
    )

    assert latest == datetime(2026, 8, 26, 13, 30, tzinfo=UTC)


def test_a_bar_becomes_complete_only_after_its_whole_interval_and_the_delay() -> None:
    just_before = datetime(2026, 8, 26, 13, 45, 4, tzinfo=UTC)
    just_after = datetime(2026, 8, 26, 13, 45, 6, tzinfo=UTC)

    assert latest_completed_session_bar(ORDINARY, now=just_before) is None
    assert latest_completed_session_bar(ORDINARY, now=just_after) == datetime(
        2026, 8, 26, 13, 30, tzinfo=UTC
    )


def test_before_the_first_bar_closes_there_is_nothing_to_act_on() -> None:
    assert (
        latest_completed_session_bar(ORDINARY, now=datetime(2026, 8, 26, 13, 35, tzinfo=UTC))
        is None
    )


def test_after_the_close_the_last_bar_of_the_session_is_the_latest_completed() -> None:
    """It is complete. Whether it may be *acted on* is the wake-time rule."""
    assert latest_completed_session_bar(
        ORDINARY, now=datetime(2026, 8, 26, 20, 30, tzinfo=UTC)
    ) == datetime(2026, 8, 26, 19, 45, tzinfo=UTC)


# ==========================================================================
# Wake times
# ==========================================================================


def test_wake_times_stop_at_the_last_boundary_inside_the_session() -> None:
    """The 15:45 bar closes at the bell, so no cycle wakes to trade it."""
    wakes = session_wake_times(ORDINARY)

    assert len(wakes) == 25
    assert wakes[0] == datetime(2026, 8, 26, 13, 45, 5, tzinfo=UTC)
    assert wakes[-1] == datetime(2026, 8, 26, 19, 45, 5, tzinfo=UTC)
    assert all(ORDINARY.contains(wake) for wake in wakes)


def test_an_early_close_produces_fewer_wake_times() -> None:
    wakes = session_wake_times(EARLY_CLOSE)

    assert len(wakes) == 13
    assert wakes[-1] == datetime(2025, 11, 28, 17, 45, 5, tzinfo=UTC)
    assert EARLY_CLOSE.contains(wakes[-1])


def test_the_next_wake_over_a_weekend_is_the_next_sessions_first_bar() -> None:
    friday = session_from_local(
        date(2026, 8, 28), datetime(2026, 8, 28, 9, 30), datetime(2026, 8, 28, 16, 0)
    )
    monday = session_from_local(
        date(2026, 8, 31), datetime(2026, 8, 31, 9, 30), datetime(2026, 8, 31, 16, 0)
    )
    calendar = FakeCalendar((friday, monday))

    wake = next_wake_time(calendar, now=datetime(2026, 8, 28, 20, 30, tzinfo=UTC))

    assert wake == datetime(2026, 8, 31, 13, 45, 5, tzinfo=UTC)


def test_the_next_wake_skips_a_holiday_entirely() -> None:
    """Thanksgiving is absent from the calendar, so the half day is next."""
    wednesday = session_from_local(
        date(2025, 11, 26), datetime(2025, 11, 26, 9, 30), datetime(2025, 11, 26, 16, 0)
    )
    calendar = FakeCalendar((wednesday, EARLY_CLOSE))

    wake = next_wake_time(calendar, now=datetime(2025, 11, 26, 21, 30, tzinfo=UTC))

    assert wake == datetime(2025, 11, 28, 14, 45, 5, tzinfo=UTC)


def test_a_calendar_with_no_session_in_range_is_an_error_not_a_forever_sleep() -> None:
    calendar = FakeCalendar(())

    with pytest.raises(SessionError):
        next_wake_time(calendar, now=datetime(2026, 8, 28, 20, 30, tzinfo=UTC))


# ==========================================================================
# Open / closed
# ==========================================================================


def test_the_session_is_open_between_the_bell_and_the_close() -> None:
    calendar = FakeCalendar((ORDINARY,))

    open_now, session = is_market_open(calendar, now=datetime(2026, 8, 26, 15, 0, tzinfo=UTC))

    assert open_now is True
    assert session == ORDINARY


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 8, 26, 13, 29, 59, tzinfo=UTC),  # one second before the bell
        datetime(2026, 8, 26, 20, 0, tzinfo=UTC),  # the closing instant itself
        datetime(2026, 8, 26, 22, 0, tzinfo=UTC),  # after hours
    ],
)
def test_the_session_is_closed_outside_the_regular_window(moment: datetime) -> None:
    calendar = FakeCalendar((ORDINARY,))

    open_now, session = is_market_open(calendar, now=moment)

    assert open_now is False
    assert session == ORDINARY


def test_a_weekend_has_no_session_at_all() -> None:
    calendar = FakeCalendar((ORDINARY,))

    open_now, session = is_market_open(calendar, now=datetime(2026, 8, 29, 15, 0, tzinfo=UTC))

    assert open_now is False
    assert session is None


def test_a_holiday_has_no_session_at_all() -> None:
    """Thanksgiving 2025 is not in the broker's calendar."""
    calendar = FakeCalendar((EARLY_CLOSE,))

    open_now, session = is_market_open(calendar, now=datetime(2025, 11, 27, 15, 0, tzinfo=UTC))

    assert open_now is False
    assert session is None


def test_an_early_close_afternoon_reads_as_closed() -> None:
    calendar = FakeCalendar((EARLY_CLOSE,))

    open_now, session = is_market_open(calendar, now=datetime(2025, 11, 28, 19, 0, tzinfo=UTC))

    assert open_now is False
    assert session == EARLY_CLOSE


# ==========================================================================
# Lookback windows
# ==========================================================================


def test_a_lookback_is_sized_against_the_shortest_real_session() -> None:
    assert sessions_needed(MIN_REGULAR_BARS_PER_SESSION) == 1
    assert sessions_needed(MIN_REGULAR_BARS_PER_SESSION + 1) == 2
    assert sessions_needed(200) == 15
    for rejected in (0, -1, True, "200"):
        with pytest.raises(SessionError):
            sessions_needed(rejected)  # type: ignore[arg-type]


def test_a_session_count_becomes_a_generous_calendar_range() -> None:
    start, end = calendar_range_for_sessions(date(2026, 8, 26), count=15)

    assert end == date(2026, 8, 26)
    assert (end - start).days >= 15 * 7 // 5


def test_recent_sessions_returns_the_newest_ones_ascending() -> None:
    sessions = consecutive_sessions(date(2026, 8, 3), 20)
    calendar = FakeCalendar(sessions)

    found = recent_sessions(calendar, day=sessions[-1].session_date, count=5)

    assert found == sessions[-5:]
    assert [session.session_date for session in found] == sorted(
        session.session_date for session in found
    )


def test_a_lookback_window_ends_at_the_newest_completed_bars_close() -> None:
    latest = datetime(2026, 8, 26, 19, 30, tzinfo=UTC)

    start, end = lookback_window((WINTER, ORDINARY), latest_bar_start=latest)

    assert start == WINTER.open_utc
    assert end == latest + timedelta(minutes=15)


def test_a_lookback_window_needs_at_least_one_session() -> None:
    with pytest.raises(SessionError):
        lookback_window((), latest_bar_start=datetime(2026, 8, 26, 19, 30, tzinfo=UTC))


def test_the_session_mask_uses_each_days_own_close() -> None:
    """CRITICAL: a mixed window with a half day in it is filtered per day."""
    bars = [
        datetime(2025, 11, 28, 17, 45, tzinfo=UTC),  # 12:45 ET, last early-close bar
        datetime(2025, 11, 28, 18, 15, tzinfo=UTC),  # 13:15 ET, after the early close
        datetime(2026, 8, 26, 12, 30, tzinfo=UTC),  # 08:30 ET, pre-market
        datetime(2026, 8, 26, 19, 45, tzinfo=UTC),  # 15:45 ET, last ordinary bar
        datetime(2026, 8, 26, 20, 0, tzinfo=UTC),  # 16:00 ET, post-market
    ]

    assert session_bar_mask((EARLY_CLOSE, ORDINARY), bars) == [True, False, False, True, False]


def test_a_bar_on_a_day_with_no_session_is_masked_out() -> None:
    weekend = datetime(2026, 8, 29, 15, 0, tzinfo=UTC)

    assert session_bar_mask((ORDINARY,), [weekend]) == [False]
