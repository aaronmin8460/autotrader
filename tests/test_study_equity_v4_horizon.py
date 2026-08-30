"""Tests for the equity V4 label/horizon study harness.

Everything runs offline on constructed sessions and synthetic bars whose
answers are known in advance. The study's conclusions come from real data; the
*rules* it applies - session-aware forward indexing at every horizon, the
purge and embargo that must scale with the label overlap, the calibration
provenance, the checkpoint resume - are properties these fixtures pin down.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from studies.equity_v1_v5.calendar import SessionRecord, SnapshotCalendar
from studies.equity_v1_v5.windows import EMBARGO_BARS, ScoringWindow
from studies.equity_v4_horizon.calibration_audit import (
    audit_artifact_calibration,
    step_index,
)
from studies.equity_v4_horizon.checkpoint import (
    CheckpointError,
    cell_path,
    is_complete,
    read_cell,
    write_cell,
)
from studies.equity_v4_horizon.evaluation import (
    common_valid_timestamps,
    evaluate_models,
    require_aligned,
    score_artifact,
    window_rows,
)
from studies.equity_v4_horizon.horizons import (
    HorizonError,
    label_spec_for,
    outer_gap_bars,
    overlap_factor,
    require_study_horizon,
)
from studies.equity_v4_horizon.walkforward import (
    assert_gap_respected,
    train_cell,
    training_frame_for,
)

from autotrader.decision.probability import (
    FAMILY_CLASS_FREQUENCY,
    FAMILY_GRADIENT_BOOSTED,
    FAMILY_LOGISTIC,
    V4_FEATURE_COLUMNS,
    FeatureStandardizer,
    IsotonicCalibration,
    LogisticEstimator,
    ProbabilityArtifact,
    TrainingWindow,
    artifact_from_record,
)
from autotrader.equity.session import MarketSession, regular_session_bar_starts
from autotrader.ml.dataset import build_observations
from autotrader.ml.grid import equity_grid
from autotrader.ml.labels import compute_labels
from autotrader.ml.splits import walk_forward_folds
from autotrader.ml.v4 import (
    Candidate,
    build_training_frame,
    train_model,
)

WINTER_OPEN_UTC = 14  # 09:30 New York is 14:30 UTC under EST.
SUMMER_OPEN_UTC = 13  # ...and 13:30 UTC under EDT.


def _session(day: date, *, open_hour: int, close_hour: int) -> MarketSession:
    return MarketSession(
        session_date=day,
        open_utc=datetime(day.year, day.month, day.day, open_hour, 30, tzinfo=UTC),
        close_utc=datetime(day.year, day.month, day.day, close_hour, 0, tzinfo=UTC),
    )


def winter_session(day: date) -> MarketSession:
    return _session(day, open_hour=WINTER_OPEN_UTC, close_hour=21)


def summer_session(day: date) -> MarketSession:
    return _session(day, open_hour=SUMMER_OPEN_UTC, close_hour=20)


def early_close_session(day: date) -> MarketSession:
    return _session(day, open_hour=WINTER_OPEN_UTC, close_hour=18)


def weekday_sessions(first: date, count: int) -> list[MarketSession]:
    """`count` consecutive full winter sessions on weekdays, skipping weekends."""
    sessions: list[MarketSession] = []
    day = first
    while len(sessions) < count:
        if day.weekday() < 5:
            sessions.append(winter_session(day))
        day += timedelta(days=1)
    return sessions


def bars_for(sessions, *, symbol: str = "SPY", seed: int = 7) -> pd.DataFrame:
    """Regular-session bars over `sessions` with a seeded, two-sided price path."""
    generator = np.random.default_rng(seed)
    rows = []
    price = 100.0
    for session in sessions:
        for moment in regular_session_bar_starts(session):
            price = max(1.0, price + float(generator.normal(0.0, 0.05)))
            rows.append(
                {
                    "timestamp": moment,
                    "symbol": symbol,
                    "open": price,
                    "high": price + 0.05,
                    "low": price - 0.05,
                    "close": price + float(generator.normal(0.0, 0.02)),
                    "volume": 1000.0 + float(generator.integers(0, 500)),
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


def labels_on(sessions, horizon: int, *, symbol: str = "SPY") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Labelled observations for synthetic bars over `sessions` at one horizon."""
    frame = bars_for(sessions, symbol=symbol)
    grid = equity_grid(sessions)
    observations = build_observations(frame, grid, symbol)
    labels = compute_labels(observations, grid, label_spec_for(horizon))
    return observations, labels


