/**
 * Health, and the reconciliation pass behind it.
 *
 * Subsystem rows, then the latest reconciliation result in full. They share one
 * card because they are one question: reconciliation is the thing that decides
 * whether trading is allowed, so putting it a scroll away from the health list
 * would separate a verdict from its evidence.
 *
 * Every status is quoted from stored truth. `CLEAN` is never inferred from the
 * absence of an error, and a database with no reconciliation run in it reports
 * `NEVER RUN`.
 *
 * **The runtime rows come from the service manager, not from a store.** Three
 * separate services on this host answer to the word "equity" and only one of
 * them ever wrote to the operational database, so a row derived from that
 * database reported the masked legacy service and called it "Equity runtime".
 * `healthRows` replaces those rows with one per named unit - see `lib/services`
 * for why that is a replacement rather than a rename.
 *
 * Three colours, three meanings, and the middle one is the point: green is an
 * expected running service, red is a service that is not what this host is
 * configured for, and neutral is a service that is off on purpose. `MASKED` on
 * the legacy runtime is neutral, because it is a decision rather than a fault.
 */

import { relative, stampUtc } from "@/lib/format";
import type { HealthRow, ServiceUnitsPanel } from "@/lib/services";
import { healthRows } from "@/lib/services";
import type { HealthComponent, ReconciliationPanel } from "@/lib/types";

import { Card, Dot, cn, toneText } from "./ui";

function Row({ component }: { component: HealthRow }) {
  const loud = component.tone === "ATTENTION" || component.tone === "NEGATIVE";
  return (
    <div className="flex items-start justify-between gap-3 py-2 first:pt-0 last:pb-0">
      <div className="min-w-0">
        <div className="text-[12.5px] leading-tight text-ink-2" title={component.unit ?? undefined}>
          {component.label}
        </div>
        {/* Shown whatever the status. "PAPER · NO REAL MONEY" and "ZERO ORDERS"
            are properties of the service, not warnings about it, and a reader
            who only sees them when something is wrong has learnt the opposite. */}
        {component.note ? (
          <div className="mt-0.5 text-[10px] leading-none tracking-[0.06em] text-ink-3 uppercase">
            {component.note}
          </div>
        ) : null}
        {loud && component.detail ? (
          <div className="mt-0.5 text-[11px] leading-snug text-ink-3">{component.detail}</div>
        ) : null}
      </div>
      <span
        className={cn(
          "mt-[3px] inline-flex shrink-0 items-center gap-1.5 text-[11px] leading-none",
          "font-medium tracking-[0.06em] uppercase",
          toneText(component.tone),
        )}
        title={component.detail ?? undefined}
      >
        <Dot tone={component.tone} />
        {component.status}
      </span>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="eyebrow text-ink-3">{label}</div>
      <div className="num mt-1 text-[13px] leading-none text-ink">{value}</div>
    </div>
  );
}

export function SystemHealth({
  components,
  services,
  reconciliation,
  generatedAt,
}: {
  components: HealthComponent[];
  services: ServiceUnitsPanel | null;
  reconciliation: ReconciliationPanel | null;
  generatedAt: string | null;
}) {
  const rows = healthRows(components, services);

  return (
    <Card title="System health" bodyClassName="">
      <div className="divide-y divide-line px-4 py-1.5">
        {rows.map((row) => (
          <Row key={row.key} component={row} />
        ))}
      </div>

      <div className="border-t border-line px-4 py-3.5">
        <div className="flex items-baseline justify-between gap-3">
          <h3 className="eyebrow text-ink-3">Latest reconciliation</h3>
          {reconciliation?.completed_at ? (
            <span className="num text-[11px] text-ink-3">
              {stampUtc(reconciliation.completed_at, generatedAt)} UTC ·{" "}
              {relative(reconciliation.completed_at, generatedAt)}
            </span>
          ) : null}
        </div>

        {reconciliation?.available ? (
          <>
            <div className="mt-2.5 flex items-center gap-2">
              <span
                className={cn(
                  "inline-flex items-center gap-1.5 text-[12px] leading-none font-medium",
                  "tracking-[0.06em] uppercase",
                  toneText(reconciliation.tone),
                )}
              >
                <Dot tone={reconciliation.tone} />
                {reconciliation.status}
              </span>
              <span className="text-[11.5px] text-ink-3">
                {reconciliation.safe_to_trade ? "safe to trade" : "trading not cleared"}
              </span>
            </div>
            <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3">
              <Stat label="Orders checked" value={String(reconciliation.orders_checked ?? "—")} />
              <Stat
                label="Positions checked"
                value={String(reconciliation.positions_checked ?? "—")}
              />
              <Stat label="Repairs" value={String(reconciliation.repairs ?? "—")} />
              <Stat label="Unresolved" value={String(reconciliation.unresolved ?? "—")} />
            </div>
          </>
        ) : (
          <p className="mt-2 text-[12px] leading-snug text-ink-3">
            {reconciliation?.unavailable_reason === "NOT_RECORDED"
              ? "No reconciliation pass has completed against this database. Nothing has cleared trading."
              : "Reconciliation state could not be read."}
          </p>
        )}
      </div>
    </Card>
  );
}
