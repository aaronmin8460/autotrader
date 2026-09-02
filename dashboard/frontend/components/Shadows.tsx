"use client";

/**
 * The Shadows workspace: every observation-only strategy as a card, one
 * comparison table, and the A1-B universe with a chart on selection.
 *
 * Everything on this page is in the observation colour and says OBSERVING,
 * because nothing on it can trade. The cards state the zero-order proof as a
 * measured fact - intents in the database, linked orders, rows outside the
 * simulated designation - and the comparison table refuses to print a
 * performance figure as a conclusion until each observer's own sample
 * threshold is met.
 *
 * Adding a sixth observer is one more card, never a tab.
 */

import { useState, type KeyboardEvent } from "react";

import type { A1BOverview, A1BSymbolRow } from "@/lib/a1b";
import { CHART_RANGES, useChartBatch, type ChartRange } from "@/lib/charts";
import { percent, relative, signedPercent, stampUtc } from "@/lib/format";
import type { ServiceUnitRow } from "@/lib/services";
import { displayStatus } from "@/lib/services";
import { STATUS_REASON_LABELS, shadowStatusLabel, shadowTone, type ShadowOverview, type ShadowStatus } from "@/lib/shadow";
import type { ShadowComparison } from "@/lib/shadows";

import { LineChart } from "./charts/LineChart";
import { Card, Empty, Field, Pill, RangeSelector, Status, Tag, Td, Th, cn } from "./ui";

export function ShadowsBanner() {
  return (
    <section aria-label="Shadows scope" className="card tint-observe px-5 py-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="text-[13px] leading-none font-semibold tracking-tight text-ink">Shadows — observation only</span>
        <Tag tone="SHADOW" title="No process on this page can submit, cancel or replace an order.">
          Zero order mutation
        </Tag>
        <Tag tone="SHADOW" title="Every figure here is compounded from recorded decisions, from a normalized 100, with no costs.">
          Simulated · no broker order
        </Tag>
      </div>
      <p className="mt-2 max-w-[100ch] text-[12px] leading-snug text-ink-2">
        These are decisions <strong className="font-semibold text-ink">recorded, not taken</strong>. The
        observers behind this page hold no execution path; no order has been placed, no position exists,
        and no figure here is broker account equity. The account&apos;s real orders are on Operations.
      </p>
    </section>
  );
}

export interface ShadowCardModel {
  key: string;
  name: string;
  mode: string;
  status: ShadowStatus;
  statusReason: string;
  unit: ServiceUnitRow | null;
  universeSize: number;
  universeNote: string;
  lastCycleAt: string | null;
  cyclesRecorded: number;
  regime: string | null;
  simulatedExposure: number | null;
  turnover: string;
  observationCount: number;
  parity: string;
  parityTone: "POSITIVE" | "ATTENTION" | "MUTED";
  zeroOrder: { intents: number; linked: number; extra: number | null; holds: boolean };
  codeSha: string | null;
  policy: string | null;
}

