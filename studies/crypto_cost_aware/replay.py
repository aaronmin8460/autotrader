"""Replaying candidate policies over the completed study's stored decisions.

Nothing here calls a decision engine. The completed V1-V5 study already
computed 579,630 engine decisions and stored them; this module replays that
series through the shipped simulator, once per candidate policy, which is what
makes a candidate evaluable in seconds rather than hours and is why this task
can run beside a concurrent research job.

**The stored series is not evidence of causality and is not used as such.** A
lookup table cannot see the future because it cannot see anything, so a leakage
probe against it would pass for a reason that establishes nothing. The
completed study established causality for V1-V5 against the engines
themselves. What *this* module adds is a policy layer, and that layer's
causality is established separately, against a computing implementation, in
`tests/test_cost_aware_policy.py`. `audit_ready` is `False` here for the same
reason it is false in the completed study, and for nothing to hide.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pandas as pd

from autotrader.research.costs import CostModel
from autotrader.research.engines import Action, ResearchSignal
from autotrader.research.replay import ReplayConfig, ReplayResult, replay

from .policy import CostAwareEngine, EligibilityPolicy

#: How a stored decision direction becomes a research action. HOLD is absent
#: deliberately: it is the absence of a proposal, not a third instruction.
ACTION_FOR = {"BUY": Action.ENTER_LONG, "SELL": Action.EXIT_LONG}

#: Reason tokens are stored pipe-joined by the completed study.
REASON_SEPARATOR = "|"


class ReplayDriverError(Exception):
    """The stored decision series cannot be replayed as asked."""


@dataclass(frozen=True)
class StoredSeriesEngine:
    """Replays one engine's stored decisions for one symbol. Not auditable, on purpose."""

    signals: tuple[ResearchSignal, ...]
    engine_name: str
    engine_version: str
    warmup: int
    audit_ready: bool = False

    @property
    def name(self) -> str:
        return self.engine_name

    @property
    def version(self) -> str:
        return self.engine_version

    @property
    def parameters(self) -> Mapping[str, object]:
        return {"source": "stored_decision_series"}

    @property
    def warmup_bars(self) -> int:
        return self.warmup

    def generate(self, bars: pd.DataFrame) -> Sequence[ResearchSignal]:
        """Return the stored proposals that fall inside this frame.

        Filtered to the frame rather than returned wholesale, so replaying a
        sub-period does not hand the simulator a signal for a bar it was not
        given -- which the simulator would refuse anyway, but refusing early
        makes the cause obvious.
        """
        window = set(bars["timestamp"])
        return tuple(s for s in self.signals if s.timestamp in window)


def load_decision_series(
    decisions_path: Path,
    symbol: str,
    engine: str,
    *,
    warmup_bars: int,
) -> StoredSeriesEngine:
    """Build a replayable engine from the completed study's decision parquet."""
    if not decisions_path.exists():
        raise ReplayDriverError(f"No decision series at {decisions_path}.")
    frame = pd.read_parquet(decisions_path)
    selected = frame[(frame.symbol == symbol) & (frame.engine == engine)]
    if selected.empty:
        raise ReplayDriverError(f"The series has no rows for {engine} on {symbol}.")
    if selected.timestamp.duplicated().any():
        raise ReplayDriverError(
            f"The series has more than one decision for one instant on {engine}/{symbol}; "
            "a series with two answers for one bar cannot be replayed."
        )

    signals: list[ResearchSignal] = []
    for row in selected.sort_values("timestamp").itertuples():
        action = ACTION_FOR.get(row.signal)
        if action is None:
            continue
        signals.append(
            ResearchSignal(
                timestamp=row.timestamp,
                symbol=symbol,
                action=action,
                reason=str(row.reasons or ""),
                strength=float(row.confidence),
            )
        )
    return StoredSeriesEngine(
        signals=tuple(signals),
        engine_name=engine,
        engine_version=str(selected.model_version.iloc[0]),
        warmup=int(warmup_bars),
    )


def replay_candidate(
    bars: pd.DataFrame,
    upstream: StoredSeriesEngine,
    policy: EligibilityPolicy,
    *,
    cost_model: CostModel,
    initial_cash: Decimal,
    volatility_bars: int,
) -> ReplayResult:
    """Replay one upstream engine under one policy, through the shipped simulator."""
    engine = CostAwareEngine(upstream, policy, volatility_bars=volatility_bars)
    config = ReplayConfig(initial_cash=initial_cash, cost_model=cost_model)
    return replay(bars, engine, config)


def summarize(result: ReplayResult, *, label: str, symbol: str, engine: str) -> dict[str, object]:
    """The row a candidate contributes to the comparison table.

    `realized_return` is reported beside `total_return` because the completed
    study's one positive figure was an unclosed position marked to the final
    bar, and a comparison that shows only the headline would repeat that
    mistake rather than expose it.
    """
    initial = result.initial_cash
    realized_equity = initial + result.realized_pnl
    return {
        "symbol": symbol,
        "engine": engine,
        "policy": label,
        "trades": result.trade_count,
        "total_return": float(result.final_equity / initial - 1),
        "realized_return": float(realized_equity / initial - 1),
        "unrealized_pnl": float(result.unrealized_pnl),
        "total_fees": float(result.total_fees),
        "total_slippage": float(result.total_slippage_cost),
        "exposure": result.exposure_bars / result.bar_count if result.bar_count else 0.0,
        "signals_proposed": result.signal_count,
        "signals_skipped": result.skipped_signal_count,
        "open_position": result.open_position is not None,
    }


__all__ = [
    "ACTION_FOR",
    "ReplayDriverError",
    "StoredSeriesEngine",
    "load_decision_series",
    "replay_candidate",
    "summarize",
]
