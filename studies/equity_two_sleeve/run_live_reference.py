"""Live-fetch reference parity (ledger §L15, the live half's Mac side).

Exercises the SHIPPED runtime path end-to-end on real provider data, with no
runtime and no database: broker calendar → session axis / governing mark →
45-symbol mark-history fetch → fingerprints → z → labels → A1-B weights —
then compares the result against the research pipeline's stored answer for
the same mark (the current governing mark lies inside the research region,
so the frozen pipeline knows the right answer).

The output JSON is the reference the VPS's first computed `a1b_mark_state`
row must match.

Usage:
    python -m studies.equity_two_sleeve.run_live_reference
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from studies.equity_deep_arch.evaluate import write_json
from studies.equity_two_sleeve import REPORT_ROOT

OUT = Path(REPORT_ROOT) / "parity"
TOLERANCE = 1e-9


def _log(message: str) -> None:
    print(message, flush=True)


def main() -> None:
    started = time.perf_counter()

    from autotrader.equity.a1b_policy import (
        build_series,
        cross_sectional_z_at_mark,
        governing_fit,
        governing_mark,
        load_policy,
        mark_weights,
        structural_at,
        symbol_sessions,
    )
    from autotrader.equity.a1b_shadow import A1BMarkBars
    from autotrader.execution.equity import AlpacaMarketCalendar

    policy = load_policy()
    calendar = AlpacaMarketCalendar()
    now = datetime.now(UTC)

    # ------------------------------------------------------------------
    # Session-axis audit: the calendar must reproduce the research axis.
    # ------------------------------------------------------------------
    from datetime import date as date_type

    research_end = date_type(2026, 8, 28)
    axis = calendar.sessions_between(policy.mark_anchor, research_end)
    axis_dates = [s.session_date for s in axis]
    report: dict[str, object] = {
        "computed_at": now.isoformat(),
        "policy_hash": policy.policy_hash,
        "calendar_axis_sessions_to_research_end": len(axis),
        "research_axis_sessions": 1233,
        "axis_match": len(axis) == 1233,
    }
    _log(f"calendar axis to {research_end}: {len(axis)} sessions (research: 1233)")

    # ------------------------------------------------------------------
    # The next session's governing mark.
    # ------------------------------------------------------------------
    probe = now.date()
    next_session = None
    for offset in range(0, 10):
        candidate = calendar.session_for(probe + timedelta(days=offset))
        if candidate is not None and candidate.session_date >= probe:
            next_session = candidate
            break
    if next_session is None:
        raise SystemExit("No upcoming session could be resolved from the calendar.")
    full_axis = calendar.sessions_between(policy.mark_anchor, next_session.session_date)
    index = len(full_axis) - 1
    mark_index = governing_mark(policy, index)
    mark_day = full_axis[mark_index].session_date
    report["next_session"] = next_session.session_date.isoformat()
    report["next_session_index"] = index
    report["governing_mark_index"] = mark_index
    report["governing_mark_date"] = mark_day.isoformat()
    _log(
        f"next session {next_session.session_date} (index {index}) → "
        f"governing mark index {mark_index} = {mark_day}"
    )
    if mark_index < len(axis_dates) and axis_dates[mark_index] != mark_day:
        raise SystemExit("Mark index does not resolve consistently on the calendar axis.")

    # ------------------------------------------------------------------
    # The shipped mark computation on freshly fetched bars.
    # ------------------------------------------------------------------
    fit = governing_fit(policy, mark_day)
    mark_data = A1BMarkBars(calendar)
    frames = mark_data.history(policy.u45_z_cross_section, before=mark_day, now=now)
    got = sum(1 for f in frames.values() if not f.empty)
    _log(f"mark-history fetch: {got}/{len(policy.u45_z_cross_section)} symbols with bars")
    reference_table = symbol_sessions(frames["SPY"])
    values = {}
    for symbol in policy.u45_z_cross_section:
        frame = frames.get(symbol)
        if frame is None or frame.empty:
            continue
        values[symbol] = structural_at(
            build_series(symbol_sessions(frame), reference_table), mark_day
        )
    z = cross_sectional_z_at_mark(
        values,
        policy.surviving_features,
        winsor=policy.z_winsor,
        min_symbols=policy.z_min_symbols,
    )
    active, reserved, labels = mark_weights(policy, fit, z)
    report["fit_mark"] = fit.fit_mark.isoformat() if fit else None
    report["labels"] = {s: labels[s] for s in sorted(labels)}
    report["active_weights"] = {s: active[s] for s in sorted(active)}
    report["reserved_weights"] = {s: reserved[s] for s in sorted(reserved)}
    report["labeled_symbols"] = len(labels)

    # ------------------------------------------------------------------
    # Compare against the research pipeline's stored answer for this mark
    # (only possible while the governing mark lies inside the research
    # region — which it does at deployment time).
    # ------------------------------------------------------------------
    try:
        from studies.equity_asset_character.fingerprints import cross_sectional_z
        from studies.equity_asset_character.run_phase2 import load_panel
        from studies.equity_asset_character.run_phase5 import TiltContext, surviving_features

        panel = load_panel()
        z_panel = cross_sectional_z(panel, tuple(surviving_features()))
        context = TiltContext("u30", z_structural=z_panel)
        if mark_day in context.marks:
            research_active, research_reserved = context.mark_weights("A1_B", mark_day)
            max_delta = max(
                abs(active.get(s, 0.0) - research_active.get(s, 0.0))
                for s in set(active) | set(research_active)
            )
            mismatches = sum(
                1
                for s in set(active) | set(research_active)
                if abs(active.get(s, 0.0) - research_active.get(s, 0.0)) > TOLERANCE
            )
            report["research_comparison"] = {
                "mark_in_research_grid": True,
                "max_weight_delta": max_delta,
                "weight_mismatches": mismatches,
                "verdict": "PASS" if mismatches == 0 else "FAIL",
            }
            _log(
                f"vs research pipeline at {mark_day}: max Δ {max_delta:.2e}, "
                f"{mismatches} mismatches"
            )
        else:
            report["research_comparison"] = {"mark_in_research_grid": False}
            _log(f"mark {mark_day} is beyond the research grid; no stored answer to compare")
    except Exception as error:  # noqa: BLE001 - the live fetch result stands alone
        report["research_comparison"] = {"error": f"{type(error).__name__}: {error}"}

    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "live_reference.json", report)
    _log(f"live reference complete in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