# --------------------------------------------------------------------------
# The frozen horizon set
# --------------------------------------------------------------------------


def test_frozen_horizon_set_refuses_expansion():
    for horizon in (4, 8, 16, 26):
        assert require_study_horizon(horizon) == horizon
    for horizon in (1, 2, 6, 12, 32, 52):
        with pytest.raises(HorizonError):
            require_study_horizon(horizon)


def test_outer_gap_and_overlap_scale_with_horizon():
    assert outer_gap_bars(4) == 30  # the pilot's own value at the shipped horizon
    assert outer_gap_bars(26) == 52
    assert [overlap_factor(h) for h in (4, 8, 16, 26)] == [4, 8, 16, 26]


# --------------------------------------------------------------------------
# Session-aware forward indexing at every boundary the calendar produces
# --------------------------------------------------------------------------


def test_weekend_boundary_exit_lands_in_next_session():
    friday = winter_session(date(2025, 1, 17))
    monday = winter_session(date(2025, 1, 20))
    observations, labels = labels_on([friday, monday], 4)
    feature = observations.index[observations["timestamp"] == pd.Timestamp("2025-01-17T20:45:00Z")][
        0
    ]
    row = labels.iloc[feature]
    assert row["label_valid"]
    # Entry is Monday's opening bar; exit is 4 session bars later, 62.5
    # wall-clock hours after the feature bar rather than one.
    assert row["label_entry_timestamp"] == pd.Timestamp("2025-01-20T14:30:00Z")
    assert row["label_exit_timestamp"] == pd.Timestamp("2025-01-20T15:30:00Z")
    assert bool(row["label_spans_session_gap"])


def test_holiday_boundary_steps_over_the_missing_session():
    friday = winter_session(date(2025, 1, 17))
    tuesday = winter_session(date(2025, 1, 21))  # Monday is a holiday: no session.
    observations, labels = labels_on([friday, tuesday], 4)
    feature = observations.index[observations["timestamp"] == pd.Timestamp("2025-01-17T20:45:00Z")][
        0
    ]
    row = labels.iloc[feature]
    assert row["label_exit_timestamp"] == pd.Timestamp("2025-01-21T15:30:00Z")
    assert bool(row["label_spans_session_gap"])
    assert not any(
        pd.Timestamp("2025-01-18") <= ts.tz_localize(None) < pd.Timestamp("2025-01-21")
        for ts in observations["timestamp"]
    )


def test_early_close_boundary_counts_session_bars_not_wall_clock():
    wednesday = early_close_session(date(2024, 11, 27))  # 14 bars, 14:30-18:00 UTC
    thursday = winter_session(date(2024, 11, 28))
    observations, labels = labels_on([wednesday, thursday], 4)
    feature = observations.index[observations["timestamp"] == pd.Timestamp("2024-11-27T17:00:00Z")][
        0
    ]
    row = labels.iloc[feature]
    # 17:00 + 4 bars of wall clock would be 18:00 - a bar that does not exist.
    # Session-aware indexing exits on Thursday's second bar instead.
    assert row["label_exit_timestamp"] == pd.Timestamp("2024-11-28T14:45:00Z")
    assert bool(row["label_spans_session_gap"])


def test_dst_boundary_spring_forward_shifts_the_utc_window():
    friday = winter_session(date(2025, 3, 7))  # EST: opens 14:30 UTC
    monday = summer_session(date(2025, 3, 10))  # EDT: opens 13:30 UTC
    observations, labels = labels_on([friday, monday], 4)
    feature = observations.index[observations["timestamp"] == pd.Timestamp("2025-03-07T20:45:00Z")][
        0
    ]
    row = labels.iloc[feature]
    assert row["label_entry_timestamp"] == pd.Timestamp("2025-03-10T13:30:00Z")
    assert row["label_exit_timestamp"] == pd.Timestamp("2025-03-10T14:30:00Z")
    assert bool(row["label_spans_session_gap"])


def test_full_session_horizon_spans_a_gap_on_every_row():
    sessions = weekday_sessions(date(2025, 1, 6), 4)
    _, labels = labels_on(sessions, 26)
    valid = labels.loc[labels["label_valid"].fillna(False).astype(bool)]
    assert len(valid) > 0
    assert valid["label_spans_session_gap"].astype(bool).all()


