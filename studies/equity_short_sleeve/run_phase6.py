"""Phase 10/11 runner: robustness attacks, concentration, squeeze and gap
stress (ledger §L12).

Attacked rows are declared here and nowhere else: the index control
(`S1_SPY_10`), the primary hypothesis (`S3_N5_10`), and the U10 bridge
control (`S2_N2_10`). Every attack that is defined on the incumbent is
applied to B0 identically.

Usage:
    python -m studies.equity_short_sleeve.run_phase6 --stage attacks
    python -m studies.equity_short_sleeve.run_phase6 --stage loso
    python -m studies.equity_short_sleeve.run_phase6 --stage squeeze
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from autotrader.research.costs import EQUITY_COST
from studies.equity_deep_arch.evaluate import write_json
from studies.equity_short_sleeve import CHARACTER_DATASETS, REPORT_ROOT
from studies.equity_short_sleeve.attacks import (
    compound_excluding,
    max_drawdown_excluding,
    session_series,
    sessions_in_year,
    sharpe_excluding,
    short_pnl_by_year,
)
from studies.equity_short_sleeve.candidates import (
    MAX_SHORT_GROSS,
    apply_short_plan,
    index_short_plan,
    selected_short_plan,
    symbol_entries,
    unavailable_entries,
)
from studies.equity_short_sleeve.context import ShortContext
from studies.equity_short_sleeve.run_phase4 import (
    LONG_COST_STRESS,
    SHORT_COST_BASE,
    SHORT_COST_STRESS,
)
from studies.equity_short_sleeve.shorts import (
    BORROW_EXTREME,
    BORROW_HIGH,
    BORROW_ZERO,
    PRIMARY_BORROW,
    replay_signed,
)

OUT = Path(REPORT_ROOT) / "phase6"
ATTACKED = ("S1_SPY_10", "S3_N5_10", "S2_N2_10")


def _log(message: str) -> None:
    print(message, flush=True)


def build(context: ShortContext, label: str, **overrides):
    """Rebuild one attacked row's plan with overrides."""
    if label.startswith("S1"):
        return index_short_plan(
            context.sessions,
            context.participate,
            gross=overrides.pop("gross", 0.10),
            members=("SPY",),
        )
    if label.startswith("S2"):
        return selected_short_plan(
            context.sessions,
            context.participate,
            context.panel,
            context.mark_of,
            context.incumbents,
            label=label,
            gross=overrides.pop("gross", 0.10),
            names=overrides.pop("names", 2),
            exclude_symbols=overrides.pop("exclude_symbols", frozenset()),
        )
    return selected_short_plan(
        context.sessions,
        context.participate,
        context.panel,
        context.mark_of,
        context.universe,
        label=label,
        gross=overrides.pop("gross", 0.10),
        names=overrides.pop("names", 5),
        characteristic=overrides.pop("characteristic", "beta_252"),
        cohort_fraction=overrides.pop("cohort_fraction", 1.0 / 3.0),
        exclude_symbols=overrides.pop("exclude_symbols", frozenset()),
    )


def replay_row(
    context,
    targets,
    label,
    *,
    long_cost=EQUITY_COST,
    short_cost=SHORT_COST_BASE,
    borrow=PRIMARY_BORROW,
):
    return replay_signed(
        context.frames,
        targets,
        long_cost,
        label=label,
        short_cost_model=short_cost,
        borrow=borrow,
        max_short_gross=MAX_SHORT_GROSS + 0.01,
    )


def summarize(result, frame=None) -> dict[str, object]:
    metrics = result.metrics().to_json_dict()
    return {
        "net_return": result.net_return,
        "sharpe": metrics["sharpe_ratio"],
        "sortino": metrics["sortino_ratio"],
        "max_drawdown": metrics["max_drawdown"],
        "short_pnl_pct": result.short_pnl / result.initial_cash,
        "short_gross_mean": result.short_gross_mean,
        "net_exposure_mean": result.net_exposure_mean,
        "borrow_cost_pct": result.borrow_cost / result.initial_cash,
    }


def high_beta_members(universe: Sequence[str]) -> frozenset[str]:
    """The high-beta archetype's stable core, recovered mechanically.

    The prior program's frozen walk-forward fits are read read-only. For each
    fit, the archetype with the highest median `beta_252` among its own
    members is the high-beta lineage; a symbol belongs to the stable core if
    it is assigned to that lineage in a majority of the fits it appears in.
    Nothing is hand-listed, and the label numbering (which is not stable
    across fits, k = 3/3/3/5/4) is never trusted.
    """
    assignments = pd.read_parquet(Path(CHARACTER_DATASETS) / "archetype_assignments.parquet")
    panel = pd.read_parquet(Path(CHARACTER_DATASETS) / "fingerprints.parquet")
    panel["mark"] = [str(pd.Timestamp(m).date()) for m in panel["mark"]]
    assignments["mark"] = [str(pd.Timestamp(m).date()) for m in assignments["mark"]]
    merged = assignments.dropna(subset=["archetype"]).merge(
        panel[["mark", "symbol", "beta_252"]], on=["mark", "symbol"], how="left"
    )
    merged = merged[merged["symbol"].isin(set(universe))]
    hits: dict[str, int] = {}
    seen: dict[str, int] = {}
    for _fit, group in merged.groupby("fit_mark"):
        medians = group.groupby("archetype")["beta_252"].median()
        if medians.empty or medians.isna().all():
            continue
        top = medians.idxmax()
        members = set(group.loc[group["archetype"] == top, "symbol"].unique())
        for symbol in group["symbol"].unique():
            seen[symbol] = seen.get(symbol, 0) + 1
            if symbol in members:
                hits[symbol] = hits.get(symbol, 0) + 1
    return frozenset(s for s, n in hits.items() if n * 2 > seen.get(s, 0))


