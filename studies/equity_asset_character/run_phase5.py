"""Phase-5 runner: A1 archetype allocation replays (§L8), and later the A2
individual-tilt replays (§L9) on the best A1 configuration.

Usage:
    python -m studies.equity_asset_character.run_phase5 --stage a1 --universe u10
    python -m studies.equity_asset_character.run_phase5 --stage a1 --universe u30
    python -m studies.equity_asset_character.run_phase5 --stage a2 --universe u30 \\
        --scheme A1_P
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date
from pathlib import Path

from studies.equity_asset_character import REPORT_ROOT
from studies.equity_asset_character.allocation import (
    A1_SCHEMES,
    A2_COMPOSITES,
    archetype_multipliers,
    build_targets_tilted,
    governing_fit,
    governing_marks,
    load_fit_records,
    response_estimates,
    retro_labels,
    state_multipliers,
    tilted_weights,
)
from studies.equity_asset_character.fingerprints import (
    STATE_FEATURES,
    build_series,
    cross_sectional_z,
    symbol_sessions,
)
from studies.equity_asset_character.response import forward_observations
from studies.equity_asset_character.run_phase2 import load_panel
from studies.equity_asset_character.run_phase4 import load_marks_regimes
from studies.equity_deep_arch.evaluate import write_json
from studies.equity_eda1_nextgen.run_phase234 import (
    UniverseContext,
    load_frame,
    load_universe,
    replay_weighted,
    weighted_report,
)
from studies.equity_v1_v5.scoring import COST_MODELS

OUT_DIR = Path(REPORT_ROOT) / "phase5"


def surviving_features() -> tuple[str, ...]:
    stability = json.loads((Path(REPORT_ROOT) / "phase2" / "stability.json").read_text())
    return tuple(stability["surviving_structural_features"])


class TiltContext:
    """Everything one universe's tilted replays share, loaded once."""

    def __init__(self, universe_name: str) -> None:
        from studies.equity_eda1_nextgen.universe import INCUMBENTS

        self.universe_name = universe_name
        members = list(INCUMBENTS) if universe_name == "u10" else load_universe(universe_name)
        self.context = UniverseContext(members)
        self.marks, self.regime_of, _ = load_marks_regimes()
        panel = load_panel()
        self.z_structural = cross_sectional_z(panel, surviving_features())
        self.z_state = cross_sectional_z(panel, STATE_FEATURES)
        self.fit_records = load_fit_records()
        u45 = load_universe("u50")
        tables = {s: symbol_sessions(load_frame(s)) for s in u45}
        series = {s: build_series(t, tables["SPY"]) for s, t in tables.items()}
        self.observations = forward_observations(series, self.marks, 21)
        self.mark_of_session = governing_marks(self.context.sessions, self.marks)
        self._estimates_cache: dict[str, dict] = {}
        self._labels_cache: dict[tuple[str, date], dict[str, int]] = {}

    def estimates_for(self, fit_record: dict) -> dict:
        key = fit_record["fit_mark"]
        if key not in self._estimates_cache:
            self._estimates_cache[key] = response_estimates(
                fit_record, self.z_structural, self.observations, self.regime_of
            )
        return self._estimates_cache[key]

    def labels_at(self, fit_record: dict, mark: date) -> dict[str, int]:
        key = (fit_record["fit_mark"], mark)
        if key not in self._labels_cache:
            self._labels_cache[key] = retro_labels(fit_record, self.z_structural, mark)
        return self._labels_cache[key]

    def mark_weights(
        self,
        scheme: str,
        mark: date,
        *,
        composite: str | None = None,
        band: tuple[float, float] | None = None,
        lambda_override: float | None = None,
        clip_override: tuple[float, float] | None = None,
        exclude_symbols: frozenset[str] = frozenset(),
    ) -> tuple[dict[str, float], dict[str, float]]:
        """(active weights, reserved weights) for the sessions of one mark."""
        symbols = [s for s in self.context.universe if s not in exclude_symbols]
        fit = governing_fit(self.fit_records, mark)
        if fit is None:
            equal = tilted_weights(symbols, {})
            return equal, equal

        import studies.equity_asset_character.allocation as alloc

        old_lambda, old_clip = alloc.TILT_LAMBDA, alloc.MULT_CLIP
        if lambda_override is not None:
            alloc.TILT_LAMBDA = lambda_override
        if clip_override is not None:
            alloc.MULT_CLIP = clip_override
        try:
            labels = self.labels_at(fit, mark)
            estimates = self.estimates_for(fit)
            active_mults = archetype_multipliers(scheme, fit, "PARTICIPATE", estimates)
            reserved_mults = (
                archetype_multipliers(scheme, fit, "DEFENSIVE", estimates)
                if scheme == "A1_R"
                else {}
            )
        finally:
            alloc.TILT_LAMBDA, alloc.MULT_CLIP = old_lambda, old_clip

        def symbol_mult(mults: dict[int, float], symbol: str) -> float:
            label = labels.get(symbol)
            return mults.get(label, 1.0) if label is not None else 1.0

        active = {s: symbol_mult(active_mults, s) for s in symbols}
        if composite is not None:
            state = state_multipliers(
                composite, self.z_state, mark, symbols, band=band or (0.85, 1.15)
            )
            active = {s: active[s] * state[s] for s in symbols}
        reserved = {s: symbol_mult(reserved_mults, s) for s in symbols} if reserved_mults else {}
        return (
            tilted_weights(symbols, active),
            tilted_weights(symbols, reserved) if reserved else tilted_weights(symbols, {}),
        )

    def evaluate(
        self,
        label: str,
        scheme: str,
        *,
        composite: str | None = None,
        band: tuple[float, float] | None = None,
        lambda_override: float | None = None,
        clip_override: tuple[float, float] | None = None,
        exclude_symbols: frozenset[str] = frozenset(),
    ) -> dict[str, object]:
        by_mark: dict[date, tuple[dict[str, float], dict[str, float]]] = {}
        active_of: dict[date, dict[str, float]] = {}
        reserved_of: dict[date, dict[str, float]] = {}
        for session in self.context.sessions:
            mark = self.mark_of_session.get(session)
            if mark is None:
                mark = self.marks[0]
            if mark not in by_mark:
                by_mark[mark] = self.mark_weights(
                    scheme,
                    mark,
                    composite=composite,
                    band=band,
                    lambda_override=lambda_override,
                    clip_override=clip_override,
                    exclude_symbols=exclude_symbols,
                )
            active_of[session], reserved_of[session] = by_mark[mark]

        frames = {s: f for s, f in self.context.frames.items() if s not in exclude_symbols}
        stance = {s: v for s, v in self.context.stance.items() if s not in exclude_symbols}
        targets = build_targets_tilted(
            frames,
            self.context.sessions,
            self.context.participate,
            stance,
            active_weight_of=active_of,
            reserved_weight_of=reserved_of,
        )
        blocks: dict[str, object] = {}
        for cost_model in COST_MODELS:
            result = replay_weighted(frames, targets, cost_model, label=label)
            blocks[cost_model.label] = weighted_report(result, self.context.states)
        # Weight diagnostics at the last governing mark (dispersion summary).
        sample = sorted(by_mark)[-1]
        active_sample = by_mark[sample][0]
        blocks["weight_diagnostics"] = {
            "sample_mark": sample.isoformat(),
            "min_weight": min(active_sample.values()),
            "max_weight": max(active_sample.values()),
            "total": sum(active_sample.values()),
        }
        return blocks


