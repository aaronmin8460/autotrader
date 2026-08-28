/**
 * The 24/7 loop, as its durable trail describes it.
 *
 * A full-width strip rather than a column card: this panel is a row of small
 * facts, and six of them read in one pass across the page where they would be
 * cramped stacked in a narrow column.
 *
 * Nothing here is read from the runtime's live heartbeat - that is an
 * in-process object belonging to a different process. Every field comes from
 * something the runtime wrote down, and `Last cycle` is therefore the newest
 * durable bar claim rather than a heartbeat tick. The label says so.
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

export function Runtime({
  panel,
  generatedAt,
}: {
  panel: RuntimePanel | null;
  generatedAt: string | null;
}) {
  if (!panel) {
    return (
      <Card title="Runtime">
        <p className="text-[12px] text-ink-3">Runtime state could not be read.</p>
      </Card>
    );
  }

  const flagStale = staleMatters(panel.state);

  const meta = (
    <>
      {panel.mode ? <span className="text-[11px] text-ink-3">{panel.mode}</span> : null}
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
    </>
  );

  return (
    <Card title="Runtime" meta={meta} bodyClassName="">
      <div className="grid grid-cols-2 gap-x-5 gap-y-3.5 px-4 py-3.5 sm:grid-cols-3">
        <Field label="Strategy">{panel.strategy_name ?? "—"}</Field>
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
        <Field label={panel.ended_at ? "Ran" : "Started"}>
          <span className="num">
            {stampUtc(panel.started_at, generatedAt)}
            {panel.ended_at ? (
              <span className="text-ink-3"> → {stampUtc(panel.ended_at, generatedAt)}</span>
            ) : null}
          </span>
        </Field>
      </div>

      <div className="grid grid-cols-1 border-t border-line lg:grid-cols-2">
        <div className="lg:border-r lg:border-line">
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

        <div className="border-t border-line px-4 py-3 lg:border-t-0">
          <div className="flex items-baseline justify-between gap-3">
            <h3 className="eyebrow text-ink-3">Last failure event</h3>
            {panel.last_error_at ? (
              <span className="num text-[11px] text-ink-3">
                {stampUtc(panel.last_error_at, generatedAt)} UTC ·{" "}
                {relative(panel.last_error_at, generatedAt)}
              </span>
            ) : null}
          </div>
          <p className="mt-1.5 text-[11.5px] leading-snug text-ink-2">
            {panel.last_error ?? (
              <span className="text-ink-3">No failure event is recorded.</span>
            )}
          </p>
        </div>
      </div>
    </Card>
  );
}
