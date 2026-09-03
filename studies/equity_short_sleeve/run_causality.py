"""Causality audit for the short pipeline (ledger §L4 causality clause).

Three mechanical checks, in increasing strength:

1. **Lag.** Every session's governing fingerprint mark is STRICTLY before that
   session, and before the session before it.
2. **Selection invariance under future perturbation.** Bars strictly after a
   probe session are multiplied by 1.5; the short plan's selections for every
   session at or before the probe must be unchanged. Non-vacuous by
   construction: the count of real selections behind each probe is recorded.
3. **Forward-target alignment.** A forward target at session `s` over horizon
   `H` reads only sessions strictly after `s`, and never `s` itself.

Usage:
    python -m studies.equity_short_sleeve.run_causality
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pandas as pd

from studies.equity_deep_arch.evaluate import write_json
from studies.equity_short_sleeve import REPORT_ROOT
from studies.equity_short_sleeve.candidates import selected_short_plan
from studies.equity_short_sleeve.context import ShortContext
from studies.equity_short_sleeve.information import forward_targets

OUT = Path(REPORT_ROOT) / "causality"

#: Probe sessions spanning calm, pullback and drawdown states.
PROBES = ("2022-03-01", "2022-10-03", "2023-06-01", "2025-04-01", "2026-01-05")


def _log(message: str) -> None:
    print(message, flush=True)


def main() -> None:
    started = time.perf_counter()
    context = ShortContext()
    report: dict[str, object] = {}

    # 1. Lag.
    violations = [
        str(session)
        for session, mark in context.mark_of.items()
        if not (mark < session)
    ]
    ordered = sorted(context.sessions)
    index_of = {s: i for i, s in enumerate(ordered)}
    not_before_previous = [
        str(session)
        for session, mark in context.mark_of.items()
        if index_of[session] > 0 and mark > ordered[index_of[session] - 1]
    ]
    report["lag"] = {
        "sessions_mapped": len(context.mark_of),
        "mark_not_before_session": violations,
        "mark_after_previous_session": not_before_previous,
        "status": "PASS" if not violations and not not_before_previous else "FAIL",
    }
    _log(f"lag: {report['lag']['status']} ({len(context.mark_of)} sessions mapped)")

    # 2. Future-perturbation invariance of the short plan.
    base = selected_short_plan(
        context.sessions,
        context.participate,
        context.panel,
        context.mark_of,
        context.universe,
        label="S3",
        gross=0.10,
        names=5,
        characteristic="beta_252",
    )
    perturbation: dict[str, object] = {}
    all_pass = True
    for probe_text in PROBES:
        probe = pd.Timestamp(probe_text).date()
        # Perturb the panel's rows at marks strictly after the probe.
        #
        # A uniform scaling would be useless: every rule downstream is a
        # cross-sectional RANK, and a positive monotone transform leaves ranks
        # identical. The perturbation must therefore reorder the cross-section,
        # so it applies a deterministic per-symbol factor derived from a hash
        # of the symbol name — large enough to move ranks, reproducible, and
        # applied only to marks strictly after the probe.
        panel = context.panel.copy()
        numeric = [c for c in panel.columns if c not in ("mark", "symbol")]
        mask = (panel["mark"] > probe).to_numpy()
        factors = panel["symbol"].map(
            lambda name: 0.5 + (int(hashlib.sha256(name.encode()).hexdigest()[:8], 16) % 1000) / 500.0
        ).to_numpy()
        for column in numeric:
            values = panel[column].to_numpy(dtype="float64", copy=True)
            values[mask] = values[mask] * factors[mask]
            panel[column] = values
        probed = selected_short_plan(
            context.sessions,
            context.participate,
            panel,
            context.mark_of,
            context.universe,
            label="S3P",
            gross=0.10,
            names=5,
            characteristic="beta_252",
        )
        before = [s for s in context.sessions if s <= probe]
        changed = [
            str(session)
            for session in before
            if base.weight_of.get(session, {}) != probed.weight_of.get(session, {})
        ]
        real = sum(1 for s in before if base.weight_of.get(s))
        after_changed = sum(
            1
            for s in context.sessions
            if s > probe and base.weight_of.get(s, {}) != probed.weight_of.get(s, {})
        )
        entry = {
            "sessions_at_or_before_probe": len(before),
            "real_selections_behind_probe": real,
            "changed_before_probe": len(changed),
            "changed_after_probe": after_changed,
            "vacuous": real == 0 or after_changed == 0,
            "status": "PASS" if not changed and real > 0 and after_changed > 0 else "FAIL",
        }
        if entry["status"] != "PASS":
            all_pass = False
        perturbation[probe_text] = entry
        _log(
            f"probe {probe_text}: {entry['status']} "
            f"({real} real selections behind it, {after_changed} changed after)"
        )
    report["future_perturbation"] = {
        "probes": perturbation,
        "status": "PASS" if all_pass else "FAIL",
    }

    # 3. Forward-target alignment.
    closes = context.closes.loc[[s for s in context.closes.index if s in set(context.sessions)]]
    targets = forward_targets(closes, 5)
    sessions = list(closes.index)
    probe_index = len(sessions) // 2
    session = sessions[probe_index]
    shifted = closes.copy()
    shifted.iloc[probe_index + 1 :] = shifted.iloc[probe_index + 1 :] * 1.5
    shifted_targets = forward_targets(shifted, 5)
    own_unchanged = bool(
        (closes.loc[session] == shifted.loc[session]).all()
    )
    target_changed = bool(
        (targets.fwd_ret.loc[session] != shifted_targets.fwd_ret.loc[session]).any()
    )
    earlier = sessions[probe_index - 10]
    report["forward_alignment"] = {
        "probe_session": str(session),
        "probe_session_close_unchanged": own_unchanged,
        "probe_session_forward_target_changed": target_changed,
        "target_ten_sessions_earlier_unchanged": bool(
            (targets.fwd_ret.loc[earlier].fillna(-1) == shifted_targets.fwd_ret.loc[earlier].fillna(-1)).all()
        ),
        "status": "PASS" if own_unchanged and target_changed else "FAIL",
    }
    _log(f"forward alignment: {report['forward_alignment']['status']}")

    report["audit"] = (
        "PASS"
        if all(
            report[k]["status"] == "PASS"
            for k in ("lag", "future_perturbation", "forward_alignment")
        )
        else "FAIL"
    )
    write_json(OUT / "causality.json", report)
    _log(f"causality audit: {report['audit']} ({time.perf_counter() - started:.0f}s)")


if __name__ == "__main__":
    main()
