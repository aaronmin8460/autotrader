"""Rendering the pilot's JSON artifacts as the tables the report is written around.

Separated from the runner so the report can be regenerated from stored results
without re-scoring anything, and so every number in the markdown has one
traceable source: a key in a JSON file this module read.

Nothing is computed here that was not computed during the run. A table that
needed a new calculation would be a result nobody audited.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

#: Metrics the pilot reports per engine, and how each is rendered.
METRIC_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("total_return", "Return", "pct"),
    ("max_drawdown", "MaxDD", "pct"),
    ("sharpe_ratio", "Sharpe", "num"),
    ("trade_count", "Trades", "int"),
    ("win_rate", "Win%", "pct"),
    ("profit_factor", "PF", "num"),
    ("turnover", "Turnover", "num"),
    ("exposure", "Exposure", "pct"),
    ("cost_drag", "CostDrag", "pct"),
)


def load(path: Path) -> Mapping[str, object]:
    """Read one JSON artifact."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _pct(value: object) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.2f}%"


def _num(value: object) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.3f}"


def _int(value: object) -> str:
    if value is None:
        return "n/a"
    return str(int(float(value)))


_RENDERERS = {"pct": _pct, "num": _num, "int": _int}


def table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    """A GitHub-flavoured markdown table."""
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def dataset_table(results: Sequence[Mapping[str, object]]) -> str:
    """Provenance and quality, one row per symbol."""
    rows = []
    for result in results:
        data = result["dataset"]
        gaps = data["gaps"]
        rows.append(
            [
                str(data["symbol"]),
                f"{data['provider']}/{data['feed']}",
                str(data["adjustment"]),
                f"{data['requested_start']}..{data['requested_end']}",
                _int(data["regular_session_rows"]),
                _int(data["extended_hours_rows_dropped"]),
                _int(gaps["missing_bars"]),
                _pct(gaps["missing_fraction"]),
                "yes" if data["validation_ok"] else "NO",
                str(data["frame_sha256"])[:16],
            ]
        )
    return table(
        [
            "Symbol",
            "Provider/feed",
            "Adj",
            "Range",
            "Session bars",
            "Ext dropped",
            "Missing",
            "Missing %",
            "Valid",
            "sha256 (16)",
        ],
        rows,
    )


def coverage_table(result: Mapping[str, object]) -> str:
    """What each scoring window contains, from the calendar rather than from a claim."""
    rows = []
    for window in result["coverage"]["windows"]:
        rows.append(
            [
                str(window["name"]),
                f"{window['start']}..{window['end']}",
                _int(window["sessions"]),
                _int(window["observed_bars"]),
                "yes" if window["dst_transition"] else "-",
                ", ".join(window["early_closes"]) or "-",
                ", ".join(window["sessions_with_no_data"]) or "-",
            ]
        )
    return table(
        ["Window", "Dates", "Sessions", "Bars", "DST", "Early closes", "No data"],
        rows,
    )


def aggregation_table(results: Sequence[Mapping[str, object]]) -> str:
    """Derived-bar legality and yield, per symbol and timeframe."""
    rows = []
    for result in results:
        symbol = str(result["symbol"])
        spans = {entry["timeframe"]: entry for entry in result["aggregation"]["spanning"]}
        yields = {entry["timeframe"]: entry for entry in result["aggregation"]["yield"]}
        causal = {entry["timeframe"]: entry for entry in result["aggregation"]["causality"]}
        for label in ("15m", "1h", "4h"):
            span = spans[label]
            per_length = yields[label]["by_session_length"]
            full = per_length.get("26", per_length.get(26, {}))
            half = per_length.get("14", per_length.get(14, {}))
            rows.append(
                [
                    symbol,
                    label,
                    _int(span["derived_bars"]),
                    _int(span["spanning_bars"]),
                    _int(span["incomplete_constituent_bars"]),
                    _int(causal[label]["violations"]),
                    _num(full.get("mean")) if full else "n/a",
                    _num(half.get("mean")) if half else "n/a",
                ]
            )
    return table(
        [
            "Symbol",
            "TF",
            "Derived bars",
            "Span violations",
            "Short buckets",
            "Causality violations",
            "Per full session",
            "Per early close",
        ],
        rows,
    )


