/**
 * The two trails in the operational store, as they describe themselves.
 *
 * **Read the labels carefully: this card is not the list of equity services.**
 * The operational database was written by two processes - the 24/7 crypto
 * runtime, and the older general equity runtime that is now masked. The
 * current equity book runs as `autotrader-equity-paper.service` against a
 * different store this API never opens, so it cannot appear here and its
 * absence must not be read as its absence from the host. That is exactly the
 * inference that made this page report equity trading as stopped, so the
 * second card is titled "Legacy Equity Runtime" and takes its headline state
 * from the service manager rather than from the trail underneath it.
 *
 * Each gets its own strip, in a fixed order, because a single merged panel
 * would have to average two states into one and would be wrong whenever they
 * differ.
 *
 * A full-width strip rather than a column card: each panel is a row of small
 * facts, and they read in one pass across the page where they would be cramped
 * stacked in a narrow column.
 *
 * Nothing here is read from a live heartbeat - that is an in-process object
 * belonging to a different process. Every field comes from something that
 * runtime wrote down, and `Last cycle` is therefore the newest durable bar
 * claim rather than a heartbeat tick. The label says so.
 *
 * The last failure event is deliberately **not** here: it belongs to the
 * account rather than to a service, and the page reports it once.
 *
 * There is no start button, no stop button, and no endpoint behind either.
 */

import { relative, stampUtc } from "@/lib/format";
import type { ServiceUnitsPanel } from "@/lib/services";
import { LEGACY_EQUITY_KEY, serviceUnit, trailPanelLabel } from "@/lib/services";
import type { RuntimePanel, Tone } from "@/lib/types";

import { Card, Dot, Field, Tag, Td, Th, cn, toneText } from "./ui";

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
  // The label is always the registry's, never the payload's. The operational
  // API's deployed build is pinned and still sends "Crypto runtime"; a page
  // that showed one name in the health panel and another on the card would
  // have reintroduced the ambiguity in a smaller font.
  const label = trailPanelLabel(panel.key, panel.label);

  if (panel.key !== "equity") {
    // The headline stays the trail's. This card is about the loop, and the
    // trail is the only source that can say STALE - a unit the manager calls
    // active while it has stopped claiming bars.
    return { label, state: panel.state, tone: panel.tone, detail: panel.detail, note: null };
  }

  const unit = serviceUnit(services, LEGACY_EQUITY_KEY);
  const note = "Superseded by Equity Paper · EDA-1. Not the current equity runtime.";
  if (unit === null) {
    return { label, state: panel.state, tone: panel.tone, detail: panel.detail, note };
  }
  return { label: unit.label, state: unit.status, tone: unit.tone, detail: unit.detail, note };
}

/** Whether a stale checkpoint is worth flagging.
 *
 * A stopped runtime has not claimed a bar recently by definition, and colouring
 * that amber would train an operator to ignore the colour. Staleness only means
 * something while the runtime claims to be looping.
 */
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

  const meta = (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-[11px] leading-none font-medium",
        "tracking-[0.06em] uppercase",
        toneText(view.tone),
      )}
      title={view.detail ?? undefined}
    >
      <Dot tone={view.tone} />
      {view.state}
    </span>
  );

  return (
    <Card title={view.label} meta={meta} bodyClassName="">
      {view.note ? (
        <p className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2 text-[11.5px] leading-snug text-ink-3">
          <Tag>Intentionally off</Tag>
          {view.note}
        </p>
      ) : null}
      <div className="grid grid-cols-2 gap-x-5 gap-y-3.5 px-4 py-3.5 sm:grid-cols-3">
        <Field label="Startup safety" title={panel.startup_safety_detail ?? undefined}>
          <span
            className={cn(
              "inline-flex items-center gap-1.5 text-[12px] font-medium tracking-[0.06em] uppercase",
              toneText(panel.startup_safety_tone),
            )}
          >
            <Dot tone={panel.startup_safety_tone} />
            {panel.startup_safety}
          </span>
        </Field>
        <Field label="Paper execution" title={panel.paper_execution_detail ?? undefined}>
          <span
            className={cn(
              "text-[12px] font-medium tracking-[0.06em] uppercase",
              panel.paper_execution_enabled ? "text-ink" : "text-ink-3",
            )}
          >
            {panel.paper_execution_enabled ? "Enabled" : "Disabled"}
          </span>
        </Field>
        <Field label={panel.ended_at ? "Ran" : "Started"}>
          <span className="num">
            {stampUtc(panel.started_at, generatedAt)}
            {panel.ended_at ? (
              <span className="text-ink-3"> → {stampUtc(panel.ended_at, generatedAt)}</span>
            ) : null}
          </span>
        </Field>
        <Field
          label="Last cycle"
          title="The newest durable bar claim. The runtime's live heartbeat is not persisted."
        >
          <span className="num">
            {stampUtc(panel.last_cycle_at, generatedAt)}
            {panel.last_cycle_at ? (
              <span className="ml-1.5 text-[11px] text-ink-3">
                {relative(panel.last_cycle_at, generatedAt)}
              </span>
            ) : null}
          </span>
        </Field>
        <Field label="Next 15m cycle" title="Next UTC boundary plus the provider-lag allowance.">
          <span className="num">
            {stampUtc(panel.next_cycle_at, generatedAt)}
            {panel.next_cycle_at ? (
              <span className="ml-1.5 text-[11px] text-ink-3">
                {relative(panel.next_cycle_at, generatedAt)}
              </span>
            ) : null}
          </span>
        </Field>
        <Field label="Symbols claimed">
          <span className="num">{panel.checkpoints.length}</span>
        </Field>
      </div>

      <div className="border-t border-line">
        <h3 className="eyebrow px-4 pt-3 text-ink-3">Processed-bar checkpoints</h3>
        {panel.checkpoints.length === 0 ? (
          <p className="px-4 py-3 text-[12px] text-ink-3">No bar has been claimed yet.</p>
        ) : (
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
                  <tr key={checkpoint.symbol} className="border-t border-line">
                    <Td className="font-medium text-ink">{checkpoint.symbol}</Td>
                    <Td numeric className="text-ink-2">
                      {stampUtc(checkpoint.last_processed_bar, generatedAt)}
                    </Td>
                    <Td
                      numeric
                      className={flagStale && checkpoint.stale ? "text-warn" : "text-ink-3"}
                    >
                      {relative(checkpoint.updated_at, generatedAt)}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Card>
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
    <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
      {panels.map((panel) => (
        <RuntimeStrip
          key={panel.key}
          panel={panel}
          services={services}
          generatedAt={generatedAt}
        />
      ))}
    </div>
  );
}
