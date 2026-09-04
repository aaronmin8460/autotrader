"use client";

/**
 * The strategy lifecycle: what is deployed and trading, what only observes,
 * and what is deliberately neither.
 *
 * One row per strategy, and the three facts that distinguish them are stated
 * as capabilities rather than implied by colour: **order capability**,
 * **position capability**, and the runtime state the service manager reports.
 *
 *   EDA-1        PRIMARY · PAPER   submits real paper orders, holds positions
 *   A1-B U30     OBSERVER · SHADOW ZERO ORDERS, holds no position
 *   V3 + EDA-1   OBSERVER · SHADOW ZERO ORDERS, holds no position
 *   legacy       LEGACY            masked on purpose; not the equity runtime
 *
 * A shadow can never be mistaken for a paper strategy here: it is violet, it
 * says OBSERVATION ONLY and ZERO ORDERS in its own row, and its runtime word
 * is OBSERVING rather than the green RUNNING a trading process gets. A future
 * strategy is one more row.
 *
 * Every figure is the runtime's own. Nothing on this page is a placeholder for
 * a strategy that is not deployed, and no research candidate appears at all.
 */

import Link from "next/link";

import { useI18n } from "@/lib/i18n";
import { useFormat } from "@/lib/i18n/useFormat";
import { percent } from "@/lib/format";
import type { PaperOverview } from "@/lib/paper";
import type { ServiceUnitsPanel } from "@/lib/services";
import { A1B_SHADOW_KEY, LEGACY_EQUITY_KEY, displayStatus, serviceUnit } from "@/lib/services";

import { Card, Field, Status, StrategyBadge, Tag, cn } from "./ui";

export interface StrategyModel {
  key: string;
  /** The runtime's own identifier. Never translated. */
  name: string;
  roleKey: "strategies.role.primary" | "strategies.role.observer" | "strategies.role.legacy";
  mode: "PAPER" | "SHADOW" | null;
  unitKey: string;
  universeSize: number | null;
  policy: string | null;
  policyHash: string | null;
  stage: string | null;
  targetGross: number | null;
  lastCycleAt: string | null;
  canSubmitOrders: boolean;
  holdsPositions: boolean;
  href: string | null;
  observe: boolean;
}

export function strategyModels(paper: PaperOverview | null): StrategyModel[] {
  return [
    {
      key: "eda1",
      name: "EDA-1",
      roleKey: "strategies.role.primary",
      mode: "PAPER",
      unitKey: "equity_paper",
      universeSize: paper?.service.execution_universe?.length ?? null,
      policy: paper?.policy?.policy_id ?? paper?.service.sizing_policy ?? null,
      policyHash: paper?.policy?.config_hash ?? paper?.service.sizing_config_hash ?? null,
      stage: paper?.service.stage ?? null,
      targetGross: paper?.policy?.target_gross ?? null,
      lastCycleAt: paper?.service.last_cycle_at ?? null,
      canSubmitOrders: true,
      holdsPositions: true,
      href: "/equity-paper",
      observe: false,
    },
    {
      key: "a1b",
      name: "A1-B U30",
      roleKey: "strategies.role.observer",
      mode: "SHADOW",
      unitKey: A1B_SHADOW_KEY,
      universeSize: null,
      policy: null,
      policyHash: null,
      stage: null,
      targetGross: null,
      lastCycleAt: null,
      canSubmitOrders: false,
      holdsPositions: false,
      href: "/shadows",
      observe: true,
    },
    {
      key: "v3-eda1",
      name: "V3 + EDA-1",
      roleKey: "strategies.role.observer",
      mode: "SHADOW",
      unitKey: "equity_shadow",
      universeSize: null,
      policy: null,
      policyHash: null,
      stage: null,
      targetGross: null,
      lastCycleAt: null,
      canSubmitOrders: false,
      holdsPositions: false,
      href: "/shadows",
      observe: true,
    },
    {
      key: "legacy",
      name: "Legacy Equity Runtime",
      roleKey: "strategies.role.legacy",
      mode: null,
      unitKey: LEGACY_EQUITY_KEY,
      universeSize: null,
      policy: null,
      policyHash: null,
      stage: null,
      targetGross: null,
      lastCycleAt: null,
      canSubmitOrders: false,
      holdsPositions: false,
      href: null,
      observe: false,
    },
  ];
}

