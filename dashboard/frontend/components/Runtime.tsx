"use client";

/**
 * The crypto runtime's own trail, as it describes itself.
 *
 * **Read the label carefully: this card is not the list of services.** The
 * operational store was written by two processes — the 24/7 crypto runtime, and
 * the older general equity runtime that is now masked. The current equity book
 * runs as `autotrader-equity-paper.service` against a different store this API
 * never opens, so it cannot appear here. The health panel names every unit;
 * this card shows the one trail worth reading in full, and the masked legacy
 * trail collapses to a single line.
 *
 * Nothing here is read from a live heartbeat — that is an in-process object
 * belonging to a different process. Every field comes from something that
 * runtime wrote down, and `Last cycle` is therefore the newest durable bar
 * claim rather than a heartbeat tick.
 *
 * There is no start button, no stop button, and no endpoint behind either.
 */

import { useI18n } from "@/lib/i18n";
import { useFormat } from "@/lib/i18n/useFormat";
import type { ServiceUnitsPanel } from "@/lib/services";
import { LEGACY_EQUITY_KEY, serviceUnit, trailPanelLabel } from "@/lib/services";
import type { RuntimePanel, Tone } from "@/lib/types";

import { Card, DataTable, Field, Status, Tag, Td, Th, Tr, cn } from "./ui";

/**
 * How a trail-derived panel should be titled and headlined on screen.
 *
 * The trail says what the process recorded; the service manager says whether
 * the unit exists and is allowed to run. When they disagree about the legacy
 * runtime — trail says "stopped cleanly", manager says "masked" — the manager
 * wins the headline, because "masked" is the fact an operator needs and
 * "stopped" is the one that misleads them.
 */
export function runtimeView(
  panel: RuntimePanel,
  services: ServiceUnitsPanel | null,
  legacyNote: string,
): { label: string; state: string; tone: Tone; detail: string | null; note: string | null } {
  const label = trailPanelLabel(panel.key, panel.label);

  if (panel.key !== "equity") {
    return { label, state: panel.state, tone: panel.tone, detail: panel.detail, note: null };
  }

  const unit = serviceUnit(services, LEGACY_EQUITY_KEY);
  if (unit === null) {
    return { label, state: panel.state, tone: panel.tone, detail: panel.detail, note: legacyNote };
  }
  return { label: unit.label, state: unit.status, tone: unit.tone, detail: unit.detail, note: legacyNote };
}

function staleMatters(state: string): boolean {
  return state === "RUNNING" || state === "STALE";
}

