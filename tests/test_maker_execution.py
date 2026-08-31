"""Tests for the crypto maker-execution feasibility pilot (research only)."""

from __future__ import annotations

import ast
import json
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest
from studies.crypto_maker_execution import accounting, bars, schedule, simulator, venue
from studies.crypto_maker_execution.simulator import (
    FULLY_FILLED,
    NOT_FILLED,
    PARTIALLY_FILLED,
    PRICE_MOVED_AWAY,
    SCENARIOS,
    TIMED_OUT,
    simulate_limit,
)

STUDY_ROOT = Path(__file__).resolve().parents[1] / "studies" / "crypto_maker_execution"

T0 = pd.Timestamp("2025-06-15T00:00:00Z")


def quotes_frame(rows: list[tuple[str, float, float, float, float]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=["t", "bid_price", "bid_size", "ask_price", "ask_size"])
    frame["t"] = pd.to_datetime(frame["t"], utc=True)
    return frame


def trades_frame(rows: list[tuple[str, float, float, str]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=["t", "price", "size", "taker_side"])
    frame["t"] = pd.to_datetime(frame["t"], utc=True)
    return frame


STEADY_QUOTES = quotes_frame(
    [
        ("2025-06-14T23:59:30Z", 100_000.0, 1.0, 100_100.0, 1.0),
        ("2025-06-15T00:10:00Z", 100_000.0, 1.0, 100_100.0, 1.0),
        ("2025-06-15T00:40:00Z", 100_000.0, 1.0, 100_100.0, 1.0),
    ]
)


# ---------------------------------------------------------------------------
# No broker mutation
# ---------------------------------------------------------------------------


def test_the_study_names_no_order_mutation_and_imports_no_trading_module() -> None:
    forbidden_names = (
        "submit_order",
        "place_order",
        "cancel_order",
        "replace_order",
        "TradingClient",
        "OrderRequest",
    )
    forbidden_imports = ("alpaca", "autotrader.execution", "autotrader.runtime")
    for module in sorted(STUDY_ROOT.rglob("*.py")):
        source = module.read_text(encoding="utf-8")
        for name in forbidden_names:
            assert name not in source, f"{module.name} names {name}"
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for imported in names:
                for banned in forbidden_imports:
                    assert not imported.startswith(banned), f"{module.name} imports {imported}"


def test_the_acquire_module_only_touches_allowlisted_data_paths() -> None:
    from studies.crypto_maker_execution import acquire

    assert all(path.startswith("/v1beta3/crypto/us/") for path in acquire.ALLOWED_PATHS)
    with pytest.raises(ValueError):
        acquire._fetch_paged("/v2/orders", "BTC/USD", "a", "b")


# ---------------------------------------------------------------------------
# Fee arithmetic and maker/taker classification
# ---------------------------------------------------------------------------


def test_taker_break_even_reproduces_the_shipped_60_18_bps() -> None:
    assert venue.taker_baseline_break_even() * 1e4 == pytest.approx(60.18, abs=0.01)


def test_maker_fee_only_round_trip_is_30_05_bps() -> None:
    assert venue.maker_fee_only_break_even() * 1e4 == pytest.approx(30.05, abs=0.01)


def test_maker_leg_pays_maker_fee_and_fallback_pays_taker_fee(monkeypatch) -> None:
    monkeypatch.setattr(bars, "close_at_or_before", lambda *a, **k: 100_050.0)
    monkeypatch.setattr(bars, "reference_close_24h", lambda *a, **k: 100_050.0)
    monkeypatch.setattr(bars, "limit_retouched_within_24h", lambda *a, **k: (False, None))
    monkeypatch.setattr(
        bars, "trailing_context", lambda *a, **k: {"realized_vol_24h": None, "trend_14d": None}
    )
    trades = trades_frame([("2025-06-15T00:02:00Z", 99_998.0, 1.0, "S")])
    filled = accounting.account_event(
        symbol="BTC/USD",
        decision_ts=T0,
        quarter="2025Q2",
        side="buy",
        notional=10_000.0,
        quotes=STEADY_QUOTES,
        trades=trades,
        policy=simulator.POLICIES["P3_FALLBACK"],
        scenario=SCENARIOS["BASE"],
    )
    assert filled["outcome"] == FULLY_FILLED
    assert filled["maker_leg_cost_bps"] == pytest.approx(15.0 + filled["maker_shortfall_bps"])
    unfilled = accounting.account_event(
        symbol="BTC/USD",
        decision_ts=T0,
        quarter="2025Q2",
        side="buy",
        notional=10_000.0,
        quotes=STEADY_QUOTES,
        trades=trades_frame([]),
        policy=simulator.POLICIES["P3_FALLBACK"],
        scenario=SCENARIOS["BASE"],
    )
    # Fallback crosses the spread at the taker rate: 25 bps + half-spread
    # (ask 100,100 against mid 100,050 is +4.9975 bps).
    assert unfilled["fallback_cost_bps"] == pytest.approx(
        25.0 + (100_100.0 - 100_050.0) / 100_050.0 * 1e4
    )
    assert unfilled["completed_one_way_bps"] == pytest.approx(unfilled["fallback_cost_bps"])


# ---------------------------------------------------------------------------
# Spread calculation
# ---------------------------------------------------------------------------


def test_spread_is_quoted_in_bps_of_mid() -> None:
    quote = STEADY_QUOTES.iloc[0]
    expected = (100_100.0 - 100_000.0) / 100_050.0 * 1e4
    assert simulator.spread_bps_of(quote) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Fill model
# ---------------------------------------------------------------------------


def test_at_touch_fills_on_a_touch_but_strict_through_does_not() -> None:
    touch_only = trades_frame([("2025-06-15T00:01:00Z", 100_000.0, 0.5, "S")])
    common = {
        "quotes": STEADY_QUOTES,
        "side": "buy",
        "limit_price": 100_000.0,
        "quantity": 0.1,
        "active_from": T0,
        "cancel_at": T0 + timedelta(minutes=30),
    }
    optimistic = simulate_limit(trades=touch_only, scenario=SCENARIOS["OPTIMISTIC"], **common)
    assert optimistic.outcome == FULLY_FILLED
    base = simulate_limit(trades=touch_only, scenario=SCENARIOS["BASE"], **common)
    assert base.outcome == TIMED_OUT
    through = trades_frame([("2025-06-15T00:01:00Z", 99_999.0, 0.5, "S")])
    base_through = simulate_limit(trades=through, scenario=SCENARIOS["BASE"], **common)
    assert base_through.outcome == FULLY_FILLED
    assert base_through.filled_quantity == pytest.approx(0.1)


def test_fills_before_activation_latency_do_not_count() -> None:
    early = trades_frame([("2025-06-15T00:00:02Z", 99_999.0, 1.0, "S")])
    result = simulate_limit(
        trades=early,
        quotes=STEADY_QUOTES,
        side="buy",
        limit_price=100_000.0,
        quantity=0.1,
        active_from=T0 + timedelta(seconds=5),
        cancel_at=T0 + timedelta(minutes=30),
        scenario=SCENARIOS["BASE"],
    )
    assert result.outcome == TIMED_OUT


def test_sell_side_mirrors_the_buy_rule() -> None:
    through = trades_frame([("2025-06-15T00:01:00Z", 100_101.0, 0.5, "B")])
    result = simulate_limit(
        trades=through,
        quotes=STEADY_QUOTES,
        side="sell",
        limit_price=100_100.0,
        quantity=0.1,
        active_from=T0,
        cancel_at=T0 + timedelta(minutes=30),
        scenario=SCENARIOS["BASE"],
    )
    assert result.outcome == FULLY_FILLED


# ---------------------------------------------------------------------------
# Partial fills
# ---------------------------------------------------------------------------


def test_partial_fill_reports_fraction_and_wait_times() -> None:
    through = trades_frame(
        [
            ("2025-06-15T00:05:00Z", 99_999.0, 0.04, "S"),
            ("2025-06-15T00:20:00Z", 99_998.0, 0.03, "S"),
        ]
    )
    result = simulate_limit(
        trades=through,
        quotes=STEADY_QUOTES,
        side="buy",
        limit_price=100_000.0,
        quantity=0.1,
        active_from=T0,
        cancel_at=T0 + timedelta(minutes=30),
        scenario=SCENARIOS["BASE"],
    )
    assert result.outcome == PARTIALLY_FILLED
    assert result.filled_quantity == pytest.approx(0.07)
    assert result.fill_fraction == pytest.approx(0.7)
    assert result.wait_to_first_fill_s == pytest.approx(300.0)
    assert result.wait_to_full_fill_s is None


def test_conservative_scenario_credits_half_the_printed_size() -> None:
    through = trades_frame([("2025-06-15T00:05:00Z", 99_999.0, 0.1, "S")])
    result = simulate_limit(
        trades=through,
        quotes=STEADY_QUOTES,
        side="buy",
        limit_price=100_000.0,
        quantity=0.1,
        active_from=T0,
        cancel_at=T0 + timedelta(minutes=30),
        scenario=SCENARIOS["CONSERVATIVE"],
    )
    assert result.outcome == PARTIALLY_FILLED
    assert result.filled_quantity == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Zero-fill outcomes
# ---------------------------------------------------------------------------


def test_timed_out_when_price_stays_near_the_limit() -> None:
    result = simulate_limit(
        trades=trades_frame([]),
        quotes=STEADY_QUOTES,
        side="buy",
        limit_price=100_000.0,
        quantity=0.1,
        active_from=T0,
        cancel_at=T0 + timedelta(minutes=30),
        scenario=SCENARIOS["BASE"],
    )
    assert result.outcome == TIMED_OUT


def test_price_moved_away_when_mid_leaves_the_limit_behind() -> None:
    moved = quotes_frame(
        [
            ("2025-06-14T23:59:30Z", 100_000.0, 1.0, 100_100.0, 1.0),
            ("2025-06-15T00:20:00Z", 100_500.0, 1.0, 100_600.0, 1.0),
        ]
    )
    result = simulate_limit(
        trades=trades_frame([]),
        quotes=moved,
        side="buy",
        limit_price=100_000.0,
        quantity=0.1,
        active_from=T0,
        cancel_at=T0 + timedelta(minutes=30),
        scenario=SCENARIOS["BASE"],
    )
    assert result.outcome == PRICE_MOVED_AWAY


def test_not_filled_when_no_quote_exists_to_classify() -> None:
    result = simulate_limit(
        trades=trades_frame([]),
        quotes=quotes_frame([]),
        side="buy",
        limit_price=100_000.0,
        quantity=0.1,
        active_from=T0,
        cancel_at=T0 + timedelta(minutes=30),
        scenario=SCENARIOS["BASE"],
    )
    assert result.outcome == NOT_FILLED


# ---------------------------------------------------------------------------
# Markout / adverse selection
# ---------------------------------------------------------------------------


def test_adverse_selection_markout_signs_for_both_sides() -> None:
    falling = quotes_frame(
        [
            ("2025-06-14T23:59:30Z", 100_000.0, 1.0, 100_100.0, 1.0),
            ("2025-06-15T00:05:30Z", 99_800.0, 1.0, 99_900.0, 1.0),
        ]
    )
    through = trades_frame([("2025-06-15T00:05:00Z", 99_999.0, 1.0, "S")])
    result = simulate_limit(
        trades=through,
        quotes=falling,
        side="buy",
        limit_price=100_000.0,
        quantity=0.1,
        active_from=T0,
        cancel_at=T0 + timedelta(minutes=30),
        scenario=SCENARIOS["BASE"],
    )
    markouts = accounting.weighted_fill_mid_markouts(falling, result, "buy")
    # Mid falls 200 dollars after our buy fill: adverse, so positive.
    assert markouts["adverse_1m_bps"] == pytest.approx((100_050.0 - 99_850.0) / 100_050.0 * 1e4)
    sell_markouts = accounting.weighted_fill_mid_markouts(falling, result, "sell")
    assert sell_markouts["adverse_1m_bps"] == pytest.approx(-markouts["adverse_1m_bps"])


# ---------------------------------------------------------------------------
# Missed-fill accounting
# ---------------------------------------------------------------------------


def test_missed_buy_before_a_rally_is_charged_positive_opportunity_cost(
    monkeypatch,
) -> None:
    monkeypatch.setattr(bars, "reference_close_24h", lambda *a, **k: 101_050.5)
    monkeypatch.setattr(bars, "limit_retouched_within_24h", lambda *a, **k: (False, None))
    monkeypatch.setattr(
        bars, "trailing_context", lambda *a, **k: {"realized_vol_24h": None, "trend_14d": None}
    )
    record = accounting.account_event(
        symbol="BTC/USD",
        decision_ts=T0,
        quarter="2025Q2",
        side="buy",
        notional=10_000.0,
        quotes=STEADY_QUOTES,
        trades=trades_frame([]),
        policy=simulator.POLICIES["P0_JOIN"],
        scenario=SCENARIOS["BASE"],
    )
    # Decision mid 100,050; 24h close 101,050.5 = +100 bps missed on a buy.
    assert record["missed_opportunity_bps"] == pytest.approx(100.0, abs=0.01)
    assert record["limit_retouched_24h"] is False


# ---------------------------------------------------------------------------
# Effective-cost decomposition
# ---------------------------------------------------------------------------


def test_completed_intent_cost_blends_maker_and_fallback_by_fill_fraction(
    monkeypatch,
) -> None:
    monkeypatch.setattr(bars, "close_at_or_before", lambda *a, **k: 100_050.0)
    monkeypatch.setattr(bars, "reference_close_24h", lambda *a, **k: 100_050.0)
    monkeypatch.setattr(bars, "limit_retouched_within_24h", lambda *a, **k: (True, 1.0))
    monkeypatch.setattr(
        bars, "trailing_context", lambda *a, **k: {"realized_vol_24h": None, "trend_14d": None}
    )
    partial = trades_frame([("2025-06-15T00:05:00Z", 99_999.0, 0.04, "S")])
    record = accounting.account_event(
        symbol="BTC/USD",
        decision_ts=T0,
        quarter="2025Q2",
        side="buy",
        notional=10_000.0,
        quotes=STEADY_QUOTES,
        trades=partial,
        policy=simulator.POLICIES["P3_FALLBACK"],
        scenario=SCENARIOS["BASE"],
    )
    assert record["outcome"] == PARTIALLY_FILLED
    fraction = record["fill_fraction"]
    expected = (
        fraction * record["maker_leg_cost_bps"] + (1 - fraction) * record["fallback_cost_bps"]
    )
    assert record["completed_one_way_bps"] == pytest.approx(expected)
    assert 0 < fraction < 1


# ---------------------------------------------------------------------------
# Determinism, schedule, checkpointing
# ---------------------------------------------------------------------------


def test_simulation_is_deterministic() -> None:
    through = trades_frame(
        [
            ("2025-06-15T00:05:00Z", 99_999.0, 0.04, "S"),
            ("2025-06-15T00:20:00Z", 99_998.0, 0.03, "S"),
        ]
    )
    kwargs = {
        "trades": through,
        "quotes": STEADY_QUOTES,
        "side": "buy",
        "limit_price": 100_000.0,
        "quantity": 0.1,
        "active_from": T0,
        "cancel_at": T0 + timedelta(minutes=30),
        "scenario": SCENARIOS["BASE"],
    }
    assert simulate_limit(**kwargs) == simulate_limit(**kwargs)


def test_schedule_is_deterministic_and_pilot_is_a_subset() -> None:
    first = schedule.events_for("BTC/USD", pilot=False)
    second = schedule.events_for("BTC/USD", pilot=False)
    assert first == second
    assert schedule.pilot_is_subset_of_full("BTC/USD")
    assert all(event.event_day <= schedule.LAST_EVENT_DAY for event in first)
    assert all(event.decision_ts.hour == 0 and event.decision_ts.minute == 0 for event in first)


def test_checkpoint_resume_skips_completed_units(monkeypatch, tmp_path) -> None:
    from studies.crypto_maker_execution import run_sim

    monkeypatch.setattr(run_sim, "results_root", lambda: tmp_path)
    calls = {"n": 0}

    class FakeWindow:
        symbol = "BTC/USD"
        decision_ts = T0
        quotes = STEADY_QUOTES
        trades = trades_frame([])

    def fake_fetch(symbol, decision_ts):
        calls["n"] += 1
        return FakeWindow()

    monkeypatch.setattr(run_sim, "fetch_window", fake_fetch)
    monkeypatch.setattr(bars, "close_at_or_before", lambda *a, **k: 100_050.0)
    monkeypatch.setattr(bars, "reference_close_24h", lambda *a, **k: 100_050.0)
    monkeypatch.setattr(bars, "limit_retouched_within_24h", lambda *a, **k: (False, None))
    monkeypatch.setattr(
        bars, "trailing_context", lambda *a, **k: {"realized_vol_24h": None, "trend_14d": None}
    )
    first = run_sim.run_unit("BTC/USD", "2025Q2", "pilot")
    fetches_after_first = calls["n"]
    assert fetches_after_first > 0
    second = run_sim.run_unit("BTC/USD", "2025Q2", "pilot")
    assert calls["n"] == fetches_after_first  # no re-fetch, no recompute
    assert json.dumps(first) == json.dumps(second)


# ---------------------------------------------------------------------------
# Quantity and minimum-notional handling
# ---------------------------------------------------------------------------


def test_quantity_rounds_down_to_the_venue_increment() -> None:
    quantity = accounting.quantity_for("BTC/USD", 10_000.0, 100_050.0)
    assert quantity == pytest.approx(0.0999)
    assert quantity * 10_000 == pytest.approx(round(quantity * 10_000))


def test_below_minimum_notional_is_skipped_not_submitted() -> None:
    record = accounting.account_event(
        symbol="BTC/USD",
        decision_ts=T0,
        quarter="2025Q2",
        side="buy",
        notional=5.0,
        quotes=STEADY_QUOTES,
        trades=trades_frame([]),
        policy=simulator.POLICIES["P0_JOIN"],
        scenario=SCENARIOS["BASE"],
    )
    assert record["status"] == "SKIPPED_BELOW_MINIMUM"
