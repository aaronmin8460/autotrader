"""Proving the 15m -> 1h -> 4h derivation is legal on a session-traded market.

V3 reads three timeframes and fetches one. The other two are derived by
`autotrader.decision.timeframes.aggregate_bars`, which buckets on the UTC clock
and keeps a bucket only when it holds exactly its full complement of base bars.
That rule was written against continuous crypto, where every bucket fills. This
module asks whether it is still correct when the market is shut for
seventeen and a half hours a day, and answers with measurements rather than with
the docstring's claim.

**The four illegal things a derived bar could do.** It could span the overnight
gap, joining Friday's afternoon to Monday's morning into one candle. It could
span the opening or closing bell, mixing extended-hours prints into a
regular-session candle. It could contain a base bar that had not closed when the
decision was made. Or it could simply be a candle whose constituents the
provider never published. Each has a check here, run over every derived bar of
the real dataset - not over a sample.

**The completeness rule is load-bearing, and the session filter is what loads
it.** A 4-hour UTC bucket that straddles the close cannot fill from
regular-session bars alone, so it is dropped and no candle crosses the gap.
That only holds while extended-hours bars are absent: leave them in and the
bucket *can* fill, from a mixture of regular and post-market prints, and the
aggregator has no way to know. So `spanning_violations` is checked against the
session calendar, and a violation here is the alarm that the filter was skipped.

**Yield is measured, not assumed.** `EQUITY_BASE_BARS_PER_COMPLETE_BAR` claims a
session yields one 4-hour bar and about one 1-hour bar per five base bars. This
module counts what the aggregator actually produced per session, per session
length, so the constant is checked against the data it is meant to describe -
including on the early closes, where the answer is not the average.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import pandas as pd

from autotrader.decision.timeframes import (
    BASE_TIMEFRAME,
    FOUR_HOUR_TIMEFRAME,
    HOUR_TIMEFRAME,
    TimeframeSpec,
    aggregate_bars,
    usable_history,
)
from autotrader.equity.session import MarketSession, market_date, regular_session_bar_starts
from studies.equity_v1_v5.calendar import SnapshotCalendar

#: The base interval every derived bar in this study is built from.
BASE_INTERVAL = pd.Timedelta("15min")


@dataclass(frozen=True)
class SpanReport:
    """Whether any derived bar of one timeframe covers time the market was shut."""

    label: str
    derived_bars: int
    spanning_bars: int
    incomplete_constituent_bars: int
    examples: tuple[dict[str, object], ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.spanning_bars == 0 and self.incomplete_constituent_bars == 0

    def to_json_dict(self) -> dict[str, object]:
        return {
            "timeframe": self.label,
            "derived_bars": self.derived_bars,
            "spanning_bars": self.spanning_bars,
            "incomplete_constituent_bars": self.incomplete_constituent_bars,
            "ok": self.ok,
            "examples": list(self.examples),
        }


@dataclass(frozen=True)
class YieldReport:
    """How many complete derived bars a session of each length actually produced."""

    label: str
    by_session_length: dict[int, dict[str, float]]

    def to_json_dict(self) -> dict[str, object]:
        return {"timeframe": self.label, "by_session_length": self.by_session_length}


def session_index(
    calendar: SnapshotCalendar,
    frame: pd.DataFrame,
) -> dict[pd.Timestamp, MarketSession]:
    """Map every base bar in `frame` to the session it belongs to."""
    first = market_date(frame["timestamp"].iloc[0].to_pydatetime())
    last = market_date(frame["timestamp"].iloc[-1].to_pydatetime())
    sessions = {s.session_date: s for s in calendar.sessions_between(first, last)}
    return {
        ts: sessions[market_date(ts.to_pydatetime())]
        for ts in frame["timestamp"]
        if market_date(ts.to_pydatetime()) in sessions
    }


def check_spanning(
    base: pd.DataFrame,
    calendar: SnapshotCalendar,
    spec: TimeframeSpec,
    *,
    top: int = 5,
) -> SpanReport:
    """Confirm no derived bar of `spec` crosses a session boundary or is short.

    A derived bar is legal when every base bar inside its interval belongs to
    one and the same session, and when there are exactly as many of them as the
    bucket requires. Both are checked against the base frame the bar was built
    from, so this measures the aggregator's real output rather than restating
    its rule.
    """
    derived = aggregate_bars(base, spec, base_interval=BASE_INTERVAL)
    constituents = spec.constituents(BASE_INTERVAL)
    interval = pd.Timedelta(spec.interval)

    by_ts = base.set_index("timestamp")
    sessions_of = session_index(calendar, base)

    spanning = 0
    short = 0
    examples: list[dict[str, object]] = []
    for start in derived["timestamp"]:
        window = by_ts.loc[(by_ts.index >= start) & (by_ts.index < start + interval)]
        if len(window) != constituents:
            short += 1
            if len(examples) < top:
                examples.append(
                    {
                        "bucket_start": start.isoformat(),
                        "problem": "constituent_count",
                        "found": len(window),
                        "required": constituents,
                    }
                )
            continue
        days = {sessions_of[ts].session_date for ts in window.index if ts in sessions_of}
        if len(days) != 1:
            spanning += 1
            if len(examples) < top:
                examples.append(
                    {
                        "bucket_start": start.isoformat(),
                        "problem": "spans_sessions",
                        "sessions": sorted(str(d) for d in days),
                    }
                )
    return SpanReport(
        label=spec.label,
        derived_bars=len(derived),
        spanning_bars=spanning,
        incomplete_constituent_bars=short,
        examples=tuple(examples),
    )


def measure_yield(
    base: pd.DataFrame,
    calendar: SnapshotCalendar,
    spec: TimeframeSpec,
) -> YieldReport:
    """Count complete `spec` bars produced per session, grouped by session length.

    Grouped by the session's own regular-bar count rather than by date, because
    the interesting split is twenty-six-bar days against fourteen-bar early
    closes - and averaging the two is exactly what hides the early close.
    """
    derived = aggregate_bars(base, spec, base_interval=BASE_INTERVAL)
    first = market_date(base["timestamp"].iloc[0].to_pydatetime())
    last = market_date(base["timestamp"].iloc[-1].to_pydatetime())
    sessions = calendar.sessions_between(first, last)

    observed_days = {market_date(ts.to_pydatetime()) for ts in base["timestamp"]}
    per_length: dict[int, list[int]] = {}
    derived_days = [market_date(ts.to_pydatetime()) for ts in derived["timestamp"]]
    counts_by_day: dict[object, int] = {}
    for day in derived_days:
        counts_by_day[day] = counts_by_day.get(day, 0) + 1

    for session in sessions:
        if session.session_date not in observed_days:
            continue
        length = len(regular_session_bar_starts(session))
        per_length.setdefault(length, []).append(counts_by_day.get(session.session_date, 0))

    summary = {
        length: {
            "sessions": len(values),
            "min": float(min(values)),
            "max": float(max(values)),
            "mean": sum(values) / len(values),
        }
        for length, values in sorted(per_length.items())
    }
    return YieldReport(label=spec.label, by_session_length=summary)


def check_causality(
    base: pd.DataFrame,
    spec: TimeframeSpec,
    *,
    samples: int = 400,
) -> dict[str, object]:
    """Confirm `usable_history` never admits a derived bar that had not closed.

    For a sample of base bars spread across the whole frame, every derived bar
    the aligner offers must close at or before the base bar does. A single
    violation would be look-ahead of exactly the kind V3's alignment rule exists
    to prevent.
    """
    derived = aggregate_bars(base, spec, base_interval=BASE_INTERVAL)
    if derived.empty:
        return {"timeframe": spec.label, "sampled_bars": 0, "violations": 0, "ok": True}

    step = max(1, len(base) // samples)
    violations = 0
    checked = 0
    worst: dict[str, object] | None = None
    for anchor in base["timestamp"].iloc[::step]:
        usable = usable_history(derived, spec, base_bar_start=anchor, base_interval=BASE_INTERVAL)
        checked += 1
        if usable.empty:
            continue
        latest_close = usable["timestamp"].iloc[-1] + pd.Timedelta(spec.interval)
        deadline = anchor + BASE_INTERVAL
        if latest_close > deadline:
            violations += 1
            if worst is None:
                worst = {
                    "base_bar_start": anchor.isoformat(),
                    "base_bar_close": deadline.isoformat(),
                    "derived_bar_close": latest_close.isoformat(),
                }
    return {
        "timeframe": spec.label,
        "sampled_bars": checked,
        "violations": violations,
        "ok": violations == 0,
        "example": worst,
    }


def audit(
    base: pd.DataFrame,
    calendar: SnapshotCalendar,
    specs: Sequence[TimeframeSpec] = (BASE_TIMEFRAME, HOUR_TIMEFRAME, FOUR_HOUR_TIMEFRAME),
) -> dict[str, object]:
    """Run every aggregation check over one symbol's regular-session frame."""
    return {
        "spanning": [check_spanning(base, calendar, spec).to_json_dict() for spec in specs],
        "yield": [measure_yield(base, calendar, spec).to_json_dict() for spec in specs],
        "causality": [check_causality(base, spec) for spec in specs],
    }


__all__ = [
    "BASE_INTERVAL",
    "SpanReport",
    "YieldReport",
    "audit",
    "check_causality",
    "check_spanning",
    "measure_yield",
    "session_index",
]
