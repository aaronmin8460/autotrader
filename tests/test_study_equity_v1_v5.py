"""Tests for the SPY/QQQ historical pilot harness.

Every test here runs offline against constructed sessions and synthetic bars.
The pilot's own conclusions come from real provider data, but the *rules* the
pilot applies - the session filter, the aggregation legality checks, the
walk-forward gap, the probe placement - are properties that must hold on data
whose answers are known in advance, which is what these fixtures supply.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from studies.equity_v1_v5.aggregation import check_spanning, measure_yield
from studies.equity_v1_v5.calendar import (
    CalendarSnapshotError,
    SessionRecord,
    SnapshotCalendar,
    read_snapshot,
    write_snapshot,
)
from studies.equity_v1_v5.dataset import (
    describe_gaps,
    drop_duplicate_bars,
    expected_bar_starts,
    filter_regular_session,
    frame_digest,
    renull_undefined_vwap,
)
from studies.equity_v1_v5.leakage import scored_probe_indices
from studies.equity_v1_v5.windows import EMBARGO_BARS, LOOKBACK_BARS, ScoringWindow

from autotrader.decision.timeframes import (
    FOUR_HOUR_TIMEFRAME,
    HOUR_TIMEFRAME,
    aggregate_bars,
)
from autotrader.equity.session import MarketSession, regular_session_bar_starts

EASTERN_WINTER_OPEN = 14  # 09:30 New York is 14:30 UTC while EST is in force.
EASTERN_SUMMER_OPEN = 13  # ...and 13:30 UTC under EDT.


def _session(day: date, *, open_hour: int, close_hour: int, close_minute: int = 0) -> MarketSession:
    return MarketSession(
        session_date=day,
        open_utc=datetime(day.year, day.month, day.day, open_hour, 30, tzinfo=UTC),
        close_utc=datetime(day.year, day.month, day.day, close_hour, close_minute, tzinfo=UTC),
    )


def full_winter_session(day: date) -> MarketSession:
    """An ordinary 09:30-16:00 EST session: 14:30-21:00 UTC."""
    return _session(day, open_hour=EASTERN_WINTER_OPEN, close_hour=21)


def full_summer_session(day: date) -> MarketSession:
    """An ordinary 09:30-16:00 EDT session: 13:30-20:00 UTC."""
    return _session(day, open_hour=EASTERN_SUMMER_OPEN, close_hour=20)


def early_close_session(day: date) -> MarketSession:
    """A 09:30-13:00 EST half day: 14:30-18:00 UTC."""
    return _session(day, open_hour=EASTERN_WINTER_OPEN, close_hour=18)


def bars_for(sessions, *, symbol: str = "SPY", start_price: float = 100.0) -> pd.DataFrame:
    """Canonical regular-session bars for `sessions`, one per scheduled boundary."""
    rows = []
    price = start_price
    for session in sessions:
        for moment in regular_session_bar_starts(session):
            price += 0.01
            rows.append(
                {
                    "timestamp": moment,
                    "symbol": symbol,
                    "open": price,
                    "high": price + 0.05,
                    "low": price - 0.05,
                    "close": price + 0.01,
                    "volume": 1000.0,
                    "trade_count": 10.0,
                    "vwap": price,
                }
            )
    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["symbol"] = frame["symbol"].astype("string")
    return frame


def calendar_for(sessions) -> SnapshotCalendar:
    return SnapshotCalendar([SessionRecord.from_session(session) for session in sessions])


# --------------------------------------------------------------------------
# Session shape: the counts every later claim rests on
# --------------------------------------------------------------------------


def test_full_session_schedules_twenty_six_bars():
    assert len(regular_session_bar_starts(full_winter_session(date(2025, 1, 6)))) == 26
    assert len(regular_session_bar_starts(full_summer_session(date(2025, 6, 3)))) == 26


def test_early_close_schedules_fourteen_bars():
    assert len(regular_session_bar_starts(early_close_session(date(2024, 11, 29)))) == 14


# --------------------------------------------------------------------------
# Aggregation legality
# --------------------------------------------------------------------------


@pytest.mark.parametrize("builder", [full_winter_session, full_summer_session])
def test_full_session_yields_six_hours_and_one_four_hour(builder):
    """The yield the equity policy's constants are written against.

    Checked on both sides of daylight saving, because the UTC buckets a session
    falls into move by an hour between them and the counts must not.
    """
    sessions = [builder(date(2025, 1, 6) + timedelta(days=index)) for index in range(3)]
    frame = bars_for(sessions)
    assert len(aggregate_bars(frame, HOUR_TIMEFRAME)) == 6 * len(sessions)
    assert len(aggregate_bars(frame, FOUR_HOUR_TIMEFRAME)) == 1 * len(sessions)


def test_early_close_yields_three_hours_and_no_four_hour():
    """The case the averaged constant hides.

    A shortened session completes three 1-hour buckets and *no* 4-hour bucket,
    so a lookback sized by the average silently comes up short whenever early
    closes fall inside it.
    """
    frame = bars_for([early_close_session(date(2024, 11, 29))])
    assert len(aggregate_bars(frame, HOUR_TIMEFRAME)) == 3
    assert len(aggregate_bars(frame, FOUR_HOUR_TIMEFRAME)) == 0


def test_no_derived_bar_spans_the_overnight_gap():
    """The property that makes derived equity bars legal at all."""
    sessions = [full_winter_session(date(2025, 1, 6) + timedelta(days=index)) for index in range(5)]
    frame = bars_for(sessions)
    calendar = calendar_for(sessions)
    for spec in (HOUR_TIMEFRAME, FOUR_HOUR_TIMEFRAME):
        report = check_spanning(frame, calendar, spec)
        assert report.spanning_bars == 0, report.examples
        assert report.incomplete_constituent_bars == 0, report.examples
        assert report.ok


def test_extended_hours_bars_would_manufacture_an_illegal_bucket():
    """Why the session filter runs before aggregation, stated as a failing case.

    With pre-market candles left in, the 14:00 UTC bucket fills from two
    pre-market bars and two regular-session ones - and the aggregator, which
    counts rather than consults a calendar, cannot tell. The bucket it emits
    straddles the opening bell.
    """
    session = full_winter_session(date(2025, 1, 6))
    regular = bars_for([session])
    premarket = regular.iloc[:2].copy()
    premarket["timestamp"] = [
        pd.Timestamp("2025-01-06T14:00:00Z"),
        pd.Timestamp("2025-01-06T14:15:00Z"),
    ]
    contaminated = pd.concat([premarket, regular], ignore_index=True).sort_values(
        "timestamp", ignore_index=True
    )

    with_extended = aggregate_bars(contaminated, HOUR_TIMEFRAME)
    assert pd.Timestamp("2025-01-06T14:00:00Z") in set(with_extended["timestamp"])

    filtered, dropped = filter_regular_session(contaminated, calendar_for([session]))
    assert dropped == 2
    without_extended = aggregate_bars(filtered, HOUR_TIMEFRAME)
    assert pd.Timestamp("2025-01-06T14:00:00Z") not in set(without_extended["timestamp"])


def test_measure_yield_separates_early_closes_from_full_sessions():
    sessions = [
        full_winter_session(date(2024, 11, 25)),
        full_winter_session(date(2024, 11, 26)),
        early_close_session(date(2024, 11, 29)),
    ]
    frame = bars_for(sessions)
    report = measure_yield(frame, calendar_for(sessions), FOUR_HOUR_TIMEFRAME)
    assert report.by_session_length[26]["mean"] == 1.0
    assert report.by_session_length[14]["max"] == 0.0


# --------------------------------------------------------------------------
# Dataset reduction
# --------------------------------------------------------------------------


def test_filter_regular_session_drops_only_extended_hours():
    session = full_winter_session(date(2025, 1, 6))
    regular = bars_for([session])
    after_hours = regular.iloc[:1].copy()
    after_hours["timestamp"] = [pd.Timestamp("2025-01-06T21:15:00Z")]
    combined = pd.concat([regular, after_hours], ignore_index=True)

    kept, dropped = filter_regular_session(combined, calendar_for([session]))
    assert dropped == 1
    assert len(kept) == 26


def test_expected_bar_starts_uses_each_session_own_close():
    sessions = [full_winter_session(date(2024, 11, 26)), early_close_session(date(2024, 11, 29))]
    assert len(expected_bar_starts(sessions)) == 26 + 14


def test_describe_gaps_reports_a_missing_session_as_one_event():
    sessions = [
        full_winter_session(date(2025, 1, 6)),
        full_winter_session(date(2025, 1, 7)),
        full_winter_session(date(2025, 1, 8)),
    ]
    frame = bars_for(sessions)
    calendar = calendar_for(sessions)
    without_middle = frame[frame["timestamp"].dt.date != date(2025, 1, 7)].reset_index(drop=True)

    report = describe_gaps(without_middle, calendar)
    assert report.expected_bars == 78
    assert report.missing_bars == 26
    assert report.gap_events == 1
    assert report.sessions_observed == 2


def test_no_bar_is_ever_manufactured_for_a_missing_session():
    """The gap is described, never filled: the frame keeps exactly what arrived."""
    sessions = [full_winter_session(date(2025, 1, 6)), full_winter_session(date(2025, 1, 7))]
    frame = bars_for(sessions)
    without_middle = frame[frame["timestamp"].dt.date != date(2025, 1, 7)].reset_index(drop=True)
    kept, _ = filter_regular_session(without_middle, calendar_for(sessions))
    assert len(kept) == 26
    assert set(kept["timestamp"].dt.date) == {date(2025, 1, 6)}


def test_drop_duplicate_bars_keeps_the_first_and_counts():
    frame = bars_for([full_winter_session(date(2025, 1, 6))])
    doubled = pd.concat([frame, frame.iloc[:3]], ignore_index=True).sort_values(
        "timestamp", ignore_index=True
    )
    deduped, removed = drop_duplicate_bars(doubled)
    assert removed == 3
    assert len(deduped) == 26


def test_renull_undefined_vwap_only_touches_untraded_bars():
    frame = bars_for([full_winter_session(date(2025, 1, 6))])
    frame.loc[0, ["vwap", "volume", "trade_count"]] = [0.0, 0.0, 0.0]
    frame.loc[1, "vwap"] = 0.0  # a zero vwap on a bar that did trade: left alone.
    corrected, count = renull_undefined_vwap(frame)
    assert count == 1
    assert pd.isna(corrected.loc[0, "vwap"])
    assert corrected.loc[1, "vwap"] == 0.0


def test_frame_digest_is_stable_and_content_sensitive():
    frame = bars_for([full_winter_session(date(2025, 1, 6))])
    assert frame_digest(frame) == frame_digest(frame.copy())
    moved = frame.copy()
    moved.loc[0, "close"] += 0.01
    assert frame_digest(moved) != frame_digest(frame)


# --------------------------------------------------------------------------
# Calendar snapshot
# --------------------------------------------------------------------------


def test_snapshot_round_trips_and_preserves_early_closes(tmp_path):
    sessions = [full_winter_session(date(2024, 11, 26)), early_close_session(date(2024, 11, 29))]
    path = tmp_path / "calendar.json"
    write_snapshot(
        sessions,
        path,
        start=date(2024, 11, 26),
        end=date(2024, 11, 29),
        retrieved_at=datetime(2026, 8, 29, tzinfo=UTC),
    )
    calendar, payload = read_snapshot(path)
    assert payload["session_count"] == 2
    assert len(calendar) == 2
    early = [record for record in calendar.records if record.is_early_close]
    assert [record.session_date for record in early] == ["2024-11-29"]
    restored = calendar.session_for(date(2024, 11, 29))
    assert restored is not None
    assert len(regular_session_bar_starts(restored)) == 14


def test_reading_a_missing_snapshot_refuses_rather_than_guessing(tmp_path):
    with pytest.raises(CalendarSnapshotError, match="never"):
        read_snapshot(tmp_path / "absent.json")


def test_snapshot_records_the_daylight_saving_offset():
    winter = SessionRecord.from_session(full_winter_session(date(2025, 1, 6)))
    summer = SessionRecord.from_session(full_summer_session(date(2025, 6, 3)))
    assert winter.utc_offset_hours == -5
    assert summer.utc_offset_hours == -4
    assert winter.open_local == summer.open_local == "09:30"


# --------------------------------------------------------------------------
# Study configuration
# --------------------------------------------------------------------------


def test_lookback_clears_the_declared_requirement_with_margin():
    """The study's window must exceed the policy's own lower bound.

    The declared figure assumes every session is full and every scheduled bar
    was published; the measured worst case on real SPY and QQQ bars is higher.
    """
    from autotrader.decision.config import EQUITY_POLICY

    declared = EQUITY_POLICY.required_base_bars(("15m", "1h", "4h"))
    assert declared == 2834
    assert declared < LOOKBACK_BARS


def test_embargo_is_a_whole_regular_session():
    assert EMBARGO_BARS == 26


def test_scoring_window_selects_only_its_own_bars():
    sessions = [full_winter_session(date(2025, 1, 6) + timedelta(days=index)) for index in range(3)]
    frame = bars_for(sessions)
    window = ScoringWindow("w", date(2025, 1, 7), date(2025, 1, 7), "one session")
    assert len(window.bars(frame)) == 26
    first, last = window.positions(frame)
    assert (first, last) == (26, 51)


# --------------------------------------------------------------------------
# Leakage probe placement
# --------------------------------------------------------------------------


def test_probes_land_inside_the_scored_region():
    """The whole reason this study does not rely on the generic probe placement.

    With a 3000-bar warm-up in a 3024-bar frame, evenly spacing probes across
    the frame puts every one of them inside the warm-up, where no decision
    exists to compare - so the audit passes without testing anything.
    """
    total, lookback = 3024, 3000
    indices = scored_probe_indices(total, lookback_bars=lookback)
    assert indices
    assert all(index >= lookback for index in indices)
    assert all(index <= total - 2 for index in indices)


def test_probe_placement_is_empty_when_nothing_could_be_scored():
    assert scored_probe_indices(3000, lookback_bars=3000) == ()
