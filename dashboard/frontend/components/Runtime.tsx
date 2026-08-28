/**
 * The two loops, as their durable trails describe them.
 *
 * There are two services now - the 24/7 crypto runtime and the regular-session
 * equity runtime - and they run as separate processes. Each gets its own strip,
 * in a fixed order, because a single merged panel would have to average two
 * states into one and would be wrong whenever they differ.
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
import type { RuntimePanel } from "@/lib/types";

import { Card, Dot, Field, Td, Th, cn, toneText } from "./ui";

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
  generatedAt,
}: {
  panel: RuntimePanel;
  generatedAt: string | null;
}) {
  const flagStale = staleMatters(panel.state);

  const meta = (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-[11px] leading-none font-medium",
        "tracking-[0.06em] uppercase",
        toneText(panel.tone),
      )}
      title={panel.detail ?? undefined}
    >
      <Dot tone={panel.tone} />
      {panel.state}
    </span>
  );

  return (
    <Card title={panel.label} meta={meta} bodyClassName="">
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
  generatedAt,
}: {
  panels: RuntimePanel[];
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
        <RuntimeStrip key={panel.key} panel={panel} generatedAt={generatedAt} />
      ))}
    </div>
  );
}
