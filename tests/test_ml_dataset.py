"""M1 data-layer tests: the grid, the features, the labels, the builder, the split.

The centre of gravity here is look-ahead. Three tests carry that weight and the
rest support them:

* `test_features_are_unchanged_when_the_future_is_deleted` truncates the input
  and requires every surviving feature row to be bit-identical.
* `test_features_are_unchanged_when_a_future_bar_is_altered` multiplies later
  bars by 1.5 and requires the same thing. A feature that peeked would move.
* `test_no_training_label_resolves_inside_the_validation_window` is the same
  property one stage later: a purged split cannot carry a training row whose
  outcome was decided by data the next window is graded on.

Everything is offline. No test here opens a socket, reads a credential, or
constructs a client, and `test_a_dataset_builds_with_every_socket_blocked`
proves it by making `socket.socket` raise.
"""

from __future__ import annotations

import ast
import inspect
import socket
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from autotrader.equity.session import session_from_local
from autotrader.ml import (
    AssetClass,
    MLError,
    asset_class_for_symbol,
    filesystem_slug,
    normalize_symbol,
)
from autotrader.ml import dataset as dataset_module
from autotrader.ml import features as features_module
from autotrader.ml.dataset import (
    DatasetError,
    DatasetSpec,
    build_dataset,
    build_dataset_from_parquet,
    build_observations,
    dataset_schema,
    frame_fingerprint,
    labelled_frame,
    read_dataset,
    write_dataset,
)
from autotrader.ml.features import (
    FEATURE_COLUMNS,
    FEATURE_NAMES,
    VOLATILITY_FEATURE,
    compute_features,
)
from autotrader.ml.grid import (
    BARS_PER_UTC_DAY,
    BarGrid,
    GridError,
    StaticMarketCalendar,
    bar_span,
    build_grid,
    crypto_grid,
    equity_grid,
    load_sessions,
    write_sessions,
)
from autotrader.ml.labels import (
    MINIMUM_ENTRY_OFFSET_BARS,
    TERNARY_BUY,
    TERNARY_SELL,
    LabelError,
    LabelKind,
    LabelSpec,
    SessionPolicy,
    ThresholdMode,
    compute_labels,
)
from autotrader.ml.schema import (
    FEATURE_SCHEMA_VERSION,
    FEATURE_WINDOW_BARS,
    KEY_COLUMNS,
    PROVENANCE_COLUMNS,
    ColumnRole,
    ColumnSpec,
    SchemaError,
    build_schema,
)
from autotrader.ml.splits import (
    SplitError,
    SplitSpec,
    assert_no_leakage,
    temporal_split,
    walk_forward_folds,
)
from autotrader.ml.storage import sha256_of_record
from autotrader.runtime.schedule import BAR_INTERVAL

CRYPTO_SYMBOL = "BTC/USD"
EQUITY_SYMBOL = "SPY"
T0 = datetime(2026, 1, 1, tzinfo=UTC)

#: A weekday the US market is open, chosen so the first session is a Monday.
FIRST_SESSION = date(2026, 3, 2)


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


def synthetic_bars(
    timestamps: list[datetime],
    *,
    symbol: str = CRYPTO_SYMBOL,
    seed: int = 7,
    base: float = 50_000.0,
) -> pd.DataFrame:
    """Canonical bars over exactly `timestamps`, deterministic given `seed`.

    A geometric random walk rather than a straight line: several tests compare
    feature values for equality across builds, and a constant series would make
    those comparisons pass whatever the feature code did.
    """
    rng = np.random.default_rng(seed)
    count = len(timestamps)
    close = base * np.exp(np.cumsum(rng.normal(0.0, 0.002, count)))
    high = close * (1.0 + np.abs(rng.normal(0.0, 0.001, count)))
    low = close * (1.0 - np.abs(rng.normal(0.0, 0.001, count)))
    open_ = np.clip(np.concatenate([[close[0]], close[:-1]]), low, high)
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(timestamps, utc=True),
            "symbol": pd.array([symbol] * count, dtype="string"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(1.0, 10.0, count),
            "trade_count": rng.integers(1, 100, count).astype("float64"),
            "vwap": close,
        }
    )


def crypto_bars(count: int = 400, *, start: datetime = T0, seed: int = 7) -> pd.DataFrame:
    """`count` consecutive 15-minute crypto bars from `start`."""
    return synthetic_bars(
        [start + timedelta(minutes=15 * index) for index in range(count)], seed=seed
    )


def grid_for(bars: pd.DataFrame) -> BarGrid:
    """The crypto grid spanning a bar frame."""
    return crypto_grid(
        bars["timestamp"].iloc[0].to_pydatetime(), bars["timestamp"].iloc[-1].to_pydatetime()
    )


