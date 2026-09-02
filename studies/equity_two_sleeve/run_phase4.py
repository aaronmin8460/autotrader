"""Phase-4 runner: robustness attacks on blend candidates (ledger §L8, §L9).

Every replay-based attack pairs the attacked blend with the identically
attacked T0 (sleeve E at full budget). Year/window removals come from stored
window returns.

Usage:
    python -m studies.equity_two_sleeve.run_phase4 --stage year-window
    python -m studies.equity_two_sleeve.run_phase4 --stage attacks --blend B30
    python -m studies.equity_two_sleeve.run_phase4 --stage loso --blend B30
    python -m studies.equity_two_sleeve.run_phase4 --stage perturb --blend B30
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from studies.equity_deep_arch.evaluate import write_json
from studies.equity_two_sleeve import REPORT_ROOT
from studies.equity_two_sleeve.blend import (
    RATIOS,
    a_sleeve_targets,
    combine_targets,
    e_sleeve_targets,
    replay_blend,
)

OUT = Path(REPORT_ROOT) / "phase4"

STRONGEST_SYMBOL = "NVDA"
YEAR_2024_WINDOWS = ("w06", "w07", "w08")


def _log(message: str) -> None:
    print(message, flush=True)


def _windows_net(window_returns: dict[str, float], drop: tuple[str, ...]) -> float:
    net = 1.0
    for name, value in window_returns.items():
        if name not in drop:
            net *= 1.0 + value
    return net - 1.0


def run_year_window() -> None:
    """Strongest-year (2024 = w06–w08) and strongest-window removal for every
    Phase-2 row plus T0, from stored window returns."""
    blends = json.loads((Path(REPORT_ROOT) / "phase2" / "blends.json").read_text())
    bridge = json.loads((Path(REPORT_ROOT) / "baseline" / "bridge_u10.json").read_text())[
        "EDA1_weighted_bridge"
    ]

    payload: dict[str, object] = {}
    rows = [("T0_bridge", bridge)]
    for label in (*RATIOS, "CTRL_SE_90", *(f"CTRL_GEN_{k[1:]}" for k in RATIOS)):
        rows.append((label, blends[label]))
    for name, block in rows:
        primary = block["equity-marketable"]
        returns = primary["window_returns"]
        strongest = max(returns, key=lambda w: returns[w])
        payload[name] = {
            "net": primary["net_return"],
            "strongest_window": strongest,
            "net_drop_strongest_window": _windows_net(returns, (strongest,)),
            "net_drop_2024": _windows_net(returns, YEAR_2024_WINDOWS),
        }
        _log(f"{name}: {payload[name]}")
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "year_window.json", payload)


class AttackContext:
    """Shared frames/state for the replay attacks, loaded once."""

    def __init__(self) -> None:
        from studies.equity_asset_character.run_phase5 import TiltContext

        self.tilt = TiltContext("u30")
        self.frames = self.tilt.context.frames
        self.sessions = self.tilt.context.sessions
        self.participate = self.tilt.context.participate
        self.stance = self.tilt.context.stance
        self.states = self.tilt.context.states

    def sleeve_e(self, exclude: frozenset[str] = frozenset()):
        return e_sleeve_targets(
            self.frames, self.sessions, self.participate, self.stance, exclude_symbols=exclude
        )

    def sleeve_a(self, exclude: frozenset[str] = frozenset(), *, delay: bool = False):
        return a_sleeve_targets(self.tilt, exclude_symbols=exclude, delay_one_session=delay)

    def blend_row(self, label: str, s_e: float, s_a: float, targets_e, targets_a):
        combined = combine_targets([(s_e, targets_e), (s_a, targets_a)])
        return replay_blend(self.frames, combined, label, self.states)

    def t0_row(self, label: str, targets_e):
        return replay_blend(self.frames, targets_e, label, self.states)


def run_attacks(blend: str) -> None:
    from studies.equity_asset_character.run_phase9 import strongest_archetype_members

    s_e, s_a = RATIOS[blend]
    context = AttackContext()
    arch_members = frozenset(strongest_archetype_members())
    nvda = frozenset({STRONGEST_SYMBOL})

    payload: dict[str, object] = {
        "blend": blend,
        "budgets": [s_e, s_a],
        "strongest_archetype_members": sorted(arch_members),
    }

    base_e = context.sleeve_e()
    base_a = context.sleeve_a()

    jobs = (
        (
            "blend_ex_nvda",
            lambda: context.blend_row(
                f"{blend}_ex_nvda", s_e, s_a, context.sleeve_e(nvda), context.sleeve_a(nvda)
            ),
        ),
        ("t0_ex_nvda", lambda: context.t0_row("T0_ex_nvda", context.sleeve_e(nvda))),
        (
            "blend_ex_nvda_e_only",
            lambda: context.blend_row(
                f"{blend}_ex_nvda_e_only", s_e, s_a, context.sleeve_e(nvda), base_a
            ),
        ),
        (
            "blend_ex_nvda_a_only",
            lambda: context.blend_row(
                f"{blend}_ex_nvda_a_only", s_e, s_a, base_e, context.sleeve_a(nvda)
            ),
        ),
        (
            "blend_ex_archetype",
            lambda: context.blend_row(
                f"{blend}_ex_arch",
                s_e,
                s_a,
                context.sleeve_e(arch_members),
                context.sleeve_a(arch_members),
            ),
        ),
        ("t0_ex_archetype", lambda: context.t0_row("T0_ex_arch", context.sleeve_e(arch_members))),
        (
            "blend_delay1",
            lambda: context.blend_row(
                f"{blend}_delay1", s_e, s_a, base_e, context.sleeve_a(delay=True)
            ),
        ),
    )
    for name, job in jobs:
        started = time.perf_counter()
        payload[name] = job()
        _log(f"{name}: done in {time.perf_counter() - started:.0f}s")

    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / f"attacks_{blend}.json", payload)


def run_loso(blend: str) -> None:
    s_e, s_a = RATIOS[blend]
    context = AttackContext()
    payload: dict[str, object] = {"blend": blend, "budgets": [s_e, s_a]}
    results: dict[str, object] = {}
    for symbol in context.tilt.context.universe:
        started = time.perf_counter()
        exclude = frozenset({symbol})
        block = context.blend_row(
            f"{blend}_ex_{symbol}", s_e, s_a, context.sleeve_e(exclude), context.sleeve_a(exclude)
        )
        primary = block["equity-marketable"]
        results[symbol] = {
            "net_return": primary["net_return"],
            "sharpe": float(primary["metrics"]["sharpe_ratio"]),
            "max_drawdown": float(primary["metrics"]["max_drawdown"]),
        }
        _log(
            f"LOSO {symbol}: net {primary['net_return']:+.4f} "
            f"({time.perf_counter() - started:.0f}s)"
        )
    payload["loso"] = results
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / f"loso_{blend}.json", payload)


def run_perturb(blend: str) -> None:
    """§L9: s_A ± 0.05, s_E adjusted to keep cash at 0.10."""
    s_e, s_a = RATIOS[blend]
    context = AttackContext()
    base_e = context.sleeve_e()
    base_a = context.sleeve_a()
    payload: dict[str, object] = {"blend": blend, "primary_budgets": [s_e, s_a]}
    for delta in (-0.05, 0.05):
        p_a = round(s_a + delta, 4)
        p_e = round(0.90 - p_a, 4)
        label = f"{blend}_perturb_a{p_a:g}"
        started = time.perf_counter()
        block = context.blend_row(label, p_e, p_a, base_e, base_a)
        block["budgets"] = [p_e, p_a]
        payload[label] = block
        _log(f"{label}: done in {time.perf_counter() - started:.0f}s")
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / f"perturb_{blend}.json", payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", required=True, choices=("year-window", "attacks", "loso", "perturb")
    )
    parser.add_argument("--blend", default="B30")
    arguments = parser.parse_args()
    started = time.perf_counter()
    if arguments.stage == "year-window":
        run_year_window()
    elif arguments.stage == "attacks":
        run_attacks(arguments.blend)
    elif arguments.stage == "loso":
        run_loso(arguments.blend)
    else:
        run_perturb(arguments.blend)
    _log(f"stage {arguments.stage} complete in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