@pytest.mark.parametrize("horizon", [4, 8, 16])
def test_interior_sessions_span_exactly_h_plus_one_rows(horizon):
    sessions = weekday_sessions(date(2025, 1, 6), 6)
    observations, labels = labels_on(sessions, horizon)
    interior = sessions[2].session_date.isoformat()
    inside = labels.loc[(observations["session_id"] == interior).to_numpy()]
    valid = inside.loc[inside["label_valid"].fillna(False).astype(bool)]
    spanning = int(valid["label_spans_session_gap"].astype(bool).sum())
    assert len(valid) == 26
    assert spanning == horizon + 1


# --------------------------------------------------------------------------
# Overlapping-label purge and embargo, scaling with the horizon
# --------------------------------------------------------------------------


@pytest.mark.parametrize("horizon", [4, 8, 16, 26])
def test_purge_and_embargo_scale_exactly_with_the_horizon(horizon):
    sessions = weekday_sessions(date(2025, 1, 6), 40)
    frame = bars_for(sessions)
    training = build_training_frame(
        frame, grid=equity_grid(sessions), label=label_spec_for(horizon)
    )
    folds = walk_forward_folds(
        training.frame, folds=2, initial_train_fraction=0.6, embargo_bars=EMBARGO_BARS
    )
    assert folds
    for fold in folds:
        boundary = fold.test.frame["feature_timestamp"].iloc[0]
        # Correctness: no surviving training label resolves at or after the
        # first test bar.
        knowable = fold.train.frame["label_knowable_at"]
        assert (knowable <= boundary).all()
        # Exactness on a gapless synthetic frame: the h+1 rows whose labels
        # read the boundary bar or later are purged, and the embargo removes
        # whatever the purge left inside one full session of the boundary.
        assert fold.train.purged_rows == horizon + 1
        assert fold.train.embargoed_rows == max(0, EMBARGO_BARS - (horizon + 1))


def test_outer_gap_keeps_every_training_label_before_the_window():
    sessions = weekday_sessions(date(2025, 1, 6), 40)
    frame = bars_for(sessions)
    calendar = calendar_for(sessions)
    window = ScoringWindow(
        name="synthetic",
        start=sessions[-3].session_date,
        end=sessions[-1].session_date,
        covers="test window",
    )
    first_scored, _ = window.positions(frame)
    for horizon in (4, 26):
        gap = outer_gap_bars(horizon)
        training = training_frame_for(
            frame,
            calendar,
            symbol="SPY",
            last_row=first_scored - gap - 1,
            horizon_bars=horizon,
        )
        labelled = training.frame.loc[training.frame["label_valid"].fillna(False).astype(bool)]
        window_open = frame["timestamp"].iloc[first_scored]
        assert labelled["label_knowable_at"].max() <= window_open
        # The last bar training could read at all sits a full session or more
        # before the window opens.
        last_read = training.frame["feature_timestamp"].max()
        separation = (
            int((frame["timestamp"] >= last_read).sum() - (frame["timestamp"] > window_open).sum())
            - 1
        )
        assert separation >= EMBARGO_BARS


# --------------------------------------------------------------------------
# Features never read the future
# --------------------------------------------------------------------------


def test_features_are_identical_when_the_future_is_removed():
    sessions = weekday_sessions(date(2025, 1, 6), 12)
    frame = bars_for(sessions)
    calendar = calendar_for(sessions)
    full = training_frame_for(
        frame, calendar, symbol="SPY", last_row=len(frame) - 1, horizon_bars=4
    )
    cut = len(frame) - 40
    truncated = training_frame_for(frame, calendar, symbol="SPY", last_row=cut - 1, horizon_bars=4)
    merged = truncated.frame.merge(full.frame, on="feature_timestamp", suffixes=("_cut", "_full"))
    assert len(merged) == len(truncated.frame)
    for column in V4_FEATURE_COLUMNS:
        assert np.allclose(
            merged[f"{column}_cut"].to_numpy(dtype="float64"),
            merged[f"{column}_full"].to_numpy(dtype="float64"),
            atol=0.0,
            rtol=0.0,
        ), f"{column} changed when future bars were removed"