export function StrategyCard({
  model,
  services,
  generatedAt,
}: {
  model: StrategyModel;
  services: ServiceUnitsPanel | null;
  generatedAt: string | null;
}) {
  const { t } = useI18n();
  const format = useFormat();
  const unit = serviceUnit(services, model.unitKey);
  const shown = unit ? displayStatus(unit) : { status: "UNKNOWN", tone: "ATTENTION" as const };
  const legacy = model.roleKey === "strategies.role.legacy";

  return (
    <Card
      tone={model.observe ? "SHADOW" : undefined}
      title={
        <span className="flex flex-wrap items-center gap-2">
          <span className={cn(model.observe && "text-observe")}>{model.name}</span>
          <Tag tone={model.observe ? "SHADOW" : undefined}>{t(model.roleKey)}</Tag>
          {model.mode ? <Tag tone={model.observe ? "SHADOW" : undefined}>{model.mode}</Tag> : null}
        </span>
      }
      meta={
        <>
          {model.mode ? <StrategyBadge kind={model.mode} /> : <Tag>{t("strategies.intentionallyOff")}</Tag>}
          <Status tone={shown.tone} title={unit?.detail ?? t("status.unknownHint")}>
            {shown.status}
          </Status>
        </>
      }
    >
      <div className="grid grid-cols-2 gap-x-5 gap-y-3.5 sm:grid-cols-4">
        <Field label={t("strategies.capability.orders")}>
          <span
            className={cn(
              "text-table font-medium tracking-[0.04em]",
              model.canSubmitOrders ? "text-ink" : "text-observe",
            )}
          >
            {model.canSubmitOrders
              ? t("strategies.capability.canSubmit")
              : t("strategies.capability.zeroOrders")}
          </span>
        </Field>
        <Field label={t("strategies.capability.positions")}>
          <span className="text-table text-ink-2">
            {model.holdsPositions
              ? t("strategies.capability.holdsPositions")
              : t("strategies.capability.noPositions")}
          </span>
        </Field>
        <Field label={t("strategies.decisionClock")}>
          <span className="text-table text-ink-2">{t("strategies.decisionClock15m")}</span>
        </Field>
        <Field label={t("strategies.universe")}>
          <span className="num">
            {model.universeSize === null
              ? "—"
              : t("strategies.symbols", { count: model.universeSize })}
            {model.stage ? (
              <span className="text-ink-3"> · {t("strategies.stage")} {model.stage}</span>
            ) : null}
          </span>
        </Field>
        <Field label={t("strategies.policy")}>
          <span className="num">{model.policy ?? "—"}</span>
        </Field>
        <Field label={t("market.targetGross")}>
          <span className="num">
            {model.targetGross === null ? "—" : percent(model.targetGross, 0)}
          </span>
        </Field>
        <Field label={t("strategies.lastCycle")}>
          <span className="num">
            {model.lastCycleAt ? format.stamp(model.lastCycleAt, generatedAt) : "—"}
          </span>
        </Field>
        <Field label={t("strategies.runtimeState")}>
          <span className="num text-ink-2">{unit?.unit ?? "—"}</span>
        </Field>
      </div>

      {legacy ? (
        <p className="mt-3.5 text-meta leading-snug text-ink-3">{t("strategies.legacyNote")}</p>
      ) : null}

      {model.href ? (
        <div className="mt-4 border-t border-subtle pt-3">
          <Link
            href={model.href}
            className={cn(
              "rounded-xs px-1 py-0.5 text-meta font-medium hover:underline",
              "focus-visible:outline-2 focus-visible:outline-accent",
              model.observe ? "text-observe" : "text-accent",
            )}
          >
            {t("strategies.openDetail")} →
          </Link>
        </div>
      ) : null}
    </Card>
  );
}