def market_sessions(count: int = 12, *, early_closes: tuple[int, ...] = (5,)) -> tuple:
    """`count` consecutive weekday sessions, some of them 13:00 early closes."""
    built = []
    day = FIRST_SESSION
    while len(built) < count:
        if day.weekday() < 5:
            close_hour = 13 if len(built) in early_closes else 16
            built.append(
                session_from_local(
                    day,
                    datetime.combine(day, time(9, 30)),
                    datetime.combine(day, time(close_hour, 0)),
                )
            )
        day += timedelta(days=1)
    return tuple(built)


def equity_bars(grid: BarGrid, *, seed: int = 3) -> pd.DataFrame:
    """Bars covering exactly the regular-session boundaries of an equity grid."""
    return synthetic_bars(
        [moment for moment in grid.starts], symbol=EQUITY_SYMBOL, seed=seed, base=400.0
    )


def forward_return_spec(horizon: int = 4) -> LabelSpec:
    return LabelSpec(name="fr", kind=LabelKind.FORWARD_RETURN, horizon_bars=horizon)


def build_crypto(bars: pd.DataFrame, label: LabelSpec | None = None):
    """Build a crypto dataset over exactly the bars supplied."""
    return build_dataset(
        bars,
        spec=DatasetSpec(symbol=CRYPTO_SYMBOL, label=label or forward_return_spec()),
        grid=grid_for(bars),
    )


