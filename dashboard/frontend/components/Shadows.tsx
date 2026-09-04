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
import { percent, signedPercent } from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { useFormat } from "@/lib/i18n/useFormat";
import type { ServiceUnitRow, ServiceUnitsPanel } from "@/lib/services";
import { A1B_SHADOW_KEY, displayStatus, serviceUnit } from "@/lib/services";
import { STATUS_REASON_LABELS, shadowStatusLabel, shadowTone, type ShadowOverview, type ShadowStatus } from "@/lib/shadow";
import type { ShadowComparison } from "@/lib/shadows";

import { LineChart } from "./charts/LineChart";
import { Card, Empty, Field, Pill, SegmentedTimeRange, Status, Tag, Td, Th, cn } from "./ui";

export function ShadowsBanner() {
  const { t } = useI18n();
  return (
    <section aria-label={t("shadows.observationOnly")} className="panel tint-observe px-5 py-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="text-heading leading-none font-semibold tracking-tight text-ink">
          {t("shadows.observationOnly")}
        </span>
        <Tag tone="SHADOW" title={t("shadows.zeroOrderMutationHint")}>
          {t("shadows.zeroOrderMutation")}
        </Tag>
        <Tag tone="SHADOW" title={t("shadows.simulatedHint")}>
          {t("shadows.simulated")}
        </Tag>
      </div>
      <p className="mt-2 max-w-[104ch] text-table leading-snug text-ink-2">{t("shadows.banner")}</p>
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
  /** Cumulative mismatches, or null when the concept does not apply. */
  parityCount: number | null;
  parityTone: "POSITIVE" | "ATTENTION" | "MUTED";
  zeroOrder: { intents: number; linked: number; extra: number | null; holds: boolean };
  codeSha: string | null;
  policy: string | null;
}

export function ShadowCard({ model, generatedAt }: { model: ShadowCardModel; generatedAt: string | null }) {
  const { t } = useI18n();
  const format = useFormat();
  const unit = model.unit ? displayStatus(model.unit) : null;
  return (
    <Card
      tone="SHADOW"
      title={model.name}
      meta={
        <>
          {unit ? <Status tone={unit.tone} title={model.unit?.detail}>{unit.status}</Status> : <Status tone="MUTED">{t("shadows.unitUnknown")}</Status>}
          <Pill tone={shadowTone(model.status)} emphasis title={STATUS_REASON_LABELS[model.statusReason] ?? model.statusReason}>
            {shadowStatusLabel(model.status)}
          </Pill>
        </>
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <Tag tone="SHADOW">{t("strategies.observationOnly")}</Tag>
        <Tag tone="SHADOW">{t("strategies.capability.zeroOrders")}</Tag>
        <Tag title={model.mode}>{model.mode}</Tag>
      </div>
      <div className="mt-4 grid grid-cols-2 gap-x-5 gap-y-3.5 sm:grid-cols-4">
        <Field label={t("strategies.universe")} title={model.universeNote}>
          <span className="num">{t("strategies.symbols", { count: model.universeSize })}</span>
        </Field>
        <Field label={t("strategies.lastCycle")}>
          <span className="num">
            {model.lastCycleAt ? format.stamp(model.lastCycleAt, generatedAt) : "—"}
            {model.lastCycleAt ? (
              <span className="ms-1.5 text-meta text-ink-3">{format.relative(model.lastCycleAt, generatedAt)}</span>
            ) : null}
          </span>
        </Field>
        <Field label={t("shadows.latestRegime")}>{model.regime ?? "—"}</Field>
        <Field label={t("shadows.simulatedExposure")} title={t("shadows.simulatedExposureHint")}>
          <span className="num">{percent(model.simulatedExposure, 1)}</span>
        </Field>
        <Field label={t("shadows.simulatedTurnover")}>
          <span className="num">{model.turnover}</span>
        </Field>
        <Field label={t("shadows.observations")}>
          <span className="num">
            {model.observationCount} · {model.cyclesRecorded} {t("shadows.bars")}
          </span>
        </Field>
        <Field
          label={t("shadows.parity")}
          title={
            model.key === "a1b"
              ? "A1-B has no execution counterpart to disagree with, so no parity figure exists."
              : "Cumulative bars on which the paper runtime's independently computed EDA-1 answer disagreed with this observer's; such a symbol is excluded from mutation for that bar."
          }
        >
          <Status tone={model.parityTone}>
            {model.parityCount === null
              ? t("shadows.parityNA")
              : model.parityCount === 0
                ? t("shadows.parityClean")
                : t(model.parityCount === 1 ? "shadows.parityMismatch" : "shadows.parityMismatches", {
                    count: model.parityCount,
                  })}
          </Status>
        </Field>
        <Field label={t("shadows.codePolicy")}>
          <span className="num text-meta">
            {model.codeSha ? model.codeSha.slice(0, 10) : "—"}
            {model.policy ? ` · ${model.policy.slice(0, 12)}` : ""}
          </span>
        </Field>
      </div>
      <div className={cn("mt-4 rounded-sm px-3 py-2.5 ring-1", model.zeroOrder.holds ? "ring-observe/30" : "ring-neg/50 tint-neg")}>
        <Status tone={model.zeroOrder.holds ? "SHADOW" : "NEGATIVE"}>
          {model.zeroOrder.holds ? t("shadows.zeroOrderProof") : t("shadows.zeroOrderViolated")}
        </Status>
        <span className="num ml-3 text-meta text-ink-3">
          {t("shadows.intents")} {model.zeroOrder.intents} · {t("shadows.linked")}{" "}
          {model.zeroOrder.linked}
          {model.zeroOrder.extra !== null
            ? ` · ${t("shadows.nonSimulatedRows")} ${model.zeroOrder.extra}`
            : ""}
        </span>
      </div>
    </Card>
  );
}

export function ShadowComparisonTable({ comparison }: { comparison: ShadowComparison }) {
  const { t } = useI18n();
  return (
    <Card
      tone="SHADOW"
      title={t("shadows.comparison")}
      meta={
        comparison.insufficient ? (
          <Pill tone="ATTENTION" emphasis title={comparison.warning}>
            {t("shadows.insufficientSample")}
          </Pill>
        ) : (
          <Tag tone="SHADOW">{t("shadows.thresholdsMet")}</Tag>
        )
      }
      bodyClassName=""
    >
      <div className="scroll-x">
        <table className="w-full min-w-[720px] border-collapse">
          <thead>
            <tr className="border-b border-subtle">
              <Th>{t("shadows.title")}</Th>
              <Th>EDA-1 Shadow</Th>
              <Th>A1-B U30 Shadow</Th>
            </tr>
          </thead>
          <tbody>
            {comparison.rows.map((row) => (
              <tr key={row.key} className="border-b border-subtle/70 last:border-0">
                <Td className="hint text-ink-2" title={row.hint}>
                  {row.label}
                </Td>
                {[row.eda1, row.a1b].map((cell, index) => (
                  <Td key={index} className={cn("num", cell.conclusive ? "text-ink" : "text-warn")}>
                    {cell.text}
                    {cell.raw ? (
                      <span className="ms-2 text-eyebrow text-ink-3">{t("shadows.raw", { value: cell.raw })}</span>
                    ) : null}
                  </Td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="px-4 pt-3 pb-3 text-meta leading-snug text-ink-3">{comparison.warning}</p>
    </Card>
  );
}

function A1BSymbolChart({ symbol, onClose }: { symbol: string; onClose: () => void }) {
  const { t } = useI18n();
  const [range, setRange] = useState<ChartRange>("1D");
  const { series } = useChartBatch([symbol], range);
  return (
    <div className="border-t border-subtle px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-table font-semibold text-ink">
          {symbol} <span className="text-ink-3">price · observation universe</span>
        </span>
        <div className="flex items-center gap-2">
          <SegmentedTimeRange options={CHART_RANGES} value={range} onChange={setRange} label={t("chart.range")} />
          <button
            type="button"
            onClick={onClose}
            className="rounded-xs px-2 py-1 text-eyebrow font-medium tracking-[0.06em] text-ink-3 uppercase ring-1 ring-subtle hover:text-ink focus-visible:outline-2 focus-visible:outline-accent"
          >
            {t("common.close")}
          </button>
        </div>
      </div>
      <div className="mt-2 max-w-[880px]">
        <LineChart series={series[symbol]} />
      </div>
      <p className="mt-1 text-meta text-ink-3">{t("chart.priceOnly")}</p>
    </div>
  );
}

export function A1BUniverse({ overview, generatedAt }: { overview: A1BOverview | null; generatedAt: string | null }) {
  const { t } = useI18n();
  const format = useFormat();
  const [selected, setSelected] = useState<string | null>(null);
  if (!overview) {
    return (
      <Card tone="SHADOW" title="A1-B U30 universe">
        <Empty headline={t("shadows.apiDown")} />
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
            <tr className="border-b border-subtle">
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
                aria-label={t("drawer.showChart", { symbol: row.symbol })}
                aria-pressed={selected === row.symbol}
                onClick={() => setSelected(selected === row.symbol ? null : row.symbol)}
                onKeyDown={open(row.symbol)}
                className={cn("row-link border-b border-subtle/70 last:border-0 hover:bg-surface-2", selected === row.symbol && "bg-surface-2")}
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
                  {row.bar_timestamp ? format.stamp(row.bar_timestamp, generatedAt) : "—"}
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
        <p className="px-4 pt-2 pb-3 text-meta text-ink-3">{t("chart.selectSymbol")}</p>
      )}
    </Card>
  );
}

export function A1BDetail({ overview, generatedAt }: { overview: A1BOverview | null; generatedAt: string | null }) {
  const format = useFormat();
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
          <Field label="Observer started">{service.started_at ? format.stamp(service.started_at, generatedAt) : "—"}</Field>
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
        <p className="mt-3 text-meta leading-snug text-ink-3">{service.invariant_note}</p>
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
        <p className="mt-3 text-meta leading-snug text-warn">{hypothetical.sample_warning}</p>
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
      parityCount: parityMismatches,
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
      parityCount: null,
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

/**
 * The Overview's condensed shadow row.
 *
 * Both observers as one violet strip: unit state, what each one is, and the
 * standing statement that neither can act. It carries no performance figure at
 * all — a summary is exactly the place a hypothetical index would be mistaken
 * for account equity, so the numbers stay on the Shadows page behind their
 * sample thresholds.
 */
export function ShadowSummary({ services }: { services: ServiceUnitsPanel | null }) {
  const { t } = useI18n();
  const rows = [
    { key: "equity_shadow", name: "Equity Shadow · V3 + EDA-1" },
    { key: A1B_SHADOW_KEY, name: "A1-B U30 Shadow" },
  ] as const;

  return (
    <Card tone="SHADOW" title={t("shadows.observationOnly")} bodyClassName="">
      <div className="divide-y divide-subtle/70 px-4">
        {rows.map((row) => {
          const unit = serviceUnit(services, row.key);
          const shown = unit ? displayStatus(unit) : { status: "UNKNOWN", tone: "ATTENTION" as const };
          return (
            <div key={row.key} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 py-3">
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <span className="text-table font-medium text-ink">{row.name}</span>
                <Tag tone="SHADOW" title={t("strategies.observationOnlyHint")}>
                  {t("strategies.observationOnly")}
                </Tag>
                <Tag tone="SHADOW">{t("strategies.capability.zeroOrders")}</Tag>
              </div>
              <Status tone={shown.tone} title={unit?.detail ?? t("status.unknownHint")}>
                {shown.status}
              </Status>
            </div>
          );
        })}
      </div>
      <p className="px-4 pt-1 pb-3.5 text-meta leading-snug text-ink-3">{t("shadows.banner")}</p>
    </Card>
  );
}
