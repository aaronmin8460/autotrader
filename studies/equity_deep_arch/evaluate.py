"""Challenger evaluation: replay, paired comparison against V3 and buy-and-hold.

One evaluation = one challenger decision-series per symbol, replayed through
the shipped research simulator under the study's three cost models, on the
identical region bars the ten-symbol full evaluation scored, with:

- portfolio and per-symbol metrics (shipped `metrics_for_replay`);
- continuous-curve per-window returns for challenger, V3 and buy-and-hold,
  and their paired differences;
- up-capture / down-capture on the frozen window classification;
- the causal SPY drawdown-state bar table;
- realized/unrealized decomposition and forced-terminal-liquidation deltas.

The V3 baseline is *replayed from its stored series through the identical
machinery*, so a challenger is never compared against numbers computed by a
different code path; the wiring is validated by asserting the V3 replay
reproduces the published portfolio figures.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path

import pandas as pd

from autotrader.data.validation import EQUITY_UNIVERSE_LABEL
from autotrader.equity import EQUITY_SYMBOLS
from autotrader.equity.session import market_date
from autotrader.research.costs import Side
from autotrader.research.metrics import EQUITY_15M, metrics_for_replay
from autotrader.research.replay import PortfolioResult, ReplayConfig, replay_portfolio
from studies.equity_10_full.benchmarks import BuyAndHoldEngine
from studies.equity_10_full.windows import FULL_WINDOWS
from studies.equity_v1_v5.adapters import DecisionRecord, DecisionSeriesEngine
from studies.equity_v1_v5.scoring import COST_MODELS, INITIAL_CASH, frame_to_decisions
from studies.equity_v1_v5.windows import ScoringWindow

#: Frozen from the published ten-symbol §40 buy-and-hold window means. The
#: classification does not depend on any challenger.
POSITIVE_WINDOWS: tuple[str, ...] = ("w04", "w05", "w06", "w07", "w08", "w10", "w12")
NEGATIVE_WINDOWS: tuple[str, ...] = ("w01", "w02", "w03", "w09", "w11")

#: The published portfolio figures the V3 wiring check must reproduce.
V3_PUBLISHED_NET = 0.7466
V3_PUBLISHED_MAXDD = -0.1294

PRIMARY_COST = "equity-marketable"


class EvaluationInputError(Exception):
    """An evaluation over inputs that cannot support its claims."""


def region_window() -> ScoringWindow:
    return ScoringWindow(
        name="region",
        start=FULL_WINDOWS[0].start,
        end=FULL_WINDOWS[-1].end,
        covers="continuous scored region",
    )


def load_region_frame(datasets: Path, symbol: str) -> pd.DataFrame:
    """One symbol's session frame, trimmed to the scored region."""
    files = sorted(datasets.glob(f"{symbol}_15m_*session.parquet"))
    if len(files) != 1:
        raise EvaluationInputError(f"Expected one session frame for {symbol}, found {files}.")
    frame = pd.read_parquet(files[0])
    return region_window().bars(frame)


def load_stored_series(decisions: Path, symbol: str, engine: str) -> tuple[DecisionRecord, ...]:
    """One engine's stored decision series, concatenated across all windows."""
    records: list[DecisionRecord] = []
    for window in FULL_WINDOWS:
        path = decisions / f"{symbol}_{window.name}_{engine}.parquet"
        if not path.exists():
            raise EvaluationInputError(f"Missing stored series {path}.")
        records.extend(frame_to_decisions(pd.read_parquet(path)))
    return tuple(records)


def window_returns(result: PortfolioResult) -> dict[str, float]:
    """Continuous-curve return of each window: equity at its close over equity
    at the previous window's close (starting cash before w01)."""
    curve = result.equity_curve
    stamps = result.timestamps
    day_of = [market_date(ts.to_pydatetime()) for ts in stamps]

    returns: dict[str, float] = {}
    previous_equity = result.initial_cash
    index = 0
    for window in FULL_WINDOWS:
        last_inside = None
        while index < len(stamps) and day_of[index] <= window.end:
            last_inside = curve[index]
            index += 1
        if last_inside is None:
            raise EvaluationInputError(f"No portfolio bars inside {window.name}.")
        returns[window.name] = float(last_inside / previous_equity - 1)
        previous_equity = last_inside
    return returns


def capture(returns: Mapping[str, float], benchmark: Mapping[str, float]) -> dict[str, float]:
    """Up/down capture on the frozen window classification."""
    up = sum(returns[w] for w in POSITIVE_WINDOWS) / len(POSITIVE_WINDOWS)
    up_bench = sum(benchmark[w] for w in POSITIVE_WINDOWS) / len(POSITIVE_WINDOWS)
    down = sum(returns[w] for w in NEGATIVE_WINDOWS) / len(NEGATIVE_WINDOWS)
    down_bench = sum(benchmark[w] for w in NEGATIVE_WINDOWS) / len(NEGATIVE_WINDOWS)
    return {
        "up_capture": up / up_bench,
        "down_capture": down / down_bench,
        "mean_positive_window_return": up,
        "mean_negative_window_return": down,
    }


def forced_liquidation_net(result: PortfolioResult, cost_model) -> float:
    """Net return if every terminal open position were sold at the last mark,
    priced under the same cost model as every prior fill."""
    total = Decimal(0)
    for sleeve in result.sleeves.values():
        equity = sleeve.final_equity
        position = sleeve.open_position
        if position is not None:
            mark = position.mark_price
            quantity = position.quantity
            fill = cost_model.fill_price(mark, Side.SELL)
            fee = cost_model.fee(quantity, fill)
            equity = sleeve.final_cash + quantity * fill - fee
        total += equity
    return float(total / result.initial_cash - 1)


