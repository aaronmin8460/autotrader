"""The completed V1-V5 results must survive this research untouched.

The cost-aware layer is only meaningful if the thing it wraps still does
exactly what it did. This module proves that against the real artifacts rather
than against a fixture: it replays the completed study's stored decisions
through this package's harness under the pass-through policy and requires the
net return of all ten engine-symbols to match the study's own reported figure
to floating-point equality.

**These tests skip when the external research workspace is not mounted.** They
depend on a 7.8 MB decision series and two dataset parquets that do not belong
in the repository, and a test that silently invented substitutes for them would
prove nothing. A skip says "not checked here"; a fixture would say "checked"
and be wrong.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from studies.crypto_cost_aware.diagnostics import decompose, load_trades, schedules_agree
from studies.crypto_cost_aware.policy import PassThrough
from studies.crypto_cost_aware.replay import load_decision_series, replay_candidate, summarize

from autotrader.research.costs import CRYPTO_COST

#: Where the completed study wrote its artifacts. Overridable so the check can
#: be pointed at a copy without editing the test.
WORKSPACE = Path(os.environ.get("AUTOTRADER_QA", "/Volumes/AUTOTRADER_QA"))
RUN_DIR = WORKSPACE / "reports" / "crypto-v1-v5-historical"
DATASETS = WORKSPACE / "datasets" / "crypto-historical"

DATASET_FILES = {
    "BTC/USD": "BTC_USD_15m_2024-01-01_2026-08-28.parquet",
    "ETH/USD": "ETH_USD_15m_2024-01-01_2026-08-28.parquet",
}
ENGINES = ("v1", "v2", "v3", "v4", "v5")
SCORING_START = "2025-01-01"
INITIAL_CASH = Decimal("100000")

#: The completed study's own headline net returns, transcribed from its report.
#: Kept here as literals on purpose: comparing the artifacts only against
#: themselves would pass even if both had drifted together.
REPORTED_NET_RETURN = {
    ("BTC/USD", "v1"): -0.9606,
    ("BTC/USD", "v2"): -0.9476,
    ("BTC/USD", "v3"): -0.6221,
    ("BTC/USD", "v4"): 0.0000,
    ("BTC/USD", "v5"): -0.3547,
    ("ETH/USD", "v1"): -0.9528,
    ("ETH/USD", "v2"): -0.9621,
    ("ETH/USD", "v3"): -0.5280,
    ("ETH/USD", "v4"): 0.0000,
    ("ETH/USD", "v5"): +0.0198,
}

requires_workspace = pytest.mark.skipif(
    not (RUN_DIR / "decisions_selected.parquet").exists() or not DATASETS.exists(),
    reason="the external research workspace is not mounted",
)


@pytest.fixture(scope="module")
def headline() -> pd.DataFrame:
    return pd.read_csv(RUN_DIR / "analysis_selected" / "headline_metrics.csv")


def _bars(symbol: str) -> pd.DataFrame:
    frame = pd.read_parquet(DATASETS / DATASET_FILES[symbol]).sort_values("timestamp")
    return frame[frame.timestamp >= SCORING_START].reset_index(drop=True)


@requires_workspace
@pytest.mark.parametrize("symbol", sorted(DATASET_FILES))
@pytest.mark.parametrize("engine", ENGINES)
def test_pass_through_reproduces_the_completed_study(
    symbol: str, engine: str, headline: pd.DataFrame
) -> None:
    """Wrapping an engine and admitting everything must change nothing at all."""
    upstream = load_decision_series(
        RUN_DIR / "decisions_selected.parquet", symbol, engine, warmup_bars=0
    )
    result = replay_candidate(
        _bars(symbol),
        upstream,
        PassThrough(),
        cost_model=CRYPTO_COST,
        initial_cash=INITIAL_CASH,
        volatility_bars=96,
    )
    row = summarize(result, label="passthrough", symbol=symbol, engine=engine)
    reference = headline[
        (headline.cost_model == "net") & (headline.symbol == symbol) & (headline.engine == engine)
    ].iloc[0]

    assert row["trades"] == int(reference.trade_count)
    assert row["total_return"] == pytest.approx(float(reference.total_return), abs=1e-12)


@requires_workspace
@pytest.mark.parametrize("key", sorted(REPORTED_NET_RETURN))
def test_the_replay_still_agrees_with_the_published_report(
    key: tuple[str, str], headline: pd.DataFrame
) -> None:
    """Guards against the artifacts and the report drifting together."""
    symbol, engine = key
    upstream = load_decision_series(
        RUN_DIR / "decisions_selected.parquet", symbol, engine, warmup_bars=0
    )
    result = replay_candidate(
        _bars(symbol),
        upstream,
        PassThrough(),
        cost_model=CRYPTO_COST,
        initial_cash=INITIAL_CASH,
        volatility_bars=96,
    )
    achieved = float(result.final_equity / INITIAL_CASH - 1)
    assert achieved == pytest.approx(REPORTED_NET_RETURN[key], abs=5e-5)


@requires_workspace
def test_every_cost_model_traded_on_the_same_instants() -> None:
    """The property the whole per-trade diagnosis rests on.

    If the cost models did not share a trade schedule, a reference return taken
    from the `gross` ledger could not be attached to a `net` trade, and the
    edge distribution would be comparing different trades.
    """
    assert schedules_agree(load_trades(RUN_DIR / "analysis_selected"))


@requires_workspace
def test_the_closed_form_identity_reproduces_the_reported_returns() -> None:
    """`PROD(1 + r) * (1 + B) ** -N` must equal the study's net return.

    Exactly, for every engine whose position was closed at the end of the
    sample. V5 is excluded from the equality because it finished holding, and
    the identity describes closed round trips; that residual is measured in
    its own test rather than tolerated here.
    """
    trades = load_trades(RUN_DIR / "analysis_selected")
    headline = pd.read_csv(RUN_DIR / "analysis_selected" / "headline_metrics.csv")
    net = headline[headline.cost_model == "net"]

    for item in decompose(trades, CRYPTO_COST):
        if item.engine == "v5":
            continue
        reported = net[(net.symbol == item.symbol) & (net.engine == item.engine)].iloc[0]
        assert item.net_return == pytest.approx(float(reported.total_return), abs=1e-9)


@requires_workspace
def test_the_only_positive_result_is_an_unclosed_position() -> None:
    """The completed study's one positive figure, re-derived rather than quoted.

    ETH V5's realized record is a loss; the published +1.98% is that loss plus
    an open position marked to the final bar. The identity separates the two,
    so this asserts on the separation rather than on the headline.
    """
    trades = load_trades(RUN_DIR / "analysis_selected")
    headline = pd.read_csv(RUN_DIR / "analysis_selected" / "headline_metrics.csv")
    net = headline[headline.cost_model == "net"]

    eth_v5 = next(
        d for d in decompose(trades, CRYPTO_COST) if (d.symbol, d.engine) == ("ETH/USD", "v5")
    )
    reported = net[(net.symbol == "ETH/USD") & (net.engine == "v5")].iloc[0]

    assert eth_v5.net_return < -0.20, "the realized record should be a substantial loss"
    assert float(reported.total_return) > 0, "the published headline should be positive"
    assert float(reported.unrealized_pnl) > 0
    residual = float(reported.total_return) - eth_v5.net_return
    assert residual == pytest.approx(float(reported.unrealized_pnl) / float(INITIAL_CASH), abs=1e-3)
