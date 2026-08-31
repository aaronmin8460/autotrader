"""Aggregation of simulation records into the study's report artifacts.

Reads the checkpointed per-quarter simulation units and writes the
machine-readable tables the mandate names. Pure aggregation — nothing
here re-simulates, and re-running it is idempotent.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

from studies.crypto_maker_execution.run_sim import load_all
from studies.crypto_maker_execution.venue import (
    maker_fee_only_break_even,
    taker_baseline_break_even,
)

PRIMARY_NOTIONAL = 10_000.0


def report_root() -> Path:
    qa = os.environ.get("AUTOTRADER_QA", "/Volumes/AUTOTRADER_QA")
    return Path(qa) / "reports" / "crypto-maker-execution"


def frame(mode: str) -> pd.DataFrame:
    df = pd.DataFrame(load_all(mode))
    if df.empty:
        raise RuntimeError(f"no simulation records for mode {mode}")
    df["era"] = df["decision_ts"].str[:4]
    df.loc[df["decision_ts"].str[:7] == "2026-08", "era"] = "2026-08"
    df["trend_aligned"] = None
    has_trend = df["trend_14d"].notna() if "trend_14d" in df else pd.Series(False, index=df.index)
    if has_trend.any():
        aligned = ((df["side"] == "buy") & (df["trend_14d"] > 0)) | (
            (df["side"] == "sell") & (df["trend_14d"] < 0)
        )
        df.loc[has_trend, "trend_aligned"] = aligned[has_trend]
    return df


def ok_primary(df: pd.DataFrame) -> pd.DataFrame:
    return df[(df["status"] == "OK") & (df["notional"] == PRIMARY_NOTIONAL)].copy()


def fill_statistics(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for keys, group in df.groupby(["policy", "scenario", "symbol", "side"]):
        outcome = group["outcome"].value_counts()
        waits = group["wait_to_first_fill_s"].dropna()
        full_waits = group["wait_to_full_fill_s"].dropna()
        rows.append(
            {
                "policy": keys[0],
                "scenario": keys[1],
                "symbol": keys[2],
                "side": keys[3],
                "events": len(group),
                "fully_filled": int(outcome.get("FULLY_FILLED", 0)),
                "partially_filled": int(outcome.get("PARTIALLY_FILLED", 0)),
                "timed_out": int(outcome.get("TIMED_OUT", 0)),
                "price_moved_away": int(outcome.get("PRICE_MOVED_AWAY", 0)),
                "not_filled": int(outcome.get("NOT_FILLED", 0)),
                "full_fill_rate": float((group["outcome"] == "FULLY_FILLED").mean()),
                "any_fill_rate": float((group["fill_fraction"] > 0).mean()),
                "mean_fill_fraction": float(group["fill_fraction"].mean()),
                "median_wait_first_s": float(waits.median()) if len(waits) else None,
                "p90_wait_first_s": float(waits.quantile(0.9)) if len(waits) else None,
                "median_wait_full_s": float(full_waits.median()) if len(full_waits) else None,
            }
        )
    return pd.DataFrame(rows)


def partial_fill_statistics(df: pd.DataFrame) -> pd.DataFrame:
    partial = df[df["outcome"] == "PARTIALLY_FILLED"]
    rows = []
    for keys, group in partial.groupby(["policy", "scenario", "symbol"]):
        rows.append(
            {
                "policy": keys[0],
                "scenario": keys[1],
                "symbol": keys[2],
                "events": len(group),
                "mean_fill_fraction": float(group["fill_fraction"].mean()),
                "median_fill_fraction": float(group["fill_fraction"].median()),
                "mean_fill_count": float(group["fill_count"].mean()),
                "median_wait_first_s": float(group["wait_to_first_fill_s"].median()),
            }
        )
    return pd.DataFrame(rows)


ADVERSE_COLUMNS = [
    "adverse_15s_bps",
    "adverse_1m_bps",
    "adverse_5m_bps",
    "adverse_15m_bps",
    "adverse_24h_bps",
]


def adverse_selection(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    filled = df[df["fill_fraction"] > 0]
    rows = []
    for keys, group in filled.groupby(by):
        row = dict(zip(by, keys if isinstance(keys, tuple) else (keys,), strict=True))
        row["fills"] = len(group)
        for column in ADVERSE_COLUMNS:
            values = group[column].dropna()
            row[f"mean_{column}"] = float(values.mean()) if len(values) else None
            row[f"median_{column}"] = float(values.median()) if len(values) else None
        for column in ("spread_component_bps", "drift_to_fill_bps", "maker_shortfall_bps"):
            values = group[column].dropna()
            row[f"mean_{column}"] = float(values.mean()) if len(values) else None
        rows.append(row)
    return pd.DataFrame(rows)


def missed_fill_cost(df: pd.DataFrame) -> pd.DataFrame:
    missed = df[df["fill_fraction"] < 1.0]
    rows = []
    for keys, group in missed.groupby(["policy", "scenario", "symbol", "side"]):
        opportunity = group["missed_opportunity_bps"].dropna()
        retouch = group["limit_retouched_24h"].dropna()
        hours = group["hours_to_retouch"].dropna()
        rows.append(
            {
                "policy": keys[0],
                "scenario": keys[1],
                "symbol": keys[2],
                "side": keys[3],
                "unfilled_or_partial": len(group),
                "mean_missed_opportunity_bps": (
                    float(opportunity.mean()) if len(opportunity) else None
                ),
                "median_missed_opportunity_bps": (
                    float(opportunity.median()) if len(opportunity) else None
                ),
                "retouch_rate_24h": float(retouch.mean()) if len(retouch) else None,
                "median_hours_to_retouch": float(hours.median()) if len(hours) else None,
            }
        )
    return pd.DataFrame(rows)


def effective_cost(df: pd.DataFrame) -> pd.DataFrame:
    completed = df[df["policy"].isin(["P3_FALLBACK", "P4_LONG"])]
    rows = []
    for keys, group in completed.groupby(["policy", "scenario", "symbol", "era"]):
        sides = group.groupby("side")["completed_one_way_bps"].mean()
        if "buy" not in sides or "sell" not in sides:
            continue
        rows.append(
            {
                "policy": keys[0],
                "scenario": keys[1],
                "symbol": keys[2],
                "era": keys[3],
                "events_per_side": int(len(group) / 2),
                "one_way_buy_bps": float(sides["buy"]),
                "one_way_sell_bps": float(sides["sell"]),
                "round_trip_bps": float(sides["buy"] + sides["sell"]),
                "mean_fill_fraction": float(group["fill_fraction"].mean()),
                "mean_fallback_fraction": float(group["fallback_fraction"].mean()),
                "mean_fallback_cost_bps": float(group["fallback_cost_bps"].dropna().mean()),
                "mean_maker_leg_bps": float(group["maker_leg_cost_bps"].dropna().mean()),
            }
        )
    return pd.DataFrame(rows)


def effective_cost_pooled(df: pd.DataFrame) -> pd.DataFrame:
    completed = df[df["policy"].isin(["P3_FALLBACK", "P4_LONG"])]
    rows = []
    for keys, group in completed.groupby(["policy", "scenario", "symbol"]):
        sides = group.groupby("side")["completed_one_way_bps"].agg(["mean", "sem", "count"])
        if "buy" not in sides.index or "sell" not in sides.index:
            continue
        round_trip = float(sides.loc["buy", "mean"] + sides.loc["sell", "mean"])
        sem = float((sides.loc["buy", "sem"] ** 2 + sides.loc["sell", "sem"] ** 2) ** 0.5)
        rows.append(
            {
                "policy": keys[0],
                "scenario": keys[1],
                "symbol": keys[2],
                "events_per_side": int(sides.loc["buy", "count"]),
                "round_trip_bps": round_trip,
                "round_trip_sem_bps": sem,
                "full_fill_rate": float((group["outcome"] == "FULLY_FILLED").mean()),
                "mean_fallback_fraction": float(group["fallback_fraction"].mean()),
            }
        )
    return pd.DataFrame(rows)


def cost_thresholds(pooled: pd.DataFrame) -> dict:
    verdicts: dict = {
        "taker_baseline_bps": taker_baseline_break_even() * 1e4,
        "maker_fee_only_floor_bps": maker_fee_only_break_even() * 1e4,
        "thresholds": {},
    }
    for threshold in (50.0, 40.0, 30.0, 20.0):
        supporting = pooled[pooled["round_trip_bps"] <= threshold]
        verdicts["thresholds"][f"<={int(threshold)}bps"] = {
            "any_cell_supports": bool(len(supporting)),
            "supporting_cells": supporting[
                ["policy", "scenario", "symbol", "round_trip_bps"]
            ].to_dict("records"),
        }
    return verdicts


def write_artifacts(mode: str) -> None:
    df = ok_primary(frame(mode))
    root = report_root()
    fill_statistics(df).to_csv(root / "fill_statistics.csv", index=False)
    partial_fill_statistics(df).to_csv(root / "partial_fill_statistics.csv", index=False)
    adverse_selection(df, ["policy", "scenario", "symbol", "side"]).to_csv(
        root / "adverse_selection.csv", index=False
    )
    adverse_selection(df, ["scenario", "symbol", "trend_aligned"]).to_csv(
        root / "markout_metrics.csv", index=False
    )
    missed_fill_cost(df).to_csv(root / "missed_fill_cost.csv", index=False)
    effective_cost(df).to_csv(root / "effective_cost_by_era.csv", index=False)
    pooled = effective_cost_pooled(df)
    pooled.to_csv(root / "effective_cost.csv", index=False)
    pooled.to_csv(root / "policy_comparison.csv", index=False)
    (root / "cost_thresholds.json").write_text(json.dumps(cost_thresholds(pooled), indent=1))
    print(f"artifacts written to {root} from mode={mode}, records={len(df)}")


if __name__ == "__main__":
    write_artifacts(sys.argv[1] if len(sys.argv) > 1 else "full")