def run_attacks() -> None:
    context = ShortContext()
    b0 = replay_row(context, context.long_targets, "B0", borrow=BORROW_ZERO)
    b0_frame = session_series(b0)
    payload: dict[str, object] = {"B0": summarize(b0)}

    try:
        archetype = high_beta_members(context.universe)
    except Exception as error:  # noqa: BLE001 - recorded, not fatal
        archetype = frozenset()
        payload["archetype_read_error"] = str(error)
    payload["high_beta_archetype_members"] = sorted(archetype)

    for label in ATTACKED:
        _log(f"attacking {label}")
        base_plan = build(context, label)
        base_targets = apply_short_plan(
            context.long_targets, context.frames, base_plan, context.sessions
        )
        base = replay_row(context, base_targets, label)
        frame = session_series(base)
        by_year = short_pnl_by_year(frame)
        strongest_short_year = max(by_year, key=lambda y: by_year[y])
        contributors = base_plan.weight_of
        tally: dict[str, int] = {}
        for weights in contributors.values():
            for symbol in weights:
                tally[symbol] = tally.get(symbol, 0) + 1
        ranked = sorted(tally, key=lambda s: (-tally[s], s))

        block: dict[str, object] = {
            "base": summarize(base),
            "short_pnl_by_year": by_year,
            "strongest_short_year": strongest_short_year,
            "proposed_sessions_by_symbol": dict(sorted(tally.items(), key=lambda kv: -kv[1])),
        }

        # Year removals — applied identically to B0.
        for year in (2022, strongest_short_year):
            drop = sessions_in_year(frame, year)
            block[f"remove_{year}"] = {
                "candidate_net": compound_excluding(frame, drop),
                "candidate_sharpe": sharpe_excluding(frame, drop),
                "candidate_maxdd": max_drawdown_excluding(frame, drop),
                "b0_net": compound_excluding(b0_frame, sessions_in_year(b0_frame, year)),
                "b0_sharpe": sharpe_excluding(b0_frame, sessions_in_year(b0_frame, year)),
                "b0_maxdd": max_drawdown_excluding(b0_frame, sessions_in_year(b0_frame, year)),
                "sessions_removed": len(drop),
            }

        # Strongest bear window removal (w11, B0's deepest; and w09 fast crash).
        from studies.equity_10_full.windows import window_by_name

        for name in ("w11", "w09", "w02"):
            window = window_by_name(name)
            drop = [s for s in frame.index if window.start <= s <= window.end]
            block[f"remove_{name}"] = {
                "candidate_net": compound_excluding(frame, drop),
                "b0_net": compound_excluding(b0_frame, drop),
                "sessions_removed": len(drop),
            }

        # Symbol removals.
        removals: list[tuple[str, frozenset[str]]] = []
        if ranked:
            removals.append((f"remove_top_symbol_{ranked[0]}", frozenset(ranked[:1])))
        if len(ranked) >= 2:
            removals.append((f"remove_top2_{ranked[0]}_{ranked[1]}", frozenset(ranked[:2])))
        for symbol in ("NVDA", "TSLA", "AMD"):
            if symbol in tally:
                removals.append((f"remove_{symbol}", frozenset({symbol})))
        if archetype and not label.startswith("S1"):
            removals.append(("remove_high_beta_archetype", frozenset(archetype)))
        for name, excluded in removals:
            if label.startswith("S1"):
                continue  # the index control has no symbol-selection to attack
            plan = build(context, label, exclude_symbols=excluded)
            targets = apply_short_plan(context.long_targets, context.frames, plan, context.sessions)
            block[name] = summarize(replay_row(context, targets, f"{label}|{name}"))

        # Execution delay.
        for delay in (1, 2):
            targets = apply_short_plan(
                context.long_targets,
                context.frames,
                base_plan,
                context.sessions,
                delay_bars=delay,
            )
            block[f"delay_{delay}_bar"] = summarize(
                replay_row(context, targets, f"{label}|delay{delay}")
            )

        # Cost and borrow stress.
        block["cost_2x"] = summarize(
            replay_row(
                context,
                base_targets,
                f"{label}|cost2x",
                long_cost=LONG_COST_STRESS,
                short_cost=SHORT_COST_STRESS,
            )
        )
        for borrow in (BORROW_HIGH, BORROW_EXTREME):
            block[f"borrow_{borrow.label}"] = summarize(
                replay_row(context, base_targets, f"{label}|{borrow.label}", borrow=borrow)
            )

        # Availability stress.
        for fraction in (0.10, 0.30):
            blocked = unavailable_entries(base_plan, fraction)
            targets = apply_short_plan(
                context.long_targets,
                context.frames,
                base_plan,
                context.sessions,
                unavailable=blocked,
            )
            block[f"unavailable_{int(fraction * 100)}pct"] = {
                **summarize(replay_row(context, targets, f"{label}|unavail{fraction}")),
                "entries_blocked": len(blocked),
            }
        if ranked:
            blocked = symbol_entries(base_plan, ranked[0])
            targets = apply_short_plan(
                context.long_targets,
                context.frames,
                base_plan,
                context.sessions,
                unavailable=blocked,
            )
            block["best_short_unavailable"] = {
                **summarize(replay_row(context, targets, f"{label}|nobest")),
                "symbol": ranked[0],
                "entries_blocked": len(blocked),
            }

        # Weight perturbation.
        for gross in (0.05, 0.15):
            plan = build(context, label, gross=gross)
            targets = apply_short_plan(context.long_targets, context.frames, plan, context.sessions)
            block[f"gross_{int(gross * 100)}"] = summarize(
                replay_row(context, targets, f"{label}|g{gross}")
            )

        # Selection-threshold perturbation (S3 only).
        if label.startswith("S3"):
            for fraction, name in ((0.25, "quartile"), (0.40, "top40")):
                plan = build(context, label, cohort_fraction=fraction)
                targets = apply_short_plan(
                    context.long_targets, context.frames, plan, context.sessions
                )
                block[f"cohort_{name}"] = summarize(replay_row(context, targets, f"{label}|{name}"))

        payload[label] = block
        _log(f"  {label}: done")

    write_json(OUT / "attacks.json", payload)


