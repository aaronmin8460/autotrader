"""Sleeve target construction and blending (ledger §L2–§L4).

A sleeve is a per-symbol, per-bar target-weight series expressed as a share
of the WHOLE portfolio's equity at full sleeve budget 1.0. Blending scales
each sleeve by its budget and sums per ticker:

    target_i(bar) = Σ_sleeves s_k × w_k_i(bar)

then applies the hard combined per-symbol cap (0.10, the operational
PER_SYMBOL_CAP lineage) with the excess left in cash. The 0.10 strategic
cash floor is structural: sleeve budgets sum to 0.90 and nothing here ever
reallocates an inactive sleeve's budget to another sleeve — defensive cash
stays cash.

Everything downstream (fills, costs, metrics) is the inherited validated
`replay_weighted` machinery, untouched.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pandas as pd

from studies.equity_eda1_nextgen.run_phase234 import build_targets, equal_weights
from studies.equity_eda1_nextgen.universe import INCUMBENTS

#: Hard combined per-symbol ceiling — the operational risk lineage's cap.
COMBINED_CAP = 0.10

#: The predeclared candidates: label → (s_E, s_A); cash = 1 − s_E − s_A = 0.10.
RATIOS: dict[str, tuple[float, float]] = {
    "B20": (0.70, 0.20),
    "B30": (0.60, 0.30),
    "B40": (0.50, 0.40),
}

Targets = dict[str, dict[pd.Timestamp, float]]


class BlendError(Exception):
    """A blend construction that cannot honour the ledger's semantics."""


def e_sleeve_targets(
    frames: Mapping[str, pd.DataFrame],
    sessions: Sequence,
    participate: Mapping,
    stance: Mapping[str, Mapping[pd.Timestamp, int]],
    *,
    exclude_symbols: frozenset[str] = frozenset(),
) -> Targets:
    """Sleeve E: the EDA-1 U10 bridge weight rule at full budget.

    Equal min(1/n, 0.10) active weights while PARTICIPATE; reserved
    min(1/n, 0.10) × V3 stance while DEFENSIVE. With the full ten names both
    are exactly 0.10 — the stored `EDA1_BRIDGE` rule. Exclusions renormalize
    to the remaining names, subject to the same inherited cap.
    """
    members = tuple(sorted(s for s in INCUMBENTS if s not in exclude_symbols))
    if not members:
        raise BlendError("Sleeve E requires at least one member.")
    slots = len(members)
    sleeve_frames = {s: frames[s] for s in members}
    sleeve_stance = {s: stance[s] for s in members}
    membership = {session: members for session in sessions}
    weights = {session: equal_weights(members, slots) for session in sessions}
    reserved = min(1.0 / slots, COMBINED_CAP)
    return build_targets(
        sleeve_frames,
        sessions,
        participate,
        membership,
        sleeve_stance,
        active_weight_of=weights,
        reserved_weight=reserved,
    )


def g_sleeve_targets(
    frames: Mapping[str, pd.DataFrame],
    universe: Sequence[str],
    sessions: Sequence,
    participate: Mapping,
    stance: Mapping[str, Mapping[pd.Timestamp, int]],
) -> Targets:
    """Generic sleeve: equal-weight all-eligible over `universe` (the U30
    ALL_ELIGIBLE rule) at full budget — the "breadth without A1-B" control."""
    members = tuple(sorted(universe))
    slots = len(members)
    membership = {session: members for session in sessions}
    weights = {session: equal_weights(members, slots) for session in sessions}
    reserved = min(1.0 / slots, COMBINED_CAP)
    return build_targets(
        {s: frames[s] for s in members},
        sessions,
        participate,
        membership,
        {s: stance[s] for s in members},
        active_weight_of=weights,
        reserved_weight=reserved,
    )


def a_sleeve_targets(
    context,
    scheme: str = "A1_B",
    *,
    exclude_symbols: frozenset[str] = frozenset(),
    delay_one_session: bool = False,
    clip_override: tuple[float, float] | None = None,
) -> Targets:
    """Sleeve A: the frozen A1-B tilted weights at full budget.

    Exactly the target-construction half of the inherited
    `TiltContext.evaluate` (asset-character §L8), returned instead of
    replayed, so it can be scaled and summed with other sleeves. `context`
    is a `studies.equity_asset_character.run_phase5.TiltContext`.
    """
    from studies.equity_asset_character.allocation import build_targets_tilted

    by_mark: dict = {}
    active_of: dict = {}
    reserved_of: dict = {}
    ordered_sessions = sorted(context.context.sessions)
    delayed_of = {
        current: context.mark_of_session.get(previous)
        for previous, current in zip(ordered_sessions[:-1], ordered_sessions[1:], strict=False)
    }
    for session in context.context.sessions:
        if delay_one_session:
            mark = delayed_of.get(session)
        else:
            mark = context.mark_of_session.get(session)
        if mark is None:
            mark = context.marks[0]
        if mark not in by_mark:
            by_mark[mark] = context.mark_weights(
                scheme,
                mark,
                clip_override=clip_override,
                exclude_symbols=exclude_symbols,
            )
        active_of[session], reserved_of[session] = by_mark[mark]

    frames = {s: f for s, f in context.context.frames.items() if s not in exclude_symbols}
    stance = {s: v for s, v in context.context.stance.items() if s not in exclude_symbols}
    return build_targets_tilted(
        frames,
        context.context.sessions,
        context.context.participate,
        stance,
        active_weight_of=active_of,
        reserved_weight_of=reserved_of,
    )


