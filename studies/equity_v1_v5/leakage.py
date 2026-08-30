"""Causality auditing for engines whose warm-up is most of the frame.

The shipped auditor (`autotrader.research.leakage.audit_engine_causality`) is the
right test and this module runs it. But it places its probes evenly across the
whole frame, which is correct for an engine that warms up in a hundred bars and
**silently vacuous** for one that warms up in three thousand: every probe lands
inside the warm-up, no signal exists at or before the cutoff, and the comparison
is `() == ()`. It passes, and it has established nothing.

Equity V3 needs 3000 base bars. So this module does three things the generic
audit cannot do on its own.

**Probes go where the decisions are.** `scored_probe_indices` places every probe
strictly inside the scored region, so each one has real decisions before it to
compare. `vacuous` records how many decisions each probe actually covered, and a
probe covering none is reported as a *failure of the audit* rather than as a pass.

**Decisions are compared, not just signals.** A signal set is the visible part of
an engine's output; the score and the confidence are the rest of it. A
perturbation that shifted V3's composite score from 0.24 to 0.31 without crossing
the buy threshold would leave the signal set identical and would still be
look-ahead. So the comparison here is over the whole `DecisionRecord` - signal,
score to nine places, confidence, regime and reasons.

**The perturbation is applied to the future only, and the past is re-checked in
full.** Every decision at or before the cutoff must be byte-identical. A single
differing score at any earlier bar is a finding.

`DecisionSeriesEngine` is deliberately excluded: a stored series cannot see the
future because it cannot see anything, and auditing it would manufacture a clean
result that means nothing. Only the live adapter is audited, and `audit_ready`
is asserted before anything is claimed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import pandas as pd

from autotrader.research.leakage import (
    PERTURBATION_FACTOR,
    audit_engine_causality,
    perturb_after,
)
from studies.equity_v1_v5.adapters import DecisionRecord, LiveDecisionEngine

#: How many probe points each engine is audited at. Five is the shipped default
#: and is kept, because each probe costs a full re-scoring of the frame.
DEFAULT_PROBES = 5

#: How many bars are scored in an audit frame. Small on purpose: every probe
#: re-runs the engine over all of them, so the cost is
#: ``(probes + 1) x scored_bars`` engine calls.
DEFAULT_SCORED_BARS = 24


class AuditError(Exception):
    """The audit could not be run in a form that would mean anything."""


@dataclass(frozen=True)
class ProbeResult:
    """One perturbation probe: what it covered, and whether anything moved."""

    probe_index: int
    cutoff: str
    decisions_before_cutoff: int
    changed_decisions: int
    first_change: str | None = None

    @property
    def vacuous(self) -> bool:
        """Whether the probe compared nothing, and so proved nothing."""
        return self.decisions_before_cutoff == 0

    def to_json_dict(self) -> dict[str, object]:
        return {
            "probe_index": self.probe_index,
            "cutoff": self.cutoff,
            "decisions_before_cutoff": self.decisions_before_cutoff,
            "changed_decisions": self.changed_decisions,
            "first_change": self.first_change,
            "vacuous": self.vacuous,
        }


@dataclass(frozen=True)
class EngineAudit:
    """The whole causality verdict for one engine on one symbol."""

    engine: str
    symbol: str
    audit_ready: bool
    scored_bars: int
    lookback_bars: int
    perturbation_factor: float
    probes: tuple[ProbeResult, ...] = field(default_factory=tuple)
    shipped_audit_findings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> int:
        return sum(probe.changed_decisions for probe in self.probes)

    @property
    def vacuous_probes(self) -> int:
        return sum(1 for probe in self.probes if probe.vacuous)

    @property
    def ok(self) -> bool:
        """Causally clean *and* actually tested."""
        return (
            self.audit_ready
            and bool(self.probes)
            and self.changed == 0
            and self.vacuous_probes == 0
            and not self.shipped_audit_findings
        )

    def to_json_dict(self) -> dict[str, object]:
        return {
            "engine": self.engine,
            "symbol": self.symbol,
            "audit_ready": self.audit_ready,
            "scored_bars": self.scored_bars,
            "lookback_bars": self.lookback_bars,
            "perturbation_factor": self.perturbation_factor,
            "probe_count": len(self.probes),
            "changed_decisions": self.changed,
            "vacuous_probes": self.vacuous_probes,
            "ok": self.ok,
            "shipped_audit_findings": list(self.shipped_audit_findings),
            "probes": [probe.to_json_dict() for probe in self.probes],
        }


def scored_probe_indices(
    total_bars: int,
    *,
    lookback_bars: int,
    probes: int = DEFAULT_PROBES,
) -> tuple[int, ...]:
    """Evenly spaced probe positions **inside the scored region**.

    The first scorable bar is at ``lookback_bars - 1``. A probe must sit strictly
    after it, so that at least one decision precedes the cutoff, and strictly
    before the last bar, so that something is left to perturb.
    """
    first = lookback_bars  # one past the first scorable bar
    last = total_bars - 2
    if last < first or probes < 1:
        return ()
    usable = min(probes, last - first + 1)
    step = (last - first + 1) / usable
    return tuple(
        sorted({min(last, first + int((position + 0.5) * step)) for position in range(usable)})
    )


def _comparable(record: DecisionRecord) -> tuple[object, ...]:
    """A decision reduced to everything that must not move. Scores included."""
    return (
        str(record.timestamp),
        record.symbol,
        record.signal.value,
        round(record.score, 9),
        round(record.confidence, 9),
        record.regime,
        record.reasons,
    )


def audit_engine(
    adapter: LiveDecisionEngine,
    bars: pd.DataFrame,
    *,
    probes: int = DEFAULT_PROBES,
    factor: float = PERTURBATION_FACTOR,
    engine_name: str | None = None,
    symbol: str = "",
) -> EngineAudit:
    """Perturb the future at several points and require every earlier decision to hold."""
    if not getattr(adapter, "audit_ready", False):
        raise AuditError(
            f"{engine_name or adapter.name} is not audit-ready. A stored decision series "
            "cannot demonstrate causality: it would pass by being blind, not by being causal."
        )

    lookback = adapter.warmup_bars
    timestamps = list(bars["timestamp"])
    baseline = {record.timestamp: record for record in adapter.decisions(bars)}
    indices = scored_probe_indices(len(bars), lookback_bars=lookback, probes=probes)

    results: list[ProbeResult] = []
    for index in indices:
        cutoff = timestamps[index]
        recomputed = {
            record.timestamp: record
            for record in adapter.decisions(perturb_after(bars, index, factor))
        }
        before = {ts: rec for ts, rec in baseline.items() if ts <= cutoff}
        changed = [
            ts
            for ts, rec in sorted(before.items())
            if ts not in recomputed or _comparable(recomputed[ts]) != _comparable(rec)
        ]
        results.append(
            ProbeResult(
                probe_index=index,
                cutoff=str(cutoff),
                decisions_before_cutoff=len(before),
                changed_decisions=len(changed),
                first_change=str(changed[0]) if changed else None,
            )
        )

    shipped = audit_engine_causality(adapter, bars, probes=probes)
    return EngineAudit(
        engine=engine_name or adapter.name,
        symbol=symbol,
        audit_ready=True,
        scored_bars=len(baseline),
        lookback_bars=lookback,
        perturbation_factor=factor,
        probes=tuple(results),
        shipped_audit_findings=tuple(
            f"{finding.code}: {finding.message}" for finding in shipped.findings
        ),
    )


def audit_frame(
    frame: pd.DataFrame,
    *,
    end_position: int,
    lookback_bars: int,
    scored_bars: int = DEFAULT_SCORED_BARS,
) -> pd.DataFrame:
    """The slice to audit on: `scored_bars` scorable bars preceded by a full warm-up."""
    start = end_position - lookback_bars - scored_bars + 1
    if start < 0:
        raise AuditError(
            f"An audit frame needs {lookback_bars + scored_bars} bars ending at position "
            f"{end_position}, and only {end_position + 1} exist."
        )
    return frame.iloc[start : end_position + 1].reset_index(drop=True)


def summarize(audits: Sequence[EngineAudit]) -> dict[str, object]:
    """The report block: one verdict, plus every engine's own."""
    return {
        "perturbation_factor": PERTURBATION_FACTOR,
        "all_causal": all(audit.ok for audit in audits),
        "engines_audited": len(audits),
        "total_changed_decisions": sum(audit.changed for audit in audits),
        "total_vacuous_probes": sum(audit.vacuous_probes for audit in audits),
        "audits": [audit.to_json_dict() for audit in audits],
    }


__all__ = [
    "DEFAULT_PROBES",
    "DEFAULT_SCORED_BARS",
    "AuditError",
    "EngineAudit",
    "ProbeResult",
    "audit_engine",
    "audit_frame",
    "scored_probe_indices",
    "summarize",
]