def run_loso() -> None:
    """Leave-one-symbol-out across the short candidate universe."""
    context = ShortContext()
    payload: dict[str, object] = {}
    for label, universe in (("S3_N5_10", context.universe), ("S2_N2_10", context.incumbents)):
        rows: dict[str, object] = {}
        for symbol in universe:
            plan = build(context, label, exclude_symbols=frozenset({symbol}))
            targets = apply_short_plan(context.long_targets, context.frames, plan, context.sessions)
            result = replay_row(context, targets, f"{label}|no{symbol}")
            rows[symbol] = {
                "net_return": result.net_return,
                "short_pnl_pct": result.short_pnl / result.initial_cash,
                "sharpe": result.metrics().to_json_dict()["sharpe_ratio"],
            }
            _log(f"  {label} without {symbol}: net {result.net_return:+.4f}")
        payload[label] = rows
    write_json(OUT / "loso.json", payload)


def run_squeeze() -> None:
    """Recovery / short-squeeze stress and adverse-gap scenario analysis."""
    context = ShortContext()
    transitions = context.transitions_to_participate()
    payload: dict[str, object] = {"transitions": [str(t) for t in transitions]}

    spy = context.closes["SPY"]
    spy_ret = spy.pct_change()
    rebound_days = [s for s, r in spy_ret.items() if r is not None and r > 0.02]
    payload["spy_rebound_sessions_over_2pct"] = len(rebound_days)

    for label in ATTACKED:
        plan = build(context, label)
        targets = apply_short_plan(context.long_targets, context.frames, plan, context.sessions)
        result = replay_row(context, targets, label)
        frame = session_series(result)
        share = frame["short_pnl"] / frame["equity"].shift(1)
        rebound = [d for d in rebound_days if d in share.index]
        worst_days = share.nsmallest(10)
        block = {
            "short_pnl_on_spy_rebound_days_pct": float(share.loc[rebound].sum()),
            "spy_rebound_days_measured": len(rebound),
            "worst_10_sessions_pct": {str(d): float(v) for d, v in worst_days.items()},
            "worst_session_pct": float(share.min()),
            "worst_5session_rolling_pct": float(share.rolling(5).sum().min()),
        }
        # Adverse-gap scenario on the largest allowed short (3 % single name,
        # or the whole 10 % index hedge for S1). Clearly labelled as scenario.
        largest = 0.10 if label.startswith("S1") else 0.03
        block["gap_scenarios"] = {
            f"gap_up_{int(pct * 100)}pct": -largest * pct for pct in (0.05, 0.10, 0.20)
        }
        block["gap_scenarios_whole_book"] = {
            f"gap_up_{int(pct * 100)}pct": -result.short_gross_max * pct
            for pct in (0.05, 0.10, 0.20)
        }
        block["short_gross_max"] = result.short_gross_max
        payload[label] = block
        _log(f"  {label}: squeeze block done")
    write_json(OUT / "squeeze.json", payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("attacks", "loso", "squeeze"))
    arguments = parser.parse_args()
    started = time.perf_counter()
    {"attacks": run_attacks, "loso": run_loso, "squeeze": run_squeeze}[arguments.stage]()
    _log(f"stage {arguments.stage} complete in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
