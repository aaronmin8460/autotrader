"""Reporting for signed replays: the metric set §L6/§Phase 6 declares.

Every row reports LONG GROSS, SHORT GROSS, TOTAL GROSS and NET — never net
alone — plus the short sleeve's standalone P&L, decomposed by regime, so a
short candidate is never hidden inside a blended curve.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import numpy as np
import pandas as pd

from studies.equity_10_full.windows import FULL_WINDOWS
from studies.equity_deep_arch.evaluate import NEGATIVE_WINDOWS, POSITIVE_WINDOWS
from studies.equity_short_sleeve.shorts import ShortResult

#: The ten-symbol equal-weight buy-and-hold window means, from the frozen
#: published capture denominator (deep-architecture `capture` block).
BH_POSITIVE_MEAN = 0.2385252413728857
BH_NEGATIVE_MEAN = -0.09256548569175034


def signed_report(
    result: ShortResult,
    states: pd.Series,
    participate: Mapping[date, bool],
    *,
    run_of: Mapping[date, int] | None = None,
    transitions: Sequence[date] = (),
) -> dict[str, object]:
    """The full declared metric set for one signed replay."""
    from autotrader.equity.session import market_date

    metrics = result.metrics().to_json_dict()
    curve = pd.Series(result.equity_curve, index=pd.DatetimeIndex(result.timestamps))
    day_of = [market_date(ts.to_pydatetime()) for ts in result.timestamps]

    window_returns: dict[str, float] = {}
    previous = result.initial_cash
    index = 0
    for window in FULL_WINDOWS:
        last_inside = None
        while index < len(result.timestamps) and day_of[index] <= window.end:
            last_inside = result.equity_curve[index]
            index += 1
        if last_inside is None:
            raise SystemExit(f"No bars inside {window.name}.")
        window_returns[window.name] = float(last_inside / previous - 1)
        previous = last_inside

    bar_returns = curve.pct_change().dropna()
    joined = pd.DataFrame({"ret": bar_returns}).join(pd.DataFrame({"state": states}), how="inner")
    regime = {
        str(state): {
            "bars": int(len(group)),
            "annualized_mean_return": float(group["ret"].mean() * 26 * 252),
        }
        for state, group in joined.groupby("state")
    }

    up = sum(window_returns[w] for w in POSITIVE_WINDOWS) / len(POSITIVE_WINDOWS)
    down = sum(window_returns[w] for w in NEGATIVE_WINDOWS) / len(NEGATIVE_WINDOWS)

    # Session-level portfolio and short-sleeve series.
    frame = pd.DataFrame(
        {
            "session": day_of,
            "equity": list(result.equity_curve),
            "short_pnl": list(result.short_pnl_series),
            "long_pnl": list(result.long_pnl_series),
            "short_gross": list(result.short_gross_series),
        }
    )
    by_session = frame.groupby("session", sort=True).agg(
        equity=("equity", "last"),
        short_pnl=("short_pnl", "sum"),
        long_pnl=("long_pnl", "sum"),
        short_gross=("short_gross", "mean"),
    )
    session_ret = by_session["equity"].pct_change()
    short_share = by_session["short_pnl"] / by_session["equity"].shift(1)

    sessions = list(by_session.index)
    defensive_mask = np.array([not participate.get(s, True) for s in sessions])
    short_defensive = float(by_session["short_pnl"].to_numpy()[defensive_mask].sum())
    short_participate = float(by_session["short_pnl"].to_numpy()[~defensive_mask].sum())

    # Post-transition (recovery / squeeze) accounting.
    recovery: dict[str, float] = {}
    position = {s: i for i, s in enumerate(sessions)}
    for horizon in (1, 2, 5):
        total = 0.0
        worst = 0.0
        for flip in transitions:
            start = position.get(flip)
            if start is None:
                continue
            window = by_session["short_pnl"].to_numpy()[start : start + horizon]
            equity_before = float(by_session["equity"].to_numpy()[max(start - 1, 0)])
            contribution = float(window.sum()) / equity_before if equity_before else 0.0
            total += contribution
            worst = min(worst, contribution)
        recovery[f"post_transition_{horizon}_mean_pct"] = (
            total / len(transitions) if transitions else 0.0
        )
        recovery[f"post_transition_{horizon}_worst_pct"] = worst

    worst_session = float(short_share.min()) if len(short_share.dropna()) else 0.0
    best_session = float(short_share.max()) if len(short_share.dropna()) else 0.0
    weekly = short_share.rolling(5).sum()
    worst_week_portfolio = float(session_ret.rolling(5).apply(lambda x: (1 + x).prod() - 1).min())

    return {
        "label": result.label,
        "cost": result.cost_label,
        "short_cost": result.short_cost_label,
        "borrow": result.borrow_label,
        "net_return": result.net_return,
        "metrics": metrics,
        "window_returns": window_returns,
        "regime_table": regime,
        "mean_positive_window_return": up,
        "mean_negative_window_return": down,
        "up_capture": up / BH_POSITIVE_MEAN,
        "down_capture": down / BH_NEGATIVE_MEAN,
        "forced_liquidation_net": result.forced_liquidation_net,
        "fills": result.fill_count,
        "short_fills": result.short_fill_count,
        "turnover": result.turnover,
        "short_turnover": result.short_turnover,
        "borrow_cost": result.borrow_cost,
        "total_fees": result.total_fees,
        "total_slippage": result.total_slippage,
        "exposure": {
            "long_gross_mean": result.long_gross_mean,
            "short_gross_mean": result.short_gross_mean,
            "short_gross_max": result.short_gross_max,
            "total_gross_mean": result.total_gross_mean,
            "total_gross_max": result.total_gross_max,
            "net_exposure_mean": result.net_exposure_mean,
            "net_exposure_min": result.net_exposure_min,
            "short_gross_mean_when_on": (
                float(np.mean([g for g in result.short_gross_series if g > 0]))
                if any(g > 0 for g in result.short_gross_series)
                else 0.0
            ),
            "mean_short_names": result.mean_short_names,
            "max_short_names": result.max_short_names,
            "max_short_weight_assigned": result.max_short_weight_assigned,
            "short_bars": result.short_bars,
        },
        "sleeve": {
            "long_pnl": result.long_pnl,
            "short_pnl": result.short_pnl,
            "short_pnl_pct_of_initial": result.short_pnl / result.initial_cash,
            "short_pnl_defensive": short_defensive,
            "short_pnl_participate": short_participate,
            "short_hit_rate": float((by_session["short_pnl"] > 0).sum())
            / max(int((by_session["short_pnl"] != 0).sum()), 1),
            "short_sessions": int((by_session["short_pnl"] != 0).sum()),
            "average_winning_session": float(
                by_session.loc[by_session["short_pnl"] > 0, "short_pnl"].mean()
            )
            if (by_session["short_pnl"] > 0).any()
            else 0.0,
            "average_losing_session": float(
                by_session.loc[by_session["short_pnl"] < 0, "short_pnl"].mean()
            )
            if (by_session["short_pnl"] < 0).any()
            else 0.0,
            "profit_factor": (
                float(by_session.loc[by_session["short_pnl"] > 0, "short_pnl"].sum())
                / abs(float(by_session.loc[by_session["short_pnl"] < 0, "short_pnl"].sum()))
                if (by_session["short_pnl"] < 0).any()
                else float("inf")
            ),
            "worst_session_contribution_pct": worst_session,
            "best_session_contribution_pct": best_session,
            "worst_5session_contribution_pct": float(weekly.min()) if len(weekly.dropna()) else 0.0,
            "short_sharpe": (
                float(short_share.mean() / short_share.std() * np.sqrt(252))
                if short_share.std() and short_share.std() > 0
                else 0.0
            ),
            **recovery,
        },
        "worst_session_return": float(session_ret.min()) if len(session_ret.dropna()) else 0.0,
        "worst_week_return": worst_week_portfolio,
        "reconciliation_error": result.reconciliation_error,
    }


__all__ = ["BH_NEGATIVE_MEAN", "BH_POSITIVE_MEAN", "signed_report"]