export function ShadowCard({ model, generatedAt }: { model: ShadowCardModel; generatedAt: string | null }) {
  const unit = model.unit ? displayStatus(model.unit) : null;
  return (
    <Card
      tone="SHADOW"
      title={model.name}
      meta={
        <>
          {unit ? <Status tone={unit.tone} title={model.unit?.detail}>{unit.status}</Status> : <Status tone="MUTED">Unit unknown</Status>}
          <Pill tone={shadowTone(model.status)} emphasis title={STATUS_REASON_LABELS[model.statusReason] ?? model.statusReason}>
            {shadowStatusLabel(model.status)}
          </Pill>
        </>
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <Tag tone="SHADOW">Observation only</Tag>
        <Tag tone="SHADOW">Zero orders</Tag>
        <Tag title={model.mode}>{model.mode}</Tag>
      </div>
      <div className="mt-4 grid grid-cols-2 gap-x-5 gap-y-3.5 sm:grid-cols-4">
        <Field label="Universe" title={model.universeNote}>
          <span className="num">{model.universeSize} symbols</span>
        </Field>
        <Field label="Last completed cycle">
          <span className="num">
            {model.lastCycleAt ? stampUtc(model.lastCycleAt, generatedAt) : "—"}
            {model.lastCycleAt ? <span className="ml-1.5 text-[11px] text-ink-3">{relative(model.lastCycleAt, generatedAt)}</span> : null}
          </span>
        </Field>
        <Field label="Latest regime">{model.regime ?? "—"}</Field>
        <Field label="Simulated exposure" title="Fraction of the hypothetical book held long at the latest bar. Not account exposure.">
          <span className="num">{percent(model.simulatedExposure, 1)}</span>
        </Field>
        <Field label="Simulated turnover">
          <span className="num">{model.turnover}</span>
        </Field>
        <Field label="Observations">
          <span className="num">
            {model.observationCount} · {model.cyclesRecorded} bars
          </span>
        </Field>
        <Field
          label="Parity / mismatch"
          title={
            model.key === "a1b"
              ? "A1-B has no execution counterpart to disagree with, so no parity figure exists."
              : "Cumulative bars on which the paper runtime's independently computed EDA-1 answer disagreed with this observer's; such a symbol is excluded from mutation for that bar."
          }
        >
          <Status tone={model.parityTone}>{model.parity}</Status>
        </Field>
        <Field label="Code · policy">
          <span className="num text-[11.5px]">
            {model.codeSha ? model.codeSha.slice(0, 10) : "—"}
            {model.policy ? ` · ${model.policy.slice(0, 12)}` : ""}
          </span>
        </Field>
      </div>
      <div className={cn("mt-4 rounded-[6px] px-3 py-2.5 ring-1", model.zeroOrder.holds ? "ring-observe/30" : "ring-neg/50 tint-neg")}>
        <Status tone={model.zeroOrder.holds ? "SHADOW" : "NEGATIVE"}>
          {model.zeroOrder.holds ? "Zero-order proof holds" : "Zero-order invariant violated"}
        </Status>
        <span className="num ml-3 text-[11px] text-ink-3">
          intents {model.zeroOrder.intents} · linked {model.zeroOrder.linked}
          {model.zeroOrder.extra !== null ? ` · non-simulated rows ${model.zeroOrder.extra}` : ""}
        </span>
      </div>
    </Card>
  );
}

export function ShadowComparisonTable({ comparison }: { comparison: ShadowComparison }) {
  return (
    <Card
      tone="SHADOW"
      title="EDA-1 Shadow vs A1-B U30"
      meta={
        comparison.insufficient ? (
          <Pill tone="ATTENTION" emphasis title={comparison.warning}>
            Insufficient sample
          </Pill>
        ) : (
          <Tag tone="SHADOW">Sample thresholds met</Tag>
        )
      }
      bodyClassName=""
    >
      <div className="scroll-x">
        <table className="w-full min-w-[720px] border-collapse">
          <thead>
            <tr className="border-b border-line">
              <Th>Metric</Th>
              <Th>EDA-1 Shadow</Th>
              <Th>A1-B U30 Shadow</Th>
            </tr>
          </thead>
          <tbody>
            {comparison.rows.map((row) => (
              <tr key={row.key} className="border-b border-line/70 last:border-0">
                <Td className="hint text-ink-2" title={row.hint}>
                  {row.label}
                </Td>
                {[row.eda1, row.a1b].map((cell, index) => (
                  <Td key={index} className={cn("num", cell.conclusive ? "text-ink" : "text-warn")}>
                    {cell.text}
                    {cell.raw ? <span className="ml-2 text-[10.5px] text-ink-3">raw {cell.raw} · not a conclusion</span> : null}
                  </Td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="px-4 pt-3 pb-3 text-[11.5px] leading-snug text-ink-3">{comparison.warning}</p>
    </Card>
  );
}

function A1BSymbolChart({ symbol, onClose }: { symbol: string; onClose: () => void }) {
  const [range, setRange] = useState<ChartRange>("1D");
  const { series } = useChartBatch([symbol], range);
  return (
    <div className="border-t border-line px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-[12.5px] font-semibold text-ink">
          {symbol} <span className="text-ink-3">price · observation universe</span>
        </span>
        <div className="flex items-center gap-2">
          <RangeSelector options={CHART_RANGES} value={range} onChange={setRange} label={`${symbol} chart range`} />
          <button
            type="button"
            onClick={onClose}
            className="rounded-[4px] px-2 py-1 text-[10px] font-medium tracking-[0.06em] text-ink-3 uppercase ring-1 ring-line hover:text-ink focus-visible:outline-2 focus-visible:outline-accent"
          >
            Close
          </button>
        </div>
      </div>
      <div className="mt-2 max-w-[880px]">
        <LineChart series={series[symbol]} />
      </div>
      <p className="mt-1 text-[11px] text-ink-3">Price only. No simulated action is drawn on this chart.</p>
    </div>
  );
}

export function A1BUniverse({ overview, generatedAt }: { overview: A1BOverview | null; generatedAt: string | null }) {
  const [selected, setSelected] = useState<string | null>(null);
  if (!overview) {
    return (
      <Card tone="SHADOW" title="A1-B U30 universe">
        <Empty headline="The A1-B shadow API is not answering." />
      </Card>
    );
  }
  const rows: A1BSymbolRow[] = overview.symbols;
  const open = (symbol: string) => (event: KeyboardEvent<HTMLTableRowElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setSelected(symbol);
    }
  };
  return (
    <Card
      tone="SHADOW"
      title="A1-B U30 universe"
      meta={
        <>
          <Tag tone="SHADOW">{overview.service.universe_size} usable symbols</Tag>
          <Tag title="Ten incumbents are scored under their own name; the rest are alias-scored as the reference symbol, the mechanism two research programs proved invariant.">
            {overview.service.incumbents.length} incumbent · {overview.service.alias_scored.length} alias-scored
          </Tag>
          <Tag title="Rebalance mark governing the current weights.">mark {overview.service.mark_date ?? "—"}</Tag>
        </>
      }
      bodyClassName=""
    >
      <div className="scroll-x">
        <table className="w-full min-w-[820px] border-collapse">
          <thead>
            <tr className="border-b border-line">
              <Th>Symbol</Th>
              <Th title="The stance accumulator: 1 after a BUY, 0 after a SELL, starting flat at deployment.">Stance</Th>
              <Th>V3 signal</Th>
              <Th align="right" title="The hypothetical target weight recorded on the latest bar.">Target wt</Th>
              <Th align="right">Active wt</Th>
              <Th align="right">Reserved wt</Th>
              <Th>Archetype</Th>
              <Th>Scored as</Th>
              <Th align="right">Latest bar</Th>
              <Th align="right">Ref close</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.symbol}
                role="button"
                tabIndex={0}
                aria-label={`Show ${row.symbol} chart`}
                aria-pressed={selected === row.symbol}
                onClick={() => setSelected(selected === row.symbol ? null : row.symbol)}
                onKeyDown={open(row.symbol)}
                className={cn("row-link border-b border-line/70 last:border-0 hover:bg-surface-2", selected === row.symbol && "bg-surface-2")}
              >
                <Td className="font-medium text-ink">{row.symbol}</Td>
                <Td>
                  {row.stance === null ? <span className="text-ink-3">—</span> : <Pill tone={row.stance === 1 ? "SHADOW" : "MUTED"}>{row.stance === 1 ? "LONG" : "FLAT"}</Pill>}
                </Td>
                <Td className="text-ink-2">{row.v3_signal ?? "—"}</Td>
                <Td numeric className="text-ink">
                  {percent(row.target_weight, 2)}
                </Td>
                <Td numeric className="text-ink-2">
                  {percent(row.active_weight, 2)}
                </Td>
                <Td numeric className="text-ink-2">
                  {percent(row.reserved_weight, 2)}
                </Td>
                <Td className="text-ink-2">{row.archetype_label === null ? "—" : `A${row.archetype_label}`}</Td>
                <Td className="text-ink-3">{row.alias_scored === null ? "—" : row.alias_scored ? "reference alias" : "own name"}</Td>
                <Td numeric className="text-ink-3">
                  {row.bar_timestamp ? stampUtc(row.bar_timestamp, generatedAt) : "—"}
                </Td>
                <Td numeric className="text-ink-2">
                  {row.reference_close?.toFixed(2) ?? "—"}
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {selected ? <A1BSymbolChart symbol={selected} onClose={() => setSelected(null)} /> : (
        <p className="px-4 pt-2 pb-3 text-[11px] text-ink-3">Select a symbol for its price chart. Charts load one symbol at a time.</p>
      )}
    </Card>
  );
}

export function A1BDetail({ overview, generatedAt }: { overview: A1BOverview | null; generatedAt: string | null }) {
  if (!overview) return null;
  const { service, regime, hypothetical, summary } = overview;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card tone="SHADOW" title="A1-B observer">
        <div className="grid grid-cols-2 gap-x-5 gap-y-3.5 sm:grid-cols-3">
          <Field label="Policy hash">
            <span className="num">{service.policy_hash ? service.policy_hash.slice(0, 12) : "—"}</span>
          </Field>
          <Field label="Mark grid" title="Weights change only at rebalance marks on the research grid, anchored at the research grid's final mark.">
            {service.mark_every_sessions ?? "—"} sessions
          </Field>
          <Field label="Grid anchor" title="The session the mark grid is counted from.">
            {service.grid_anchor ?? "—"}
          </Field>
          <Field label="Governing mark">
            {service.mark_date ?? "—"}
            <span className="text-ink-3"> · fit {service.fit_mark ?? "—"}</span>
          </Field>
          <Field label="Labelled symbols">
            <span className="num">{service.labeled_symbols ?? "—"}</span>
          </Field>
          <Field label="Observer started">{service.started_at ? stampUtc(service.started_at, generatedAt) : "—"}</Field>
          <Field label="Session (broker calendar)">{service.session_confirmed_open ? "Open — confirmed" : "No session confirmed today"}</Field>
          <Field label="Regime">
            {regime.state ?? "—"} <span className="text-ink-3">{regime.session_date ?? ""}</span>
          </Field>
          <Field label="Signals (window)">
            <span className="num">
              BUY {summary.buy_signals} · SELL {summary.sell_signals} · HOLD {summary.hold_signals}
            </span>
          </Field>
          <Field label="Regime transitions">
            <span className="num">{summary.regime_transitions}</span>
          </Field>
        </div>
        <p className="mt-3 text-[11px] leading-snug text-ink-3">{service.invariant_note}</p>
      </Card>
      <Card tone="SHADOW" title="A1-B hypothetical book" meta={<Pill tone="SHADOW" emphasis>{hypothetical.label}</Pill>}>
        <div className="grid grid-cols-2 gap-x-5 gap-y-3.5 sm:grid-cols-3">
          <Field label="Index value" title="Compounded from a normalized 100, no costs.">
            <span className="num">{hypothetical.portfolio_value?.toFixed(2) ?? "—"}</span>
          </Field>
          <Field label="Return (raw)">
            <span className="num">{signedPercent(hypothetical.cumulative_return)}</span>
          </Field>
          <Field label="Max drawdown (raw)">
            <span className="num">{signedPercent(hypothetical.max_drawdown)}</span>
          </Field>
          <Field label="Average exposure">
            <span className="num">{percent(hypothetical.average_exposure, 1)}</span>
          </Field>
          <Field label="Current exposure">
            <span className="num">
              {percent(hypothetical.current_exposure, 1)} · long {hypothetical.long_symbols}/{service.universe_size}
            </span>
          </Field>
          <Field label="Steps">
            <span className="num">{hypothetical.steps}</span>
          </Field>
        </div>
        <p className="mt-3 text-[11.5px] leading-snug text-warn">{hypothetical.sample_warning}</p>
      </Card>
    </div>
  );
}

/** Build the two cards' models from the payloads. Kept here so the page stays a composition. */
export function shadowCards(
  eda1: ShadowOverview | null,
  a1b: A1BOverview | null,
  units: { eda1: ServiceUnitRow | null; a1b: ServiceUnitRow | null },
  parityMismatches: number | null,
): ShadowCardModel[] {
  const eda1Book = eda1?.hypothetical.eda1 ?? null;
  return [
    {
      key: "eda1",
      name: "Equity Shadow · V3 + EDA-1",
      mode: eda1?.service.mode ?? "V3 + EDA-1 SIDE-BY-SIDE SHADOW",
      status: eda1?.service.status ?? "UNAVAILABLE",
      statusReason: eda1?.service.status_reason ?? "DATABASE_UNREADABLE",
      unit: units.eda1,
      universeSize: eda1?.service.universe.length ?? 0,
      universeNote: "The ten-symbol equity universe, both engines on every bar.",
      lastCycleAt: eda1?.service.last_cycle_at ?? null,
      cyclesRecorded: eda1?.service.cycles_recorded ?? 0,
      regime: eda1?.regime.state ?? null,
      simulatedExposure:
        eda1 && eda1Book ? eda1Book.current_long_symbols.length / Math.max(1, eda1.service.universe.length) : null,
      turnover: eda1Book?.turnover_per_step === null || eda1Book?.turnover_per_step === undefined ? "—" : `${eda1Book.turnover_per_step.toFixed(3)} Δ/step`,
      observationCount: eda1?.comparison.bars_compared ?? 0,
      parity:
        parityMismatches === null
          ? "N/A"
          : parityMismatches === 0
            ? "Parity clean"
            : `${parityMismatches} mismatch${parityMismatches === 1 ? "" : "es"}`,
      parityTone: parityMismatches === null ? "MUTED" : parityMismatches === 0 ? "POSITIVE" : "ATTENTION",
      zeroOrder: {
        intents: eda1?.service.order_intents_in_database ?? 0,
        linked: eda1?.service.linked_orders_in_database ?? 0,
        extra: null,
        holds: eda1?.service.zero_order_invariant_holds ?? false,
      },
      codeSha: eda1?.service.code_sha ?? null,
      policy: null,
    },
    {
      key: "a1b",
      name: "A1-B U30 Shadow",
      mode: a1b?.service.mode ?? "A1-B U30 ARCHETYPE ALLOCATION SHADOW",
      status: a1b?.service.status ?? "UNAVAILABLE",
      statusReason: a1b?.service.status_reason ?? "DATABASE_UNREADABLE",
      unit: units.a1b,
      universeSize: a1b?.service.universe_size ?? 0,
      universeNote: "The frozen U30 observation manifest: the usable symbols the observer watches every bar.",
      lastCycleAt: a1b?.service.last_cycle_at ?? null,
      cyclesRecorded: a1b?.service.cycles_recorded ?? 0,
      regime: a1b?.regime.state ?? null,
      simulatedExposure: a1b?.hypothetical.current_exposure ?? null,
      turnover:
        a1b?.hypothetical.turnover_per_step === null || a1b?.hypothetical.turnover_per_step === undefined
          ? "—"
          : `${percent(a1b.hypothetical.turnover_per_step, 2)} wt/step`,
      observationCount: a1b?.service.observations_recorded ?? 0,
      parity: "N/A",
      parityTone: "MUTED",
      zeroOrder: {
        intents: a1b?.service.order_intents_in_database ?? 0,
        linked: a1b?.service.linked_orders_in_database ?? 0,
        extra: a1b?.service.non_simulated_rows ?? null,
        holds: a1b?.service.zero_order_invariant_holds ?? false,
      },
      codeSha: a1b?.service.code_sha ?? null,
      policy: a1b?.service.policy_hash ?? null,
    },
  ];
}