# --------------------------------------------------------------------------
# Cross-horizon evaluation alignment
# --------------------------------------------------------------------------


def test_evaluation_frames_align_and_common_subset_drops_the_long_tail():
    sessions = weekday_sessions(date(2025, 1, 6), 12)
    frame = bars_for(sessions)
    calendar = calendar_for(sessions)
    frames = {
        horizon: training_frame_for(
            frame, calendar, symbol="SPY", last_row=len(frame) - 1, horizon_bars=horizon
        )
        for horizon in (4, 26)
    }
    require_aligned(frames)
    common = common_valid_timestamps(frames)
    valid_h4 = int(frames[4].frame["label_valid"].fillna(False).sum())
    valid_h26 = int(frames[26].frame["label_valid"].fillna(False).sum())
    assert valid_h26 < valid_h4
    assert len(common) == valid_h26
    window = ScoringWindow(
        name="tail",
        start=sessions[-2].session_date,
        end=sessions[-1].session_date,
        covers="the frame tail",
    )
    full_rows = window_rows(frames[4], window)
    common_rows = window_rows(frames[4], window, restrict_to=common)
    assert len(common_rows) < len(full_rows)


# --------------------------------------------------------------------------
# Calibration provenance
# --------------------------------------------------------------------------


def _artifact_with(calibration) -> ProbabilityArtifact:
    width = len(V4_FEATURE_COLUMNS)
    return ProbabilityArtifact(
        model_version="test-artifact",
        feature_version="test-features",
        feature_columns=V4_FEATURE_COLUMNS,
        label_spec_id="test-label",
        standardizer=FeatureStandardizer(means=(0.0,) * width, scales=(1.0,) * width),
        estimator=LogisticEstimator(intercept=0.0, coefficients=(1.0,) + (0.0,) * (width - 1)),
        calibration=calibration,
        training_window=TrainingWindow(
            first_feature_timestamp="2025-01-01T00:00:00+00:00",
            last_feature_timestamp="2025-01-02T00:00:00+00:00",
            rows=10,
            symbols=("SPY",),
            asset_class="us_equity",
        ),
        trained_at_utc="",
        code_revision={},
        hyperparameters={},
        metrics={},
        seed=0,
        notes="",
    )


def _validation_frame(first_feature: np.ndarray, labels: np.ndarray) -> pd.DataFrame:
    frame = pd.DataFrame({name: np.zeros(len(first_feature)) for name in V4_FEATURE_COLUMNS})
    frame[V4_FEATURE_COLUMNS[0]] = first_feature
    frame["label"] = pd.array(labels, dtype="Int8")
    return frame


def test_step_index_matches_the_shipped_apply_rule():
    calibration = IsotonicCalibration(thresholds=(0.2, 0.5, 0.8), values=(0.005, 0.5, 0.995))
    for score in np.linspace(0.0, 1.0, 101):
        expected = calibration.apply(float(score))
        assert calibration.values[step_index(calibration.thresholds, float(score))] == expected


def test_calibration_audit_flags_extremes_from_thin_bins():
    calibration = IsotonicCalibration(thresholds=(0.2, 0.5, 0.8), values=(0.005, 0.5, 0.995))
    artifact = _artifact_with(calibration)
    # Two rows land in the extreme top step (raw score sigmoid(2.2) ~ 0.9),
    # far fewer than the 30 the design requires.
    features = np.concatenate([np.full(40, 0.0), np.full(2, 2.2)])
    labels = np.concatenate([np.zeros(40), np.ones(2)]).astype("int8")
    audit = audit_artifact_calibration(artifact, _validation_frame(features, labels))
    assert audit["distinct_levels"] == 3
    assert audit["extreme_from_thin_bins"] is True
    top = audit["steps"][-1]
    assert top["validation_support"] == 2
    assert top["extreme"] is True


def test_calibration_audit_accepts_extremes_with_real_support():
    calibration = IsotonicCalibration(thresholds=(0.2, 0.5, 0.8), values=(0.005, 0.5, 0.995))
    artifact = _artifact_with(calibration)
    features = np.concatenate([np.full(30, -0.85), np.full(40, 0.0), np.full(35, 2.2)])
    labels = np.concatenate([np.zeros(30), np.zeros(40), np.ones(35)]).astype("int8")
    audit = audit_artifact_calibration(artifact, _validation_frame(features, labels))
    assert audit["steps"][0]["validation_support"] == 30
    assert audit["steps"][-1]["validation_support"] == 35
    assert audit["extreme_from_thin_bins"] is False