def run_a1(universe_name: str) -> None:
    context = TiltContext(universe_name)
    payload: dict[str, object] = {"universe": context.context.universe}
    for scheme in A1_SCHEMES:
        started = time.perf_counter()
        payload[scheme] = context.evaluate(scheme, scheme)
        print(f"{scheme}: done in {time.perf_counter() - started:.0f}s", flush=True)
    write_json(OUT_DIR / f"a1_{universe_name}.json", payload)


def run_a2(universe_name: str, scheme: str) -> None:
    context = TiltContext(universe_name)
    payload: dict[str, object] = {
        "universe": context.context.universe,
        "base_scheme": scheme,
    }
    for composite in A2_COMPOSITES:
        started = time.perf_counter()
        payload[composite] = context.evaluate(f"{scheme}+{composite}", scheme, composite=composite)
        print(f"{composite}: done in {time.perf_counter() - started:.0f}s", flush=True)
    write_json(OUT_DIR / f"a2_{universe_name}_{scheme}.json", payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("a1", "a2"))
    parser.add_argument("--universe", default="u30")
    parser.add_argument("--scheme", default="A1_P")
    arguments = parser.parse_args()
    started = time.perf_counter()
    if arguments.stage == "a1":
        run_a1(arguments.universe)
    else:
        run_a2(arguments.universe, arguments.scheme)
    print(f"stage {arguments.stage} complete in {time.perf_counter() - started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