def walkforward_table(results: Sequence[Mapping[str, object]]) -> str:
    """V4's training plan, one row per model."""
    rows = []
    for result in results:
        for model in result["walk_forward"]["models"]:
            rows.append(
                [
                    str(model["symbol"]),
                    str(model["window"]),
                    _int(model["training_rows"]),
                    str(model["training_last_bar_utc"])[:16],
                    str(model["scoring_first_bar_utc"])[:16],
                    _int(model["gap_bars"]),
                    _int(model["labels_spanning_session_gap"]),
                    str(model["selected_family"]),
                    _num(model["selected_log_loss"]),
                ]
            )
    return table(
        [
            "Symbol",
            "Window",
            "Train rows",
            "Last train bar",
            "First scored bar",
            "Gap",
            "Overnight labels",
            "Selected",
            "Log loss",
        ],
        rows,
    )


def metrics_table(
    results: Sequence[Mapping[str, object]],
    *,
    cost_label: str,
) -> str:
    """Per-engine metrics under one cost model, aggregated over every window.

    Aggregated by compounding the per-window returns, which is what holding the
    strategy through all six windows would have produced - and stated as such,
    because the windows are not contiguous and this is therefore a diagnostic
    rather than a track record.
    """
    rows = []
    for result in results:
        symbol = str(result["symbol"])
        totals: dict[str, dict[str, float]] = {}
        for window in result["windows"]:
            for engine in window["engines"]:
                name = str(engine["engine"])
                metrics = engine["metrics"][cost_label]
                bucket = totals.setdefault(
                    name,
                    {
                        "compound": 1.0,
                        "trades": 0.0,
                        "signals": 0.0,
                        "worst_dd": 0.0,
                        "exposure": 0.0,
                        "cost": 0.0,
                        "windows": 0.0,
                    },
                )
                bucket["compound"] *= 1.0 + float(metrics.get("total_return") or 0.0)
                bucket["trades"] += float(metrics.get("trade_count") or 0)
                bucket["signals"] += float(engine["signals"])
                bucket["worst_dd"] = min(
                    bucket["worst_dd"], float(metrics.get("max_drawdown") or 0.0)
                )
                bucket["exposure"] += float(metrics.get("exposure") or 0.0)
                bucket["cost"] += float(metrics.get("cost_drag") or 0.0)
                bucket["windows"] += 1
        for name, bucket in totals.items():
            windows = bucket["windows"] or 1
            rows.append(
                [
                    symbol,
                    name,
                    _pct(bucket["compound"] - 1.0),
                    _pct(bucket["worst_dd"]),
                    _int(bucket["signals"]),
                    _int(bucket["trades"]),
                    _pct(bucket["exposure"] / windows),
                    _pct(bucket["cost"]),
                ]
            )
    return table(
        [
            "Symbol",
            "Engine",
            "Compounded return",
            "Worst window MaxDD",
            "Signals",
            "Round trips",
            "Mean exposure",
            "Cost drag",
        ],
        rows,
    )


def integrity_table(results: Sequence[Mapping[str, object]]) -> str:
    """The checks that make every other table admissible."""
    rows = []
    for result in results:
        symbol = str(result["symbol"])
        for window in result["windows"]:
            for engine in window["engines"]:
                rows.append(
                    [
                        symbol,
                        str(window["window"]["name"]),
                        str(engine["engine"]),
                        _int(engine["decisions"]),
                        _int(engine["insufficient_history"]),
                        _int(len(engine["live_series_mismatches"])),
                        _int(engine["overnight_fills"]),
                    ]
                )
    return table(
        [
            "Symbol",
            "Window",
            "Engine",
            "Decisions",
            "Insufficient history",
            "Series mismatches",
            "Overnight fills",
        ],
        rows,
    )


def leakage_table(report: Mapping[str, object]) -> str:
    """The causality verdict, one row per engine and symbol."""
    rows = []
    for audit in report["audits"]:
        rows.append(
            [
                str(audit["symbol"]),
                str(audit["engine"]),
                "yes" if audit["audit_ready"] else "no",
                _int(audit["probe_count"]),
                _int(audit["scored_bars"]),
                _int(audit["changed_decisions"]),
                _int(audit["vacuous_probes"]),
                "PASS" if audit["ok"] else "FAIL",
            ]
        )
    return table(
        [
            "Symbol",
            "Engine",
            "Audit-ready",
            "Probes",
            "Scored bars",
            "Changed decisions",
            "Vacuous probes",
            "Verdict",
        ],
        rows,
    )


__all__ = [
    "METRIC_COLUMNS",
    "aggregation_table",
    "coverage_table",
    "dataset_table",
    "integrity_table",
    "leakage_table",
    "load",
    "metrics_table",
    "table",
    "walkforward_table",
]