def spy_drawdown_states(datasets: Path) -> pd.Series:
    """The published causal labelling: SPY trailing-peak drawdown state per bar."""
    files = sorted(datasets.glob("SPY_15m_*session.parquet"))
    frame = pd.read_parquet(files[0])
    closes = frame["close"].astype("float64")
    drawdown = closes / closes.cummax() - 1.0
    state = pd.Series("drawdown", index=frame.index)
    state[drawdown >= -0.10] = "pullback"
    state[drawdown >= -0.05] = "calm"
    labelled = pd.Series(state.to_numpy(), index=pd.DatetimeIndex(frame["timestamp"]))
    region = region_window().bars(frame)
    return labelled.loc[pd.DatetimeIndex(region["timestamp"])]


def regime_table(result: PortfolioResult, states: pd.Series) -> dict[str, dict[str, float]]:
    """Annualized mean portfolio bar return under each SPY drawdown state."""
    curve = pd.Series(
        [float(value) for value in result.equity_curve],
        index=pd.DatetimeIndex(result.timestamps),
    )
    bar_returns = curve.pct_change().dropna()
    joined = pd.DataFrame({"ret": bar_returns}).join(pd.DataFrame({"state": states}), how="inner")
    table: dict[str, dict[str, float]] = {}
    for state, group in joined.groupby("state"):
        table[str(state)] = {
            "bars": int(len(group)),
            "annualized_mean_return": float(group["ret"].mean() * 26 * 252),
        }
    return table


def replay_engine(
    frames: Mapping[str, pd.DataFrame],
    engine,
    cost_model,
) -> PortfolioResult:
    config = ReplayConfig(
        initial_cash=INITIAL_CASH,
        cost_model=cost_model,
        supported_symbols=EQUITY_SYMBOLS,
        universe_label=EQUITY_UNIVERSE_LABEL,
    )
    return replay_portfolio(dict(frames), engine, config)


def evaluate_challenger(
    datasets: Path,
    decisions: Path,
    challenger: Mapping[str, Sequence[DecisionRecord]],
    *,
    label: str,
    symbols: Sequence[str],
    verify_v3_wiring: bool = True,
) -> dict[str, object]:
    """The full paired evaluation block for one challenger decision series."""
    frames = {symbol: load_region_frame(datasets, symbol) for symbol in symbols}
    states = spy_drawdown_states(datasets)

    challenger_records = [record for symbol in symbols for record in challenger[symbol]]
    v3_records = [
        record for symbol in symbols for record in load_stored_series(decisions, symbol, "V3")
    ]

    engines = {
        label: DecisionSeriesEngine(challenger_records, name=label, version="eda", warmup_bars=0),
        "V3": DecisionSeriesEngine(v3_records, name="V3", version="v3", warmup_bars=0),
        "BUY_AND_HOLD": BuyAndHoldEngine(),
    }

    output: dict[str, object] = {"label": label, "symbols": list(symbols), "engines": {}}
    window_table: dict[str, dict[str, float]] = {}

    for name, engine in engines.items():
        blocks: dict[str, object] = {}
        for cost_model in COST_MODELS:
            replayed = replay_engine(frames, engine, cost_model)
            metrics = metrics_for_replay(replayed, EQUITY_15M).to_json_dict()
            block: dict[str, object] = {
                "metrics": metrics,
                "per_symbol_net": {
                    symbol: float(sleeve.final_equity / sleeve.initial_cash - 1)
                    for symbol, sleeve in replayed.sleeves.items()
                },
                "realized_pnl": str(replayed.realized_pnl),
                "unrealized_pnl": str(replayed.unrealized_pnl),
                "open_terminal_positions": sum(
                    1 for sleeve in replayed.sleeves.values() if sleeve.open_position is not None
                ),
                "forced_liquidation_net": forced_liquidation_net(replayed, cost_model),
            }
            if cost_model.label == PRIMARY_COST:
                block["window_returns"] = window_returns(replayed)
                block["regime_table"] = regime_table(replayed, states)
                window_table[name] = block["window_returns"]
            blocks[cost_model.label] = block
        output["engines"][name] = blocks

    benchmark = window_table["BUY_AND_HOLD"]
    for name in engines:
        output["engines"][name]["capture"] = capture(window_table[name], benchmark)
    output["paired_window_diff_vs_v3"] = {
        window: window_table[label][window] - window_table["V3"][window]
        for window in window_table[label]
    }
    output["paired_window_diff_vs_bh"] = {
        window: window_table[label][window] - benchmark[window] for window in window_table[label]
    }

    if verify_v3_wiring and tuple(symbols) == tuple(EQUITY_SYMBOLS):
        v3_net = output["engines"]["V3"][PRIMARY_COST]["metrics"]["total_return"]
        v3_dd = output["engines"]["V3"][PRIMARY_COST]["metrics"]["max_drawdown"]
        if abs(v3_net - V3_PUBLISHED_NET) > 0.005 or abs(v3_dd - V3_PUBLISHED_MAXDD) > 0.005:
            raise EvaluationInputError(
                f"V3 wiring check failed: replayed net {v3_net:+.4f} / maxDD {v3_dd:+.4f} "
                f"vs published {V3_PUBLISHED_NET:+.4f} / {V3_PUBLISHED_MAXDD:+.4f}."
            )
        output["v3_wiring_check"] = "PASS"

    return output


def write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


__all__ = [
    "EvaluationInputError",
    "NEGATIVE_WINDOWS",
    "POSITIVE_WINDOWS",
    "PRIMARY_COST",
    "capture",
    "evaluate_challenger",
    "load_region_frame",
    "load_stored_series",
    "region_window",
    "spy_drawdown_states",
    "window_returns",
    "write_json",
]
