"""Per-event execution accounting: decomposed costs, markouts, missed fills.

Sign convention: `side_sign` is +1 for BUY and −1 for SELL, and every cost
figure is quoted in basis points where **positive = worse for us**. A
negative spread component is genuine capture (bought below / sold above
the prevailing mid).
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

from studies.crypto_maker_execution import bars
from studies.crypto_maker_execution.simulator import (
    POLICIES,
    SCENARIOS,
    Policy,
    Scenario,
    SimulationResult,
    activation_and_cancel,
    limit_price_for,
    mid_of,
    quote_at_or_before,
    simulate_limit,
    spread_bps_of,
)
from studies.crypto_maker_execution.venue import (
    MAKER_FEE,
    MINIMUM_NOTIONAL_USD,
    SYMBOLS,
    TAKER_FEE,
)

MAKER_FEE_BPS = MAKER_FEE * 1e4
TAKER_FEE_BPS = TAKER_FEE * 1e4

#: The quote record prints updates; the standing book persists between
#: them. A decision is priceable from the latest update up to 24h back
#: (staleness recorded and stratified); beyond that the event is skipped.
MAX_QUOTE_STALENESS_S = 86_400.0

MARKOUT_HORIZONS = {"15s": 15.0, "1m": 60.0, "5m": 300.0, "15m": 900.0}


def side_sign(side: str) -> int:
    return 1 if side == "buy" else -1


def shortfall_bps(side: str, executed_price: float, reference_mid: float) -> float:
    return side_sign(side) * (executed_price - reference_mid) / reference_mid * 1e4


def quantity_for(symbol: str, notional: float, reference_mid: float) -> float:
    spec = SYMBOLS[symbol]
    raw = notional / reference_mid
    steps = int(raw / spec.quantity_increment)
    return steps * spec.quantity_increment


def weighted_fill_mid_markouts(quotes: pd.DataFrame, result: SimulationResult, side: str) -> dict:
    """Quantity-weighted fill-time mid and adverse-selection markouts.

    Adverse selection at horizon Δ is `side × (mid_fill − mid_Δ)` in bps of
    the fill-time mid: positive when the market moves against the position
    after the fill (mid falls after our buy; rises after our sell).
    """
    out: dict = {"fill_mid": None}
    for label in MARKOUT_HORIZONS:
        out[f"adverse_{label}_bps"] = None
    if not result.fills or result.filled_quantity <= 0:
        return out
    total = result.filled_quantity
    mid_sum = 0.0
    adverse_sums = dict.fromkeys(MARKOUT_HORIZONS, 0.0)
    adverse_ok = dict.fromkeys(MARKOUT_HORIZONS, True)
    for fill in result.fills:
        fill_mid = mid_of(quote_at_or_before(quotes, fill.timestamp))
        if fill_mid is None:
            return out
        mid_sum += fill_mid * fill.quantity
        for label, seconds in MARKOUT_HORIZONS.items():
            later = mid_of(quote_at_or_before(quotes, fill.timestamp + timedelta(seconds=seconds)))
            if later is None:
                adverse_ok[label] = False
                continue
            adverse = side_sign(side) * (fill_mid - later) / fill_mid * 1e4
            adverse_sums[label] += adverse * fill.quantity
    out["fill_mid"] = mid_sum / total
    for label in MARKOUT_HORIZONS:
        out[f"adverse_{label}_bps"] = adverse_sums[label] / total if adverse_ok[label] else None
    return out


def account_event(
    *,
    symbol: str,
    decision_ts: pd.Timestamp,
    quarter: str,
    side: str,
    notional: float,
    quotes: pd.DataFrame,
    trades: pd.DataFrame,
    policy: Policy,
    scenario: Scenario,
) -> dict:
    """Full predeclared accounting for one event × side × policy × scenario."""
    record: dict = {
        "symbol": symbol,
        "decision_ts": decision_ts.isoformat(),
        "quarter": quarter,
        "side": side,
        "notional": notional,
        "policy": policy.name,
        "scenario": scenario.name,
    }
    decision_quote = quote_at_or_before(quotes, decision_ts)
    staleness = None
    if decision_quote is not None:
        staleness = (decision_ts - decision_quote["t"]).total_seconds()
        if staleness > MAX_QUOTE_STALENESS_S:
            decision_quote = None
    if decision_quote is None:
        record["status"] = "SKIPPED_NO_QUOTE"
        return record
    record["decision_quote_staleness_s"] = staleness
    mid0 = mid_of(decision_quote)
    if mid0 is None:
        record["status"] = "SKIPPED_NO_QUOTE"
        return record

    spec = SYMBOLS[symbol]
    limit = limit_price_for(side, decision_quote, spec.price_increment, policy.price_improve_ticks)
    quantity = quantity_for(symbol, notional, mid0)
    if quantity * mid0 < MINIMUM_NOTIONAL_USD:
        record["status"] = "SKIPPED_BELOW_MINIMUM"
        return record
    active_from, cancel_at = activation_and_cancel(decision_ts, policy, scenario)

    result = simulate_limit(
        trades=trades,
        quotes=quotes,
        side=side,
        limit_price=limit,
        quantity=quantity,
        active_from=active_from,
        cancel_at=cancel_at,
        scenario=scenario,
    )

    record.update(
        {
            "status": "OK",
            "decision_mid": mid0,
            "decision_spread_bps": spread_bps_of(decision_quote),
            "limit_price": limit,
            "quantity": quantity,
            "outcome": result.outcome,
            "fill_fraction": result.fill_fraction,
            "fill_count": len(result.fills),
            "wait_to_first_fill_s": result.wait_to_first_fill_s,
            "wait_to_full_fill_s": result.wait_to_full_fill_s,
        }
    )

    markouts = weighted_fill_mid_markouts(quotes, result, side)
    fill_mid = markouts.pop("fill_mid")
    record.update(markouts)

    if result.filled_quantity > 0:
        record["maker_shortfall_bps"] = shortfall_bps(side, limit, mid0)
        record["maker_leg_cost_bps"] = MAKER_FEE_BPS + record["maker_shortfall_bps"]
        if fill_mid is not None:
            record["spread_component_bps"] = shortfall_bps(side, limit, fill_mid)
            record["drift_to_fill_bps"] = side_sign(side) * (fill_mid - mid0) / mid0 * 1e4
        first = result.fills[0]
        adverse_24h = None
        close_24h = bars.close_at_or_before(symbol, first.timestamp + timedelta(hours=24))
        if close_24h is not None and fill_mid is not None:
            adverse_24h = side_sign(side) * (fill_mid - close_24h) / fill_mid * 1e4
        record["adverse_24h_bps"] = adverse_24h

    close_24h_decision = bars.reference_close_24h(symbol, decision_ts)
    if result.remaining_quantity > 0:
        missed = None
        if close_24h_decision is not None:
            missed = side_sign(side) * (close_24h_decision - mid0) / mid0 * 1e4
        record["missed_opportunity_bps"] = missed
        touched, hours = bars.limit_retouched_within_24h(symbol, side, limit, cancel_at)
        record["limit_retouched_24h"] = touched
        record["hours_to_retouch"] = hours

    if policy.taker_fallback:
        fallback_quote = quote_at_or_before(quotes, cancel_at)
        fallback_price = None
        if fallback_quote is not None:
            fallback_price = (
                float(fallback_quote["ask_price"])
                if side == "buy"
                else float(fallback_quote["bid_price"])
            )
        if result.remaining_quantity > 0 and fallback_price is None:
            record["status"] = "FALLBACK_UNPRICEABLE"
            return record
        fallback_cost = None
        if fallback_price is not None:
            fallback_cost = TAKER_FEE_BPS + shortfall_bps(side, fallback_price, mid0)
        record["fallback_price"] = fallback_price
        record["fallback_cost_bps"] = fallback_cost
        maker_leg = record.get("maker_leg_cost_bps", 0.0)
        fraction = result.fill_fraction
        record["completed_one_way_bps"] = fraction * maker_leg + (1.0 - fraction) * (
            fallback_cost if fallback_cost is not None else 0.0
        )
        record["fallback_fraction"] = 1.0 - fraction

    taker_quote = quote_at_or_before(quotes, decision_ts + timedelta(seconds=5))
    if taker_quote is not None:
        taker_price = (
            float(taker_quote["ask_price"]) if side == "buy" else float(taker_quote["bid_price"])
        )
        record["taker_immediate_cost_bps"] = TAKER_FEE_BPS + shortfall_bps(side, taker_price, mid0)

    context = bars.trailing_context(symbol, decision_ts)
    record["realized_vol_24h"] = context["realized_vol_24h"]
    record["trend_14d"] = context["trend_14d"]
    return record


def account_all_policies(
    *,
    symbol: str,
    decision_ts: pd.Timestamp,
    quarter: str,
    quotes: pd.DataFrame,
    trades: pd.DataFrame,
    notionals: tuple[float, ...] = (10_000.0, 1_000.0),
) -> list[dict]:
    records = []
    for notional in notionals:
        for side in ("buy", "sell"):
            for policy in POLICIES.values():
                for scenario in SCENARIOS.values():
                    records.append(
                        account_event(
                            symbol=symbol,
                            decision_ts=decision_ts,
                            quarter=quarter,
                            side=side,
                            notional=notional,
                            quotes=quotes,
                            trades=trades,
                            policy=policy,
                            scenario=scenario,
                        )
                    )
    return records
