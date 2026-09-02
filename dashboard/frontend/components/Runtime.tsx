/**
 * The crypto runtime's own trail, as it describes itself.
 *
 * **Read the label carefully: this card is not the list of services.** The
 * operational store was written by two processes - the 24/7 crypto runtime,
 * and the older general equity runtime that is now masked. The current equity
 * book runs as `autotrader-equity-paper.service` against a different store
 * this API never opens, so it cannot appear here. The health panel names every
 * unit; this card shows the one trail that is worth reading in full, and the
 * masked legacy trail collapses to a single line.
 *
 * Nothing here is read from a live heartbeat - that is an in-process object
 * belonging to a different process. Every field comes from something that
 * runtime wrote down, and `Last cycle` is therefore the newest durable bar
 * claim rather than a heartbeat tick.
 *
 * There is no start button, no stop button, and no endpoint behind either.
 */

import { relative, stampUtc } from "@/lib/format";
import type { ServiceUnitsPanel } from "@/lib/services";
import { LEGACY_EQUITY_KEY, serviceUnit, trailPanelLabel } from "@/lib/services";
import type { RuntimePanel, Tone } from "@/lib/types";

import { Card, Field, Status, Tag, Td, Th, cn } from "./ui";

/** How a trail-derived panel should be titled and headlined on screen.
 *
 * The trail says what the process recorded; the service manager says whether
 * the unit exists and is allowed to run. When they disagree about the legacy
 * runtime - trail says "stopped cleanly", manager says "masked" - the manager
 * wins the headline, because "masked" is the fact an operator needs and
 * "stopped" is the one that misleads them.
 */
export function runtimeView(
  panel: RuntimePanel,
  services: ServiceUnitsPanel | null,
): { label: string; state: string; tone: Tone; detail: string | null; note: string | null } {
  const label = trailPanelLabel(panel.key, panel.label);

  if (panel.key !== "equity") {
    return { label, state: panel.state, tone: panel.tone, detail: panel.detail, note: null };
  }

  const unit = serviceUnit(services, LEGACY_EQUITY_KEY);
  const note = "Superseded by Equity Paper · EDA-1. Not the current equity runtime.";
  if (unit === null) {
    return { label, state: panel.state, tone: panel.tone, detail: panel.detail, note };
  }
  return { label: unit.label, state: unit.status, tone: unit.tone, detail: unit.detail, note };
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
  const view = runtimeView(panel, services);
  const flagStale = staleMatters(view.state);

  return (
    <Card
      title={view.label}
      meta={
        <>
          <Tag title="Broker paper account. No real money.">Paper</Tag>
          <Status tone={view.tone} title={view.detail ?? undefined}>
            {view.state}
          </Status>
        </>
      }
      bodyClassName=""
    >
      <div className="grid grid-cols-2 gap-x-5 gap-y-3 px-4 pb-3 sm:grid-cols-3">
        <Field label="Startup safety" title={panel.startup_safety_detail ?? undefined}>
          <Status tone={panel.startup_safety_tone}>{panel.startup_safety}</Status>
        </Field>
        <Field label="Paper execution" title={panel.paper_execution_detail ?? undefined}>
          <span className={cn("text-[12px] font-medium tracking-[0.06em] uppercase", panel.paper_execution_enabled ? "text-ink" : "text-ink-3")}>
            {panel.paper_execution_enabled ? "Enabled" : "Disabled"}
          </span>
        </Field>
        <Field label={panel.ended_at ? "Ran" : "Started"}>
          <span className="num">
            {stampUtc(panel.started_at, generatedAt)}
            {panel.ended_at ? <span className="text-ink-3"> → {stampUtc(panel.ended_at, generatedAt)}</span> : null}
          </span>
        </Field>
        <Field label="Last cycle" title="The newest durable bar claim. The runtime's live heartbeat is not persisted.">
          <span className="num">
            {stampUtc(panel.last_cycle_at, generatedAt)}
            {panel.last_cycle_at ? <span className="ml-1.5 text-[11px] text-ink-3">{relative(panel.last_cycle_at, generatedAt)}</span> : null}
          </span>
        </Field>
        <Field label="Next 15m cycle" title="Next UTC boundary plus the provider-lag allowance.">
          <span className="num">
            {stampUtc(panel.next_cycle_at, generatedAt)}
            {panel.next_cycle_at ? <span className="ml-1.5 text-[11px] text-ink-3">{relative(panel.next_cycle_at, generatedAt)}</span> : null}
          </span>
        </Field>
        <Field label="Symbols claimed">
          <span className="num">{panel.checkpoints.length}</span>
        </Field>
      </div>

      {panel.checkpoints.length ? (
        <div className="border-t border-line">
          <h3 className="eyebrow px-4 pt-3 text-ink-3">Processed-bar checkpoints</h3>
          <div className="scroll-x mt-1 px-1 pb-2">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <Th>Symbol</Th>
                  <Th align="right">Last processed bar</Th>
                  <Th align="right">Claimed</Th>
                </tr>
              </thead>
              <tbody>
                {panel.checkpoints.map((checkpoint) => (
                  <tr key={checkpoint.symbol} className="border-t border-line/70">
                    <Td className="font-medium text-ink">{checkpoint.symbol}</Td>
                    <Td numeric className="text-ink-2">
                      {stampUtc(checkpoint.last_processed_bar, generatedAt)}
                    </Td>
                    <Td numeric className={flagStale && checkpoint.stale ? "text-warn" : "text-ink-3"}>
                      {relative(checkpoint.updated_at, generatedAt)}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </Card>
  );
}

function LegacyLine({ panel, services }: { panel: RuntimePanel; services: ServiceUnitsPanel | null }) {
  const view = runtimeView(panel, services);
  return (
    <div className="card flex flex-wrap items-center justify-between gap-2 px-4 py-2.5">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="text-[12.5px] font-medium text-ink-2">{view.label}</span>
        <Tag>Intentionally off</Tag>
        <span className="text-[11px] text-ink-3">{view.note}</span>
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
  if (panels.length === 0) {
    return (
      <Card title="Runtimes">
        <p className="text-[12px] text-ink-3">Runtime state could not be read.</p>
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
