"""Phase-2 candidate pool, roles, sector map, and the deterministic manifest
rule (ledger §L3 + dated amendment adding XLC).

Classification: FIXED CURRENT LIQUID UNIVERSE RESEARCH. The pool is a
present-day liquid list; testing it into 2021 carries survivorship/selection
bias, disclosed wherever results are reported.
"""

from __future__ import annotations

import pandas as pd

INCUMBENTS: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "IWM",
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
)

BROAD_ETFS: tuple[str, ...] = ("SPY", "QQQ", "IWM", "DIA")

#: The GICS-11 sector SPDRs (XLRE excluded: real estate has no pool stock and
#: adds nothing to the roles declared in the ledger; XLC added by the dated
#: amendment before any Phase-2 download).
SECTOR_ETFS: tuple[str, ...] = (
    "XLK",
    "XLF",
    "XLE",
    "XLV",
    "XLI",
    "XLY",
    "XLP",
    "XLU",
    "XLB",
    "XLC",
)

SEMI_ETFS: tuple[str, ...] = ("SMH", "SOXX")

#: Context-only: never traded, never counted in U30/U50.
CONTEXT_ONLY: tuple[str, ...] = ("IEF", "TLT")

#: Large-cap stock candidates (ledger §L3), incumbent single names included so
#: everyone competes on the same liquidity metric.
STOCK_POOL: tuple[str, ...] = (
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "AVGO",
    "AMD",
    "NFLX",
    "CRM",
    "ORCL",
    "ADBE",
    "INTC",
    "QCOM",
    "TXN",
    "CSCO",
    "JPM",
    "BAC",
    "WFC",
    "GS",
    "MS",
    "V",
    "MA",
    "COST",
    "WMT",
    "HD",
    "MCD",
    "LOW",
    "DIS",
    "LLY",
    "UNH",
    "JNJ",
    "PFE",
    "MRK",
    "ABBV",
    "TMO",
    "XOM",
    "CVX",
    "COP",
    "CAT",
    "GE",
    "BA",
    "HON",
    "UPS",
    "RTX",
    "DE",
    "LIN",
    "PEP",
    "KO",
    "PG",
    "VZ",
    "CMCSA",
    "IBM",
)

#: Static sector map, current GICS (the 2023 V/MA reclassification into
#: financials is applied throughout — a disclosed static simplification).
SECTOR_OF: dict[str, str] = {
    # technology
    **{
        s: "XLK"
        for s in (
            "AAPL",
            "MSFT",
            "NVDA",
            "AVGO",
            "AMD",
            "CRM",
            "ORCL",
            "ADBE",
            "INTC",
            "QCOM",
            "TXN",
            "CSCO",
            "IBM",
        )
    },
    # communication services
    **{s: "XLC" for s in ("META", "GOOGL", "NFLX", "DIS", "CMCSA", "VZ")},
    # consumer discretionary
    **{s: "XLY" for s in ("AMZN", "TSLA", "HD", "MCD", "LOW")},
    # financials
    **{s: "XLF" for s in ("JPM", "BAC", "WFC", "GS", "MS", "V", "MA")},
    # health care
    **{s: "XLV" for s in ("LLY", "UNH", "JNJ", "PFE", "MRK", "ABBV", "TMO")},
    # energy
    **{s: "XLE" for s in ("XOM", "CVX", "COP")},
    # industrials
    **{s: "XLI" for s in ("CAT", "GE", "BA", "HON", "UPS", "RTX", "DE")},
    # materials
    "LIN": "XLB",
    # consumer staples
    **{s: "XLP" for s in ("COST", "WMT", "PEP", "KO", "PG")},
}

#: Everything Phase 2 downloads (the ten incumbents are reused read-only from
#: the frozen directory).
DOWNLOAD_SYMBOLS: tuple[str, ...] = tuple(
    sorted(
        (set(BROAD_ETFS) | set(SECTOR_ETFS) | set(SEMI_ETFS) | set(CONTEXT_ONLY) | set(STOCK_POOL))
        - set(INCUMBENTS)
    )
)

#: The prior study's exclusion threshold, reused unchanged.
MAX_MISSING_FRACTION = 0.01


def liquidity_metric(frame: pd.DataFrame) -> float:
    """Median session dollar volume: sum(close × volume) per session, median
    across sessions. A tradability screen, not a return signal."""
    from autotrader.equity.session import market_date

    working = pd.DataFrame(
        {
            "session": [market_date(ts.to_pydatetime()) for ts in frame["timestamp"]],
            "dollars": frame["close"].to_numpy(dtype="float64")
            * frame["volume"].to_numpy(dtype="float64"),
        }
    )
    per_session = working.groupby("session", sort=True)["dollars"].sum()
    return float(per_session.median())


def build_manifests(
    eligibility: dict[str, dict[str, object]],
) -> dict[str, object]:
    """Apply the ledger's deterministic U30/U50 rule to per-symbol eligibility
    rows (each: eligible: bool, liquidity: float, reasons: list[str])."""

    def eligible(symbol: str) -> bool:
        return bool(eligibility.get(symbol, {}).get("eligible", False))

    def rank_key(symbol: str) -> tuple[float, str]:
        return (-float(eligibility[symbol]["liquidity"]), symbol)

    etfs_u30 = [s for s in (*BROAD_ETFS, *SECTOR_ETFS, "SMH") if eligible(s)]
    stocks_ranked = sorted((s for s in STOCK_POOL if eligible(s)), key=rank_key)

    u30 = sorted(etfs_u30) + stocks_ranked[:15]
    u50_extra_etfs = ["SOXX"] if eligible("SOXX") else []
    u50 = sorted(etfs_u30 + u50_extra_etfs) + stocks_ranked[: 15 + 19]

    return {
        "rule": "ledger L3 + XLC amendment: 4 broad + 10 sector + SMH + top-15 stocks (U30); "
        "+ SOXX + next-19 stocks (U50); rank by median session dollar volume desc, "
        "tie lexicographic asc",
        "u30": u30,
        "u50": u50,
        "u30_size": len(u30),
        "u50_size": len(u50),
        "stock_ranking": stocks_ranked,
        "excluded": {
            symbol: row.get("reasons", [])
            for symbol, row in sorted(eligibility.items())
            if not row.get("eligible", False)
        },
    }


__all__ = [
    "BROAD_ETFS",
    "CONTEXT_ONLY",
    "DOWNLOAD_SYMBOLS",
    "INCUMBENTS",
    "MAX_MISSING_FRACTION",
    "SECTOR_ETFS",
    "SECTOR_OF",
    "SEMI_ETFS",
    "STOCK_POOL",
    "build_manifests",
    "liquidity_metric",
]
