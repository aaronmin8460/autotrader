"""Diagnosis of the completed V1-V5 crypto study's trade ledger.

This module recomputes, from the study's own recorded trades, the quantities
the cost-aware question turns on. It does not re-run an engine, re-score a bar
or retrain a model: every number here is derived from artifacts the completed
study already wrote, which is what makes the pass cheap enough to run beside a
concurrent research job.

The central identity
-------------------

The study's simulator commits all available cash to a single long position, so
for one round trip the equity multiplier is exact and closed-form:

    equity_after / equity_before = (1 + r) / (1 + B)

where `r` is the reference-price move over the trade and `B` is the break-even
move of `costs.breakeven_move`. Over a sequence of `N` trades that composes to

    total multiplier = PROD(1 + r_i) * (1 + B) ** -N

which separates the result into a part the engine chose (the moves it caught)
and a part only its trade *count* controls (the friction). The separation is
arithmetic, not a model: it holds exactly, and it is checked against the
study's own reported returns in `reconcile`.

The identity is what lets this research answer "how much would turnover
reduction alone have changed the result" without replaying anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from autotrader.research.costs import CostModel

from .costs import breakeven_move

#: Bars are 15 minutes on this dataset; holds are reported in hours.
BAR_MINUTES = 15


class DiagnosticInputError(Exception):
    """The completed study's artifacts were not shaped the way this pass needs."""


@dataclass(frozen=True)
class Decomposition:
    """One engine-symbol result split into the move it caught and what it paid.

    `gross_multiplier` is what the same trade sequence would have returned with
    no friction at all; `friction_multiplier` is what friction alone did to it.
    Their product is the net multiplier, exactly.
    """

    symbol: str
    engine: str
    trades: int
    gross_multiplier: float
    friction_multiplier: float

    @property
    def net_multiplier(self) -> float:
        return self.gross_multiplier * self.friction_multiplier

    @property
    def gross_return(self) -> float:
        return self.gross_multiplier - 1.0

    @property
    def net_return(self) -> float:
        return self.net_multiplier - 1.0

    @property
    def friction_drag(self) -> float:
        """Fraction of capital destroyed by friction, as a positive number."""
        return 1.0 - self.friction_multiplier


def load_trades(analysis_dir: Path) -> pd.DataFrame:
    """Read the completed study's trade ledger and attach reference returns.

    The ledger records the same trade under each cost model. The `gross` rows
    carry unadjusted reference prices -- that model charges no slippage, so its
    fill *is* the bar's open -- and those are the prices a cost-free move must
    be measured on. The reference return is joined onto every cost model's rows
    so a `net` row can be asked what the price actually did, separately from
    what the trade earned after paying for it.
    """
    path = analysis_dir / "trades.csv"
    if not path.exists():
        raise DiagnosticInputError(f"No trade ledger at {path}.")
    trades = pd.read_csv(path, parse_dates=["entry_timestamp", "exit_timestamp"])

    required = {
        "symbol",
        "engine",
        "cost_model",
        "entry_timestamp",
        "exit_timestamp",
        "bars_held",
        "entry_price",
        "exit_price",
        "gross_pnl",
        "fees",
        "slippage_cost",
        "net_pnl",
    }
    missing = required - set(trades.columns)
    if missing:
        raise DiagnosticInputError(f"Trade ledger is missing {sorted(missing)}.")

    key = ["symbol", "engine", "entry_timestamp", "exit_timestamp"]
    reference = trades[trades.cost_model == "gross"][[*key, "entry_price", "exit_price"]].copy()
    if reference.empty:
        raise DiagnosticInputError(
            "The ledger has no `gross` rows, so no reference price series exists."
        )
    reference["reference_return"] = reference.exit_price / reference.entry_price - 1.0
    reference = reference.drop(columns=["entry_price", "exit_price"])

    merged = trades.merge(reference, on=key, how="left", validate="many_to_one")
    if merged.reference_return.isna().any():
        raise DiagnosticInputError(
            "Some trades have no matching `gross` row; the cost models do not share "
            "a trade schedule, so reference returns cannot be attached."
        )

    merged["hold_hours"] = merged.bars_held * BAR_MINUTES / 60.0
    return merged


def schedules_agree(trades: pd.DataFrame) -> bool:
    """True when every cost model traded on the same instants.

    The completed study warned that charging nothing changes the equity path
    and therefore position sizes. It does -- but with all-in sizing on a single
    long position the *schedule* is decided by the decisions alone, and this
    checks that rather than assuming it. When it holds, per-trade returns are
    exactly comparable across cost models.
    """
    columns = ["symbol", "engine", "entry_timestamp", "exit_timestamp"]
    frames = [
        trades[trades.cost_model == label][columns].reset_index(drop=True)
        for label in sorted(trades.cost_model.unique())
    ]
    return all(frame.equals(frames[0]) for frame in frames[1:])


def decompose(trades: pd.DataFrame, model: CostModel) -> list[Decomposition]:
    """Split each engine-symbol result into caught-move and paid-friction.

    Uses the closed-form identity in the module docstring, so the friction term
    depends on the trade *count* and nothing else. That is the point: it makes
    "what would fewer trades have been worth" a question about one exponent.
    """
    b = float(breakeven_move(model))
    out: list[Decomposition] = []
    rows = trades[trades.cost_model == "gross"]
    for (symbol, engine), group in rows.groupby(["symbol", "engine"], sort=True):
        n = len(group)
        log_gross = float((1.0 + group.reference_return).map(math.log).sum())
        out.append(
            Decomposition(
                symbol=str(symbol),
                engine=str(engine),
                trades=n,
                gross_multiplier=math.exp(log_gross),
                friction_multiplier=math.exp(-n * math.log1p(b)),
            )
        )
    return out