def code_without_prose(source: str) -> str:
    """`source` with every docstring removed.

    The same helper the backtest suite uses, for the same reason: the source
    guards below are about executable code, and these modules document the
    idioms they refuse by name.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------


def test_symbols_resolve_to_their_own_asset_class() -> None:
    assert asset_class_for_symbol("BTC/USD") is AssetClass.CRYPTO
    assert asset_class_for_symbol("spy") is AssetClass.EQUITY


def test_an_unknown_symbol_is_refused() -> None:
    with pytest.raises(MLError, match="Unknown symbol"):
        asset_class_for_symbol("DOGE/USD")


def test_a_symbol_from_the_wrong_universe_is_refused() -> None:
    with pytest.raises(MLError, match="crypto symbol"):
        normalize_symbol("BTC/USD", AssetClass.EQUITY)


def test_the_filesystem_slug_covers_both_universes() -> None:
    assert filesystem_slug("btc/usd") == "BTC_USD"
    assert filesystem_slug("spy") == "SPY"


# --------------------------------------------------------------------------
# The grid: crypto is continuous
# --------------------------------------------------------------------------


def test_a_crypto_grid_holds_every_boundary_including_the_weekend() -> None:
    saturday = datetime(2026, 1, 3, tzinfo=UTC)
    grid = crypto_grid(saturday, saturday + timedelta(days=2) - BAR_INTERVAL)
    assert len(grid) == 2 * BARS_PER_UTC_DAY
    assert grid.starts[BARS_PER_UTC_DAY] == saturday + timedelta(days=1)


def test_a_crypto_grid_has_no_session_gap_at_midnight() -> None:
    """A UTC date rolls over; the market does not close."""
    grid = crypto_grid(T0, T0 + timedelta(days=2))
    midnight = grid.position_of(T0 + timedelta(days=1))
    assert grid.session_ids[midnight - 1] != grid.session_ids[midnight]
    assert grid.has_session_gaps is False
    assert grid.spans_session_gap(midnight - 1, midnight) is False


def test_a_crypto_session_bar_index_comes_from_the_clock_not_the_range() -> None:
    """So the same market moment is the same row however the request was framed."""
    early = crypto_grid(T0, T0 + timedelta(hours=6))
    late = crypto_grid(T0 + timedelta(hours=2), T0 + timedelta(hours=6))
    moment = T0 + timedelta(hours=3)
    assert (
        early.session_bar_indices[early.position_of(moment)]
        == late.session_bar_indices[late.position_of(moment)]
        == 12
    )
    assert set(early.session_bar_counts) == {BARS_PER_UTC_DAY}


def test_a_crypto_grid_refuses_an_inverted_range() -> None:
    with pytest.raises(GridError, match="before start"):
        crypto_grid(T0 + timedelta(days=1), T0)


# --------------------------------------------------------------------------
# The grid: equities run sessions
# --------------------------------------------------------------------------


def test_an_equity_grid_holds_only_regular_session_bars() -> None:
    grid = equity_grid(market_sessions(count=1, early_closes=()))
    assert len(grid) == 26
    assert grid.starts[0] == datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    assert grid.starts[-1] == datetime(2026, 3, 2, 20, 45, tzinfo=UTC)


def test_an_early_close_contributes_its_own_shorter_session() -> None:
    grid = equity_grid(market_sessions(count=6, early_closes=(5,)))
    counts = {
        session_id: count
        for session_id, count in zip(grid.session_ids, grid.session_bar_counts, strict=True)
    }
    assert counts["2026-03-09"] == 14
    assert counts["2026-03-06"] == 26


def test_consecutive_equity_positions_can_cross_a_night() -> None:
    grid = equity_grid(market_sessions(count=2, early_closes=()))
    last_of_day = grid.position_of(datetime(2026, 3, 2, 20, 45, tzinfo=UTC))
    assert grid.starts[last_of_day + 1] == datetime(2026, 3, 3, 14, 30, tzinfo=UTC)
    assert grid.spans_session_gap(last_of_day, last_of_day + 1) is True


def test_an_equity_grid_needs_a_calendar_and_refuses_a_date_range() -> None:
    with pytest.raises(GridError, match="explicit session calendar"):
        build_grid(AssetClass.EQUITY)
    with pytest.raises(GridError, match="bounded by its sessions"):
        build_grid(AssetClass.EQUITY, start=T0, end=T0, sessions=market_sessions(1))


def test_a_crypto_grid_refuses_a_session_calendar() -> None:
    with pytest.raises(GridError, match="24/7"):
        build_grid(AssetClass.CRYPTO, sessions=market_sessions(1))


def test_unordered_sessions_are_refused_rather_than_sorted() -> None:
    sessions = market_sessions(count=3, early_closes=())
    with pytest.raises(GridError, match="strictly ascending"):
        equity_grid((sessions[1], sessions[0], sessions[2]))


def test_a_timestamp_off_the_grid_is_refused_not_rounded() -> None:
    grid = equity_grid(market_sessions(count=1, early_closes=()))
    with pytest.raises(GridError, match="not a bar on this equity grid"):
        grid.position_of(datetime(2026, 3, 2, 13, 0, tzinfo=UTC))


def test_the_same_bar_distance_is_a_different_amount_of_time_in_each_book() -> None:
    """Four bars is an hour of crypto and can be three days of equity."""
    continuous = crypto_grid(T0, T0 + timedelta(days=1))
    assert bar_span(continuous, 0, 4) == timedelta(hours=1)

    sessions = equity_grid(market_sessions(count=3, early_closes=()))
    friday_close = sessions.position_of(datetime(2026, 3, 3, 20, 45, tzinfo=UTC))
    assert bar_span(sessions, friday_close, friday_close + 1) > timedelta(hours=16)


def test_a_session_calendar_round_trips_through_json(tmp_path: Path) -> None:
    sessions = market_sessions(count=4)
    path = tmp_path / "sessions.json"
    write_sessions(path, sessions)
    assert load_sessions(path) == sessions


def test_a_static_calendar_satisfies_the_market_calendar_protocol() -> None:
    calendar = StaticMarketCalendar(market_sessions(count=5, early_closes=()))
    assert calendar.session_for(FIRST_SESSION) is not None
    assert calendar.session_for(date(2026, 3, 7)) is None
    assert len(calendar.sessions_between(FIRST_SESSION, date(2026, 3, 4))) == 3


def test_a_snapshot_of_any_calendar_becomes_a_static_one() -> None:
    """The seam that keeps dataset building offline."""
    live = StaticMarketCalendar(market_sessions(count=6, early_closes=()))
    snapshot = StaticMarketCalendar.from_calendar(live, FIRST_SESSION, date(2026, 3, 4))
    assert len(snapshot.sessions) == 3


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


def test_a_feature_column_may_not_declare_a_forward_horizon() -> None:
    with pytest.raises(SchemaError, match="had not happened"):
        ColumnSpec(
            name="peek",
            dtype="float64",
            role=ColumnRole.FEATURE,
            description="Reads tomorrow.",
            forward_bars=1,
        )


def test_every_shipped_feature_declares_a_zero_forward_horizon() -> None:
    assert [column.forward_bars for column in FEATURE_COLUMNS] == [0] * len(FEATURE_COLUMNS)


def test_the_longest_feature_window_matches_the_declared_window_constant() -> None:
    """`bars_present_in_window` is sized against this; drift would make it a lie."""
    assert max(column.lookback_bars for column in FEATURE_COLUMNS) == FEATURE_WINDOW_BARS


def test_the_feature_contract_has_not_changed_without_a_version_bump() -> None:
    """A golden pin over the fixed and feature columns.

    Editing a feature's window, dtype, or description changes this hash. When
    that is intended, bump FEATURE_SCHEMA_VERSION and update the pin in the
    same commit - which is exactly the review this test exists to force.
    """
    pinned = "0e8b393ffc81251019ad77debb84fdc29e77dbff5d80e5517e356a6682f2ac1e"
    actual = sha256_of_record(
        [column.to_record() for column in (*KEY_COLUMNS, *PROVENANCE_COLUMNS, *FEATURE_COLUMNS)]
    )
    assert actual == pinned, (
        f"The feature contract changed (now {actual}). Bump FEATURE_SCHEMA_VERSION "
        "from " + FEATURE_SCHEMA_VERSION + " and update this pin."
    )


def test_a_schema_carries_exactly_one_label() -> None:
    with pytest.raises(SchemaError, match="exactly one label"):
        build_schema(FEATURE_COLUMNS, ())


def test_the_schema_fingerprint_moves_when_a_label_changes() -> None:
    assert (
        dataset_schema(forward_return_spec(4)).fingerprint
        != dataset_schema(forward_return_spec(8)).fingerprint
    )


def test_a_frame_with_reordered_columns_is_refused() -> None:
    schema = dataset_schema(forward_return_spec())
    build = build_crypto(crypto_bars())
    shuffled = build.frame[list(reversed(schema.names))]
    with pytest.raises(SchemaError, match="wrong order"):
        schema.validate_frame(shuffled)


# --------------------------------------------------------------------------
# Features: no look-ahead
# --------------------------------------------------------------------------


def test_the_feature_module_contains_no_forward_looking_idiom() -> None:
    """A structural guard, so a future edit has to argue with a test."""
    source = code_without_prose(inspect.getsource(features_module))
    for forbidden in (
        "shift(-",
        "center=True",
        "bfill",
        "backfill",
        "interpolate",
        "iloc[::-1]",
        "[::-1]",
    ):
        assert forbidden not in source, forbidden


def test_features_are_unchanged_when_the_future_is_deleted() -> None:
    """THE no-look-ahead test. Truncate the input; every surviving row must match."""
    bars = crypto_bars(count=400)
    full = build_crypto(bars).frame
    truncated_bars = bars.iloc[:320].reset_index(drop=True)
    truncated = build_crypto(truncated_bars).frame

    overlap = full.loc[full["feature_timestamp"].isin(truncated["feature_timestamp"])].reset_index(
        drop=True
    )
    assert len(overlap) == len(truncated)
    pd.testing.assert_frame_equal(overlap[list(FEATURE_NAMES)], truncated[list(FEATURE_NAMES)])


def test_features_are_unchanged_when_a_future_bar_is_altered() -> None:
    """Stronger than truncation: the future exists, and is different."""
    bars = crypto_bars(count=400)
    spec = DatasetSpec(symbol=CRYPTO_SYMBOL, label=forward_return_spec())
    original = build_dataset(bars, spec=spec, grid=grid_for(bars)).frame

    perturbed_bars = bars.copy()
    boundary = perturbed_bars["timestamp"].iloc[350]
    perturbed_bars.loc[350:, ["open", "high", "low", "close"]] *= 1.5
    perturbed = build_dataset(perturbed_bars, spec=spec, grid=grid_for(perturbed_bars)).frame

    def past(frame: pd.DataFrame) -> pd.DataFrame:
        return frame.loc[frame["feature_timestamp"] < boundary, list(FEATURE_NAMES)].reset_index(
            drop=True
        )

    pd.testing.assert_frame_equal(past(original), past(perturbed))


def test_a_feature_row_is_stamped_with_its_own_bars_close() -> None:
    bars = crypto_bars(count=200)
    frame = build_crypto(bars).frame
    assert (frame["knowable_at"] == frame["feature_timestamp"] + BAR_INTERVAL).all()


def test_an_overnight_return_is_flagged_on_an_equity_grid() -> None:
    grid = equity_grid(market_sessions(count=12, early_closes=()))
    frame = build_dataset(
        equity_bars(grid),
        spec=DatasetSpec(symbol=EQUITY_SYMBOL, label=forward_return_spec()),
        grid=grid,
    ).frame
    flagged = frame.loc[frame["prior_bar_crosses_session_gap"] == 1.0]
    assert not flagged.empty
    assert (flagged["session_bar_count"] > 0).all()
    # Every flagged row is the first bar of its session, and no other row is.
    assert (flagged["bars_since_session_start"] == 0.0).all()
    unflagged = frame.loc[frame["prior_bar_crosses_session_gap"] == 0.0]
    assert (unflagged["bars_since_session_start"] > 0.0).all()


def test_a_crypto_midnight_is_never_flagged_as_a_session_gap() -> None:
    bars = crypto_bars(count=300)
    frame = build_crypto(bars).frame
    assert (frame["prior_bar_crosses_session_gap"] == 0.0).all()
    assert frame["bars_since_session_start"].min() == 0.0


def test_features_are_positional_not_time_based() -> None:
    """An equity 4-bar return spans four *tradable* bars, not four wall-clock hours."""
    grid = equity_grid(market_sessions(count=3, early_closes=()))
    bars = equity_bars(grid)
    observations = build_observations(bars, grid, EQUITY_SYMBOL)
    computed = compute_features(observations, has_session_gaps=True)
    first_of_second_day = grid.position_of(datetime(2026, 3, 3, 14, 30, tzinfo=UTC))
    close = observations["close"]
    expected = close.iloc[first_of_second_day] / close.iloc[first_of_second_day - 4] - 1.0
    assert computed["return_4"].iloc[first_of_second_day] == pytest.approx(expected)


# --------------------------------------------------------------------------
# Features: missing data
# --------------------------------------------------------------------------


def test_a_missing_bar_is_a_hole_and_is_never_filled() -> None:
    bars = crypto_bars(count=300)
    grid = grid_for(bars)
    dropped = bars.drop(index=120).reset_index(drop=True)
    observations = build_observations(dropped, grid, CRYPTO_SYMBOL)

    assert bool(observations["is_present"].iloc[120]) is False
    assert pd.isna(observations["close"].iloc[120])
    # The neighbours were not back-filled from each other.
    assert observations["close"].iloc[119] != observations["close"].iloc[121]


def test_a_window_covering_a_hole_yields_no_value() -> None:
    bars = crypto_bars(count=300)
    grid = grid_for(bars)
    holed = bars.drop(index=200).reset_index(drop=True)
    observations = build_observations(holed, grid, CRYPTO_SYMBOL)
    computed = compute_features(observations, has_session_gaps=False)
    assert pd.isna(computed["realized_volatility_16"].iloc[205])
    assert pd.isna(computed["return_1"].iloc[201])


def test_rows_whose_window_was_not_fully_observed_are_dropped() -> None:
    bars = crypto_bars(count=300)
    grid = grid_for(bars)
    spec = DatasetSpec(symbol=CRYPTO_SYMBOL, label=forward_return_spec())
    build = build_dataset(bars.drop(index=200).reset_index(drop=True), spec=spec, grid=grid)

    assert build.missing_bar_count == 1
    assert build.dropped_incomplete_window_count > 0
    kept = build.frame["feature_timestamp"]
    assert bars["timestamp"].iloc[200] not in set(kept)
    # The hole is invisible from FEATURE_WINDOW_BARS onwards.
    assert bars["timestamp"].iloc[200 + FEATURE_WINDOW_BARS] in set(kept)


def test_the_warm_up_head_is_dropped_by_the_same_policy() -> None:
    bars = crypto_bars(count=200)
    build = build_crypto(bars)
    assert build.row_count == 200 - (FEATURE_WINDOW_BARS - 1)
    assert build.frame["bars_present_in_window"].min() == FEATURE_WINDOW_BARS


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------


def test_a_label_cannot_be_entered_on_its_own_feature_bar() -> None:
    with pytest.raises(LabelError, match="cannot be filled inside bar t"):
        LabelSpec(
            name="same-bar",
            kind=LabelKind.FORWARD_RETURN,
            horizon_bars=4,
            entry_offset_bars=0,
        )
    assert MINIMUM_ENTRY_OFFSET_BARS == 1


def test_a_zero_bar_horizon_is_refused() -> None:
    with pytest.raises(LabelError, match="at least 1"):
        LabelSpec(name="none", kind=LabelKind.FORWARD_RETURN, horizon_bars=0)


def test_a_label_may_not_exit_at_a_bars_high() -> None:
    with pytest.raises(LabelError, match="only knowable once the bar is over"):
        LabelSpec(
            name="perfect", kind=LabelKind.FORWARD_RETURN, horizon_bars=4, exit_price_column="high"
        )


def test_a_ternary_label_needs_a_hold_band() -> None:
    with pytest.raises(LabelError, match="HOLD is empty"):
        LabelSpec(
            name="t",
            kind=LabelKind.TERNARY,
            horizon_bars=4,
            upper_threshold=0.0,
            lower_threshold=0.0,
        )


def test_a_continuous_label_refuses_a_threshold_it_would_ignore() -> None:
    with pytest.raises(LabelError, match="applies no"):
        LabelSpec(name="fr", kind=LabelKind.FORWARD_RETURN, horizon_bars=4, upper_threshold=0.01)


def test_the_label_interval_is_exactly_what_the_row_records() -> None:
    bars = crypto_bars(count=200)
    grid = grid_for(bars)
    spec = LabelSpec(name="fr", kind=LabelKind.FORWARD_RETURN, horizon_bars=4, entry_offset_bars=1)
    frame = build_dataset(bars, spec=DatasetSpec(symbol=CRYPTO_SYMBOL, label=spec), grid=grid).frame

    row = frame.iloc[0]
    assert row["label_entry_timestamp"] == row["feature_timestamp"] + BAR_INTERVAL
    assert row["label_exit_timestamp"] == row["feature_timestamp"] + 5 * BAR_INTERVAL
    assert row["label_knowable_at"] == row["label_exit_timestamp"] + BAR_INTERVAL

    entry = bars.set_index("timestamp").loc[row["label_entry_timestamp"], "open"]
    exit_price = bars.set_index("timestamp").loc[row["label_exit_timestamp"], "open"]
    assert row["label_forward_return"] == pytest.approx(exit_price / entry - 1.0)


def test_the_horizon_end_of_a_dataset_is_unlabelled_not_imputed() -> None:
    bars = crypto_bars(count=200)
    spec = forward_return_spec(horizon=4)
    build = build_crypto(bars, spec)
    tail = build.frame.tail(spec.exit_offset_bars)
    assert not tail["label_valid"].any()
    assert tail["label"].isna().all()
    assert build.labelled_row_count == build.row_count - spec.exit_offset_bars


def test_an_equity_horizon_skips_the_overnight_gap() -> None:
    grid = equity_grid(market_sessions(count=4, early_closes=()))
    bars = equity_bars(grid)
    spec = LabelSpec(name="fr", kind=LabelKind.FORWARD_RETURN, horizon_bars=4)
    labels = compute_labels(build_observations(bars, grid, EQUITY_SYMBOL), grid, spec)

    last_bar = grid.position_of(datetime(2026, 3, 2, 20, 45, tzinfo=UTC))
    # Entry is the first bar of the next session; exit is four tradable bars later.
    assert labels["label_entry_timestamp"].iloc[last_bar] == datetime(
        2026, 3, 3, 14, 30, tzinfo=UTC
    )
    assert labels["label_exit_timestamp"].iloc[last_bar] == datetime(2026, 3, 3, 15, 30, tzinfo=UTC)
    assert bool(labels["label_spans_session_gap"].iloc[last_bar]) is True


def test_a_within_session_policy_refuses_a_gap_crossing_interval() -> None:
    grid = equity_grid(market_sessions(count=8, early_closes=()))
    bars = equity_bars(grid)
    spanning = build_dataset(
        bars,
        spec=DatasetSpec(symbol=EQUITY_SYMBOL, label=forward_return_spec()),
        grid=grid,
    )
    confined = build_dataset(
        bars,
        spec=DatasetSpec(
            symbol=EQUITY_SYMBOL,
            label=LabelSpec(
                name="fr",
                kind=LabelKind.FORWARD_RETURN,
                horizon_bars=4,
                session_policy=SessionPolicy.WITHIN_SESSION,
            ),
        ),
        grid=grid,
    )
    assert confined.labelled_row_count < spanning.labelled_row_count
    kept = confined.frame.loc[confined.frame["label_valid"].fillna(False)]
    assert not kept["label_spans_session_gap"].any()


def test_a_within_session_policy_is_meaningless_on_a_crypto_grid() -> None:
    bars = crypto_bars(count=150)
    grid = grid_for(bars)
    spec = LabelSpec(
        name="fr",
        kind=LabelKind.FORWARD_RETURN,
        horizon_bars=4,
        session_policy=SessionPolicy.WITHIN_SESSION,
    )
    with pytest.raises(LabelError, match="no session to stay within"):
        compute_labels(build_observations(bars, grid, CRYPTO_SYMBOL), grid, spec)


def test_a_volatility_scaled_threshold_uses_a_backward_looking_column() -> None:
    bars = crypto_bars(count=300)
    grid = grid_for(bars)
    spec = LabelSpec(
        name="t",
        kind=LabelKind.TERNARY,
        horizon_bars=4,
        threshold_mode=ThresholdMode.VOLATILITY,
        upper_threshold=0.5,
        lower_threshold=-0.5,
    )
    assert spec.volatility_column == VOLATILITY_FEATURE
    build = build_dataset(bars, spec=DatasetSpec(symbol=CRYPTO_SYMBOL, label=spec), grid=grid)
    classes = set(build.frame["label"].dropna().unique())
    assert classes <= {TERNARY_SELL, 0, TERNARY_BUY}
    assert build.frame["label"].dtype == "Int8"


def test_a_label_documents_its_interval_in_words() -> None:
    described = LabelSpec(
        name="t",
        kind=LabelKind.TERNARY,
        horizon_bars=4,
        upper_threshold=0.002,
        lower_threshold=-0.002,
    ).describe()
    assert "1 grid bar(s) after the feature bar" in described
    assert "5 grid bar(s) after it" in described
    assert "label_knowable_at" in described


def test_a_changed_label_definition_changes_its_fingerprint() -> None:
    assert forward_return_spec(4).fingerprint != forward_return_spec(8).fingerprint
    assert forward_return_spec(4).identifier != forward_return_spec(8).identifier


# --------------------------------------------------------------------------
# The builder
# --------------------------------------------------------------------------


def test_a_build_is_deterministic() -> None:
    bars = crypto_bars(count=300)
    spec = DatasetSpec(symbol=CRYPTO_SYMBOL, label=forward_return_spec())
    first = build_dataset(bars, spec=spec, grid=grid_for(bars))
    second = build_dataset(bars.copy(), spec=spec, grid=grid_for(bars))
    assert first.fingerprint == second.fingerprint
    pd.testing.assert_frame_equal(first.frame, second.frame)


def test_a_different_label_produces_a_different_fingerprint() -> None:
    bars = crypto_bars(count=300)
    grid = grid_for(bars)
    four = build_dataset(
        bars, spec=DatasetSpec(symbol=CRYPTO_SYMBOL, label=forward_return_spec(4)), grid=grid
    )
    eight = build_dataset(
        bars, spec=DatasetSpec(symbol=CRYPTO_SYMBOL, label=forward_return_spec(8)), grid=grid
    )
    assert four.fingerprint != eight.fingerprint


def test_an_invalid_bar_dataset_is_refused_rather_than_repaired() -> None:
    bars = crypto_bars(count=200)
    bars.loc[10, "high"] = bars.loc[10, "low"] - 1.0
    with pytest.raises(DatasetError, match="not valid"):
        build_crypto(bars)


def test_bars_outside_the_grid_are_refused() -> None:
    """An extended-hours candle, or a calendar that does not cover the file."""
    grid = equity_grid(market_sessions(count=2, early_closes=()))
    bars = equity_bars(grid)
    extended = bars.copy()
    extended.loc[len(extended)] = {
        **bars.iloc[-1].to_dict(),
        "timestamp": pd.Timestamp("2026-03-03 21:00", tz="UTC"),
    }
    with pytest.raises(DatasetError, match="outside the equity grid"):
        build_dataset(
            extended.sort_values("timestamp", ignore_index=True),
            spec=DatasetSpec(symbol=EQUITY_SYMBOL, label=forward_return_spec()),
            grid=grid,
        )


def test_a_dataset_declared_equity_refuses_a_crypto_symbol() -> None:
    with pytest.raises(DatasetError, match="crypto symbol but the grid"):
        build_dataset(
            crypto_bars(count=100),
            spec=DatasetSpec(symbol=CRYPTO_SYMBOL, label=forward_return_spec()),
            grid=equity_grid(market_sessions(count=6, early_closes=())),
        )


def test_the_built_frame_matches_the_schema_exactly() -> None:
    bars = crypto_bars(count=200)
    build = build_crypto(bars)
    build.schema.validate_frame(build.frame)
    assert list(build.frame.columns) == list(build.schema.names)


def test_the_labelled_filter_keeps_only_usable_rows() -> None:
    bars = crypto_bars(count=200)
    build = build_crypto(bars)
    usable = labelled_frame(build.frame)
    assert len(usable) == build.labelled_row_count
    assert usable["label"].notna().all()


def test_a_dataset_round_trips_through_parquet(tmp_path: Path) -> None:
    bars = crypto_bars(count=200)
    build = build_crypto(bars)
    artifact = write_dataset(build, output_dir=tmp_path, built_at=T0)
    restored = read_dataset(artifact.parquet_path)
    assert frame_fingerprint(restored) == build.fingerprint
    build.schema.validate_frame(restored)


def test_the_metadata_sidecar_records_how_to_rebuild(tmp_path: Path) -> None:
    import json

    bars = crypto_bars(count=200)
    bars_path = tmp_path / "bars.parquet"
    bars.to_parquet(bars_path, engine="pyarrow", index=False)
    artifact = build_dataset_from_parquet(
        bars_path,
        spec=DatasetSpec(symbol=CRYPTO_SYMBOL, label=forward_return_spec()),
        output_dir=tmp_path,
        built_at=T0,
    )
    metadata = json.loads(artifact.metadata_path.read_text(encoding="utf-8"))
    assert metadata["feature_schema"]["feature_schema_version"] == FEATURE_SCHEMA_VERSION
    assert metadata["dataset_fingerprint"] == artifact.fingerprint
    assert metadata["source_bars"]["sha256"]
    assert "grid bar(s) after the feature bar" in metadata["label_interval"]
    assert metadata["grid"]["has_session_gaps"] is False


def test_the_metadata_sidecar_carries_no_credential(tmp_path: Path) -> None:
    bars = crypto_bars(count=200)
    build = build_crypto(bars)
    artifact = write_dataset(build, output_dir=tmp_path, built_at=T0)
    body = artifact.metadata_path.read_text(encoding="utf-8").lower()
    for forbidden in ("api_key", "secret", "token", "password", "account"):
        assert forbidden not in body, forbidden


def test_a_minimum_window_below_the_feature_window_is_a_deliberate_choice() -> None:
    with pytest.raises(DatasetError, match="between 1 and"):
        DatasetSpec(
            symbol=CRYPTO_SYMBOL,
            label=forward_return_spec(),
            minimum_bars_present_in_window=FEATURE_WINDOW_BARS + 1,
        )


# --------------------------------------------------------------------------
# Temporal splitting
# --------------------------------------------------------------------------


def built_frame(count: int = 900, *, horizon: int = 4) -> pd.DataFrame:
    bars = crypto_bars(count=count)
    return build_dataset(
        bars,
        spec=DatasetSpec(symbol=CRYPTO_SYMBOL, label=forward_return_spec(horizon)),
        grid=grid_for(bars),
    ).frame


def test_a_split_is_ordered_in_time_and_never_shuffled() -> None:
    split = temporal_split(built_frame())
    assert_no_leakage(split)
    for part in split.parts:
        assert part.frame["feature_timestamp"].is_monotonic_increasing
    assert split.train.last_timestamp < split.validation.first_timestamp
    assert split.validation.last_timestamp < split.test.first_timestamp


def test_no_training_label_resolves_inside_the_validation_window() -> None:
    """Purging, which is the correctness rule the whole module exists for."""
    split = temporal_split(built_frame(horizon=8))
    assert split.train.purged_rows > 0
    assert split.train.frame["label_knowable_at"].max() <= split.validation.first_timestamp


def test_no_validation_label_resolves_inside_the_test_window() -> None:
    split = temporal_split(built_frame(horizon=8))
    assert split.validation.frame["label_knowable_at"].max() <= split.test.first_timestamp


def test_an_embargo_removes_further_bars_before_each_boundary() -> None:
    """The embargo widens the exclusion zone; it does not re-remove purged rows.

    With a one-bar horizon the purge already drops the two rows whose labels
    reach past the boundary, so a 20-bar embargo drops the eighteen behind
    them - and the surviving gap between the last training bar and the first
    validation bar is more than twenty grid positions either way.
    """
    frame = built_frame(horizon=1)
    without = temporal_split(frame, SplitSpec(embargo_bars=0, snap_to_session=False))
    with_embargo = temporal_split(frame, SplitSpec(embargo_bars=20, snap_to_session=False))

    removed_without = without.train.purged_rows + without.train.embargoed_rows
    removed_with = with_embargo.train.purged_rows + with_embargo.train.embargoed_rows
    assert removed_without == 2
    assert removed_with == 20
    assert with_embargo.train.row_count == without.train.row_count - (20 - removed_without)

    boundary = int(with_embargo.validation.frame["grid_index"].iloc[0])
    last_training = int(with_embargo.train.frame["grid_index"].iloc[-1])
    assert boundary - last_training > 20


def test_boundaries_snap_so_a_session_is_never_divided() -> None:
    split = temporal_split(built_frame(), SplitSpec(snap_to_session=True))
    train_sessions = set(split.train.frame["session_id"])
    test_sessions = set(split.test.frame["session_id"])
    assert not train_sessions & test_sessions


def test_unlabelled_rows_are_excluded_and_counted() -> None:
    frame = built_frame(horizon=4)
    split = temporal_split(frame)
    assert split.unlabelled_rows == int((~frame["label_valid"].fillna(False)).sum())
    for part in split.parts:
        assert part.frame["label_valid"].all()


def test_the_leakage_assertion_catches_a_leak_it_is_handed() -> None:
    split = temporal_split(built_frame())
    leaking = split.train.frame.copy()
    leaking.loc[leaking.index[-1], "label_knowable_at"] = split.test.first_timestamp
    broken = type(split)(
        spec=split.spec,
        train=type(split.train)("train", leaking, 0, 0),
        validation=split.validation,
        test=split.test,
        unlabelled_rows=split.unlabelled_rows,
    )
    with pytest.raises(SplitError, match="Purging failed"):
        assert_no_leakage(broken)


def test_a_frame_out_of_time_order_is_refused_rather_than_sorted() -> None:
    frame = built_frame().iloc[::-1].reset_index(drop=True)
    with pytest.raises(SplitError, match="does not sort its input"):
        temporal_split(frame)


def test_split_fractions_must_leave_a_test_set() -> None:
    with pytest.raises(SplitError, match="room for a test set"):
        SplitSpec(train_fraction=0.8, validation_fraction=0.3)


def test_walk_forward_folds_train_on_the_past_and_are_graded_on_their_own_future() -> None:
    folds = walk_forward_folds(built_frame(horizon=8), folds=4, embargo_bars=5)
    assert len(folds) == 4
    previous_end = None
    for fold in folds:
        assert fold.train.frame["label_knowable_at"].max() <= fold.test.first_timestamp
        assert fold.train.last_timestamp < fold.test.first_timestamp
        if previous_end is not None:
            assert fold.test.first_timestamp > previous_end
        previous_end = fold.test.last_timestamp


def test_walk_forward_refuses_more_folds_than_the_data_supports() -> None:
    with pytest.raises(SplitError, match="cannot yield"):
        walk_forward_folds(built_frame(count=200), folds=500)


# --------------------------------------------------------------------------
# Offline guarantees
# --------------------------------------------------------------------------


def test_a_dataset_builds_with_every_socket_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the ML foundation must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    bars = crypto_bars(count=200)
    build = build_crypto(bars)
    assert build.row_count > 0


def test_a_dataset_builds_with_no_credential_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    bars = crypto_bars(count=200)
    assert build_crypto(bars).row_count > 0


def test_the_builder_names_no_market_data_client() -> None:
    source = code_without_prose(inspect.getsource(dataset_module))
    for forbidden in ("alpaca", "Client", "requests", "urllib", "http"):
        assert forbidden not in source, forbidden