def scale_targets(targets: Targets, factor: float) -> Targets:
    """Every weight × factor — a sleeve at a partial budget."""
    if factor < 0.0 or factor > 1.0:
        raise BlendError(f"Scale factor {factor} outside [0, 1].")
    return {
        symbol: {stamp: weight * factor for stamp, weight in series.items()}
        for symbol, series in targets.items()
    }


def combine_targets(
    components: Sequence[tuple[float, Targets]],
    *,
    cap: float = COMBINED_CAP,
) -> Targets:
    """Σ scale × sleeve, one final target per ticker, capped at `cap`.

    Shared symbols must carry identical bar keys in every sleeve that holds
    them (they do by construction — all sleeves are built from the same
    frames); this is asserted, not assumed. Deterministic and input-order
    invariant: symbols and bars are dictionary-merged by key, and addition
    is commutative over the (scale, weight) pairs.
    """
    combined: Targets = {}
    for scale, targets in components:
        if scale < 0.0:
            raise BlendError(f"Sleeve scale {scale} is negative.")
        for symbol in targets:
            series = targets[symbol]
            if symbol not in combined:
                combined[symbol] = {stamp: scale * weight for stamp, weight in series.items()}
                continue
            existing = combined[symbol]
            if existing.keys() != series.keys():
                raise BlendError(f"{symbol}: sleeve bar keys differ; frames are misaligned.")
            for stamp, weight in series.items():
                existing[stamp] += scale * weight
    total_budget = sum(scale for scale, _ in components)
    if total_budget > 1.0 + 1e-9:
        raise BlendError(f"Sleeve budgets sum to {total_budget} > 1.")
    for symbol in combined:
        series = combined[symbol]
        for stamp, weight in series.items():
            if weight > cap:
                series[stamp] = cap
    return {symbol: combined[symbol] for symbol in sorted(combined)}


def target_diagnostics(targets: Targets) -> dict[str, object]:
    """Concentration/overlap accounting from the target series alone.

    Per-bar totals and per-symbol maxima are exact properties of the target
    dict (pre-fill), which is what the ledger's concentration rules govern.
    """
    per_bar_total: dict[pd.Timestamp, float] = {}
    max_weight = 0.0
    max_weight_symbol = ""
    for symbol, series in targets.items():
        for stamp, weight in series.items():
            per_bar_total[stamp] = per_bar_total.get(stamp, 0.0) + weight
            if weight > max_weight:
                max_weight, max_weight_symbol = weight, symbol
    totals = sorted(per_bar_total.values())
    peak_bar_total = totals[-1] if totals else 0.0

    last_stamp = max(per_bar_total) if per_bar_total else None
    top5 = 0.0
    snapshot: dict[str, float] = {}
    if last_stamp is not None:
        snapshot = {
            symbol: series[last_stamp]
            for symbol, series in targets.items()
            if last_stamp in series and series[last_stamp] > 0.0
        }
        top5 = sum(sorted(snapshot.values(), reverse=True)[:5])
    return {
        "max_symbol_weight": max_weight,
        "max_symbol_weight_symbol": max_weight_symbol,
        "peak_bar_total_weight": peak_bar_total,
        "final_bar_top5_weight": top5,
        "final_bar_active_names": len(snapshot),
    }


def save_curve(result, path: Path) -> None:
    """Persist a WeightedResult's equity curve (primary-cost analysis input)."""
    frame = pd.DataFrame(
        {
            "timestamp": list(result.timestamps),
            "equity": list(result.equity_curve),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.parquet")
    frame.to_parquet(tmp, engine="pyarrow", index=False)
    tmp.rename(path)


def replay_blend(
    frames: Mapping[str, pd.DataFrame],
    targets: Targets,
    label: str,
    states,
    *,
    curve_dir: Path | None = None,
) -> dict[str, object]:
    """The inherited three-cost replay + report, with curve persistence."""
    from studies.equity_eda1_nextgen.run_phase234 import replay_weighted, weighted_report
    from studies.equity_v1_v5.scoring import COST_MODELS

    used = {s: frames[s] for s in targets}
    blocks: dict[str, object] = {}
    for cost_model in COST_MODELS:
        result = replay_weighted(used, targets, cost_model, label=label)
        blocks[cost_model.label] = weighted_report(result, states)
        if curve_dir is not None and cost_model.label == "equity-marketable":
            save_curve(result, curve_dir / f"{label}_{cost_model.label}.parquet")
    blocks["target_diagnostics"] = target_diagnostics(targets)
    return blocks


__all__ = [
    "COMBINED_CAP",
    "RATIOS",
    "BlendError",
    "Targets",
    "a_sleeve_targets",
    "combine_targets",
    "e_sleeve_targets",
    "g_sleeve_targets",
    "replay_blend",
    "save_curve",
    "scale_targets",
    "target_diagnostics",
]