def reconcile(
    decompositions: list[Decomposition],
    headline: pd.DataFrame,
    cost_label: str,
) -> pd.DataFrame:
    """Check the identity against the completed study's own reported returns.

    A mismatch here would mean either that this pass has misread the ledger or
    that the study's equity path did something the identity does not describe
    -- an unclosed position, most likely, since an open trade contributes to
    reported equity but not to the closed-trade ledger. The residual column is
    reported rather than asserted away, because its *size* is the diagnosis.
    """
    reported = headline[headline.cost_model == cost_label][
        ["symbol", "engine", "total_return", "trade_count", "unrealized_pnl"]
    ]
    frame = pd.DataFrame(
        [
            {
                "symbol": d.symbol,
                "engine": d.engine,
                "trades_ledger": d.trades,
                "identity_net_return": d.net_return,
                "gross_return": d.gross_return,
                "friction_drag": d.friction_drag,
            }
            for d in decompositions
        ]
    )
    merged = frame.merge(reported, on=["symbol", "engine"], how="left")
    merged["residual"] = merged.total_return - merged.identity_net_return
    return merged


def trade_edge_table(trades: pd.DataFrame, model: CostModel) -> pd.DataFrame:
    """Per-engine distribution of reference move against the break-even bar.

    The column that matters is `pct_clearing_breakeven`: the share of trades
    whose price move was large enough, in the right direction, to have paid for
    itself. A policy that cannot raise that share is not a cost-aware policy.
    """
    b = float(breakeven_move(model))
    rows = trades[trades.cost_model == "gross"]
    records = []
    for (symbol, engine), group in rows.groupby(["symbol", "engine"], sort=True):
        r = group.reference_return
        records.append(
            {
                "symbol": symbol,
                "engine": engine,
                "trades": len(group),
                "median_hold_h": group.hold_hours.median(),
                "mean_hold_h": group.hold_hours.mean(),
                "median_move_bps": r.median() * 10_000,
                "mean_move_bps": r.mean() * 10_000,
                "p10_move_bps": r.quantile(0.10) * 10_000,
                "p90_move_bps": r.quantile(0.90) * 10_000,
                "mean_abs_move_bps": r.abs().mean() * 10_000,
                "pct_positive": (r > 0).mean() * 100,
                "pct_clearing_breakeven": (r > b).mean() * 100,
                "pct_abs_below_breakeven": (r.abs() < b).mean() * 100,
                "mean_edge_after_cost_bps": (r - b).mean() * 10_000,
            }
        )
    return pd.DataFrame(records)


def churn_table(trades: pd.DataFrame) -> pd.DataFrame:
    """How quickly each engine re-enters after it exits, and how short its trades are.

    `median_flat_h` is the time spent out of the market between one exit and the
    next entry. A small figure beside a small median hold is the signature of a
    threshold oscillating around its own trigger rather than of a position being
    managed.
    """
    rows = trades[trades.cost_model == "gross"].sort_values(["symbol", "engine", "entry_timestamp"])
    records = []
    for (symbol, engine), group in rows.groupby(["symbol", "engine"], sort=True):
        group = group.sort_values("entry_timestamp")
        flat = (group.entry_timestamp.shift(-1) - group.exit_timestamp).dt.total_seconds() / 3600.0
        flat = flat.dropna()
        span_h = (group.exit_timestamp.max() - group.entry_timestamp.min()).total_seconds() / 3600.0
        records.append(
            {
                "symbol": symbol,
                "engine": engine,
                "trades": len(group),
                "median_flat_h": flat.median() if len(flat) else float("nan"),
                "pct_reentry_within_1h": (flat <= 1.0).mean() * 100 if len(flat) else float("nan"),
                "pct_reentry_within_4h": (flat <= 4.0).mean() * 100 if len(flat) else float("nan"),
                "pct_hold_under_4h": (group.hold_hours < 4.0).mean() * 100,
                "pct_hold_under_1h": (group.hold_hours < 1.0).mean() * 100,
                "trades_per_month": len(group) / (span_h / (24 * 30.44)) if span_h > 0 else 0.0,
            }
        )
    return pd.DataFrame(records)


def turnover_sensitivity(
    decompositions: list[Decomposition],
    model: CostModel,
    keep_fractions: tuple[float, ...] = (1.0, 0.5, 0.25, 0.10, 0.05, 0.01),
) -> pd.DataFrame:
    """What the same per-trade edge would be worth at a fraction of the trade count.

    This is a bound, not a backtest. It assumes the surviving trades keep the
    engine's *mean* log reference return, which no real filter guarantees --
    a filter that removed trades at random would achieve exactly this, and a
    filter that removed the good ones would do worse. It is reported to size
    the prize: if even the idealised version does not reach a positive number,
    turnover reduction alone is not the answer and no filter needs building.
    """
    b = math.log1p(float(breakeven_move(model)))
    records = []
    for d in decompositions:
        if d.trades == 0:
            continue
        mean_log_edge = math.log(d.gross_multiplier) / d.trades
        for keep in keep_fractions:
            n = d.trades * keep
            records.append(
                {
                    "symbol": d.symbol,
                    "engine": d.engine,
                    "keep_fraction": keep,
                    "trades": n,
                    "net_return": math.exp(n * (mean_log_edge - b)) - 1.0,
                }
            )
    return pd.DataFrame(records)


__all__ = [
    "BAR_MINUTES",
    "Decomposition",
    "DiagnosticInputError",
    "churn_table",
    "decompose",
    "load_trades",
    "reconcile",
    "schedules_agree",
    "trade_edge_table",
    "turnover_sensitivity",
]