function RuntimeStrip({
  panel,
  services,
  generatedAt,
}: {
  panel: RuntimePanel;
  services: ServiceUnitsPanel | null;
  generatedAt: string | null;
}) {
  const { t } = useI18n();
  const format = useFormat();
  const view = runtimeView(panel, services, t("strategies.legacyNote"));
  const flagStale = staleMatters(view.state);

  return (
    <Card
      title={view.label}
      meta={
        <>
          <Tag title={t("strategies.noRealMoneyHint")}>Paper</Tag>
          <Status tone={view.tone} title={view.detail ?? undefined}>
            {view.state}
          </Status>
        </>
      }
      bodyClassName=""
    >
      <div className="grid grid-cols-2 gap-x-5 gap-y-3 px-4 pb-3 sm:grid-cols-3">
        <Field label={t("system.startupSafety")} title={panel.startup_safety_detail ?? undefined}>
          <Status tone={panel.startup_safety_tone}>{panel.startup_safety}</Status>
        </Field>
        <Field label={t("system.paperExecution")} title={panel.paper_execution_detail ?? undefined}>
          <span
            className={cn(
              "text-table font-medium tracking-[0.06em] uppercase",
              panel.paper_execution_enabled ? "text-ink" : "text-ink-3",
            )}
          >
            {panel.paper_execution_enabled ? t("system.enabled") : t("system.disabled")}
          </span>
        </Field>
        <Field label={panel.ended_at ? t("system.ran") : t("system.started")}>
          <span className="num">
            {format.stamp(panel.started_at, generatedAt)}
            {panel.ended_at ? (
              <span className="text-ink-3"> → {format.stamp(panel.ended_at, generatedAt)}</span>
            ) : null}
          </span>
        </Field>
        <Field label={t("strategies.lastCycle")} title={t("system.checkpointsHint")}>
          <span className="num">
            {format.stamp(panel.last_cycle_at, generatedAt)}
            {panel.last_cycle_at ? (
              <span className="ms-1.5 text-meta text-ink-3">
                {format.relative(panel.last_cycle_at, generatedAt)}
              </span>
            ) : null}
          </span>
        </Field>
        <Field label={t("system.nextCycle")}>
          <span className="num">
            {format.stamp(panel.next_cycle_at, generatedAt)}
            {panel.next_cycle_at ? (
              <span className="ms-1.5 text-meta text-ink-3">
                {format.relative(panel.next_cycle_at, generatedAt)}
              </span>
            ) : null}
          </span>
        </Field>
        <Field label={t("system.symbolsClaimed")}>
          <span className="num">{panel.checkpoints.length}</span>
        </Field>
      </div>

      {panel.checkpoints.length ? (
        <div className="border-t border-subtle">
          <h3 className="eyebrow px-4 pt-3 text-ink-3">{t("system.checkpoints")}</h3>
          <div className="mt-1 px-1 pb-2">
            <DataTable
              caption={t("system.checkpoints")}
              minWidth="min-w-[320px]"
              sticky={false}
              head={
                <>
                  <Th>{t("system.col.symbol")}</Th>
                  <Th align="right">{t("system.col.lastBar")}</Th>
                  <Th align="right">{t("system.col.claimed")}</Th>
                </>
              }
            >
              {panel.checkpoints.map((checkpoint) => (
                <Tr key={checkpoint.symbol}>
                  <Td className="font-medium text-ink">{checkpoint.symbol}</Td>
                  <Td numeric className="text-ink-2">
                    {format.stamp(checkpoint.last_processed_bar, generatedAt)}
                  </Td>
                  <Td numeric className={flagStale && checkpoint.stale ? "text-warn" : "text-ink-3"}>
                    {format.relative(checkpoint.updated_at, generatedAt)}
                  </Td>
                </Tr>
              ))}
            </DataTable>
          </div>
        </div>
      ) : null}
    </Card>
  );
}

function LegacyLine({ panel, services }: { panel: RuntimePanel; services: ServiceUnitsPanel | null }) {
  const { t } = useI18n();
  const view = runtimeView(panel, services, t("strategies.legacyNote"));
  return (
    <div className="panel flex flex-wrap items-center justify-between gap-2 px-4 py-2.5">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="text-table font-medium text-ink-2">{view.label}</span>
        <Tag>{t("strategies.intentionallyOff")}</Tag>
        <span className="text-meta text-ink-3">{view.note}</span>
      </div>
      <Status tone={view.tone} title={view.detail ?? undefined}>
        {view.state}
      </Status>
    </div>
  );
}

export function Runtimes({
  panels,
  services,
  generatedAt,
}: {
  panels: RuntimePanel[];
  services: ServiceUnitsPanel | null;
  generatedAt: string | null;
}) {
  const { t } = useI18n();
  if (panels.length === 0) {
    return (
      <Card title={t("system.runtimeTrail")}>
        <p className="text-table text-ink-3">{t("system.unitsUnreadable")}</p>
      </Card>
    );
  }

  return (
    <div className="space-y-3">
      {panels.map((panel) =>
        panel.key === "equity" ? (
          <LegacyLine key={panel.key} panel={panel} services={services} />
        ) : (
          <RuntimeStrip key={panel.key} panel={panel} services={services} generatedAt={generatedAt} />
        ),
      )}
    </div>
  );
}