# --------------------------------------------------------------------------
# Checkpoints: resume skips finished work and cannot duplicate it
# --------------------------------------------------------------------------


def test_checkpoint_round_trip_and_resume(tmp_path):
    path = cell_path(tmp_path, symbol="SPY", window="2021-autumn", horizon_bars=8)
    assert not is_complete(path)
    write_cell(path, {"selected_family": "logistic"})
    assert is_complete(path)
    loaded = read_cell(path)
    assert loaded["selected_family"] == "logistic"
    assert loaded["complete"] is True
    # A finished cell is never overwritten - resume must skip, not redo.
    with pytest.raises(CheckpointError):
        write_cell(path, {"selected_family": "gradient_boosted"})


def test_checkpoint_treats_a_torn_write_as_unfinished(tmp_path):
    path = cell_path(tmp_path, symbol="SPY", window="2021-autumn", horizon_bars=8)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"selected_family": "logistic"', encoding="utf-8")  # truncated JSON
    assert not is_complete(path)
    path.write_text('{"selected_family": "logistic"}', encoding="utf-8")  # no stamp
    assert not is_complete(path)


# --------------------------------------------------------------------------
# Determinism, and the full cell path
# --------------------------------------------------------------------------


def test_train_model_is_deterministic():
    sessions = weekday_sessions(date(2025, 1, 6), 30)
    frame = bars_for(sessions)
    calendar = calendar_for(sessions)
    training = training_frame_for(
        frame, calendar, symbol="SPY", last_row=len(frame) - 1, horizon_bars=8
    )
    candidate = Candidate(name="logistic-l2", family=FAMILY_LOGISTIC, hyperparameters={"l2": 1.0})
    stamp = datetime(2026, 8, 29, tzinfo=UTC)
    first = train_model(
        training, candidate, model_version="determinism-check", seed=0, trained_at=stamp
    )
    second = train_model(
        training, candidate, model_version="determinism-check", seed=0, trained_at=stamp
    )
    assert first.artifact.to_record() == second.artifact.to_record()


def test_train_cell_end_to_end_on_synthetic_bars(tmp_path):
    sessions = weekday_sessions(date(2024, 6, 3), 70)
    frame = bars_for(sessions)
    calendar = calendar_for(sessions)
    window = ScoringWindow(
        name="synthetic",
        start=sessions[-3].session_date,
        end=sessions[-1].session_date,
        covers="the last three synthetic sessions",
    )
    cell = train_cell(frame, calendar, window, symbol="SPY", horizon_bars=8)
    assert cell.gap_bars == 34
    assert cell.horizon_bars == 8
    assert not assert_gap_respected(cell, frame)
    assert cell.selected_family in {
        FAMILY_CLASS_FREQUENCY,
        FAMILY_LOGISTIC,
        FAMILY_GRADIENT_BOOSTED,
    }
    assert set(cell.shadow_artifacts) == {
        FAMILY_CLASS_FREQUENCY,
        FAMILY_LOGISTIC,
        FAMILY_GRADIENT_BOOSTED,
    } - {cell.selected_family}
    assert len(cell.calibration_audits) == 3

    # The stored record round-trips through JSON, and the rebuilt artifact
    # reproduces the live computation exactly on sampled rows.
    payload = cell.to_json_dict()
    path = cell_path(tmp_path, symbol="SPY", window=window.name, horizon_bars=8)
    write_cell(path, payload)
    stored = read_cell(path)
    rebuilt = artifact_from_record(stored["selected_artifact"])
    evaluation = training_frame_for(
        frame, calendar, symbol="SPY", last_row=len(frame) - 1, horizon_bars=8
    )
    rows = window_rows(evaluation, window).head(16)
    fresh = score_artifact(cell.selected_artifact, rows)
    replayed = score_artifact(rebuilt, rows)
    assert np.array_equal(fresh, replayed)

    # And the out-of-sample evaluation carries the null under its own name.
    result = evaluate_models(rows, {"selected": cell.selected_artifact, "null": cell.null_artifact})
    assert set(result["models"]) == {"selected", "null"}
    assert result["models"]["null"]["log_loss_gain_vs_null"] == 0.0
