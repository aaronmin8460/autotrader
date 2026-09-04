"use client";

/**
 * Realized trade P&L on screen, with the rules that make it readable.
 *
 * **Three metrics that must not look like a sum.** Daily account P&L,
 * realized trade P&L and unrealized open P&L are measured over different
 * windows, from different sources, over different scopes - the first is the
 * whole account against a UTC-day baseline, the last two are the equity book.
 * They are placed side by side because an operator wants all three, and the
 * strip says in words that they are not required to add up. There is no
 * arithmetic between them anywhere in this file.
 *
 * **Status is never optional.** The accounting verdict renders in the same
 * strip as the figures, and when it is anything but CLEAN it renders loudly.
 * A precise wrong dollar amount is worse than an admitted unknown, so a
 * ledger that has not reconciled shows its figures *and* the reason they may
 * be wrong - it does not quietly show them alone.
 *
 * **The horizon is always stated.** "REALIZED SINCE <timestamp>" comes from
 * the ledger's own metadata. Nothing here ever says "all time".
 */

import { money, signedMoney, signTone, stampUtc } from "@/lib/format";
import { statusTone } from "@/lib/pnl";
import type { AccountingStatusPanel, RealizedEventRow, RealizedPnlPanel, SymbolRealized } from "@/lib/realized";
import type { Amount } from "@/lib/types";

import { Card, Empty, Metric, Pill, Tag, Td, Th, cn, toneText } from "./ui";

const INDEPENDENCE_NOTE =
  "Three different measurements, not three parts of one. Daily account P&L is account equity against the stored UTC-day baseline and covers the whole account, crypto included. Realized is what confirmed equity sales released. Unrealized is the broker's figure on open equity positions. They are not required to sum: overnight marks, fees, dividends and other account activity all sit between them.";

function statusDetail(panel: RealizedPnlPanel | null): string {
  if (!panel) return "The accounting ledger has not been read yet.";
  if (!panel.available) {
    return `The accounting ledger could not be read (${panel.unavailable_reason ?? "unknown reason"}), so there are no realized figures to show. This is not a value of zero.`;
  }
  const status = panel.status;
  if (!status) return "The accounting ledger reported no status.";
  if (status.status === "CLEAN") {
    return `Ledger quantities match the broker on all ${status.symbols_checked} symbols.`;
  }
  if (status.status === "BASIS_DIVERGENCE") {
    return (
      status.message
        ? `${status.message}. Quantities match the broker exactly on all ${status.symbols_checked} symbols and every fill is accounted for; the two sides relieve sold lots differently, so the same P&L is recognised on different days.`
        : "The ledger and the broker hold the same shares at the same prices and relieve sold lots differently."
    );
  }
  return status.message ?? "The ledger and the broker do not agree.";
}

/**
 * The word beside the figures. `UNKNOWN` when there is no ledger to ask,
 * never a blank and never an implied CLEAN - a missing ledger is a state an
 * operator has to see, not the absence of one.
 */
function statusWord(panel: RealizedPnlPanel | null): string {
  return panel?.status?.status ?? "UNKNOWN";
}

/** The four-metric accounting strip. */
export function RealizedStrip({
  panel,
  dailyPnl,
  dailyPnlFraction,
  unrealized,
  generatedAt,
}: {
  panel: RealizedPnlPanel | null;
  dailyPnl: Amount | null;
  dailyPnlFraction: number | null;
  unrealized: number | null;
  generatedAt: string | null;
}) {
  const summary = panel?.summary ?? null;
  const status = panel?.status ?? null;
  const tone = statusTone(panel && panel.available ? status?.status : "UNKNOWN");
  const daily = dailyPnl?.available ? dailyPnl.value : null;

  return (
    <Card
      title="Profit and loss"
      meta={
        <>
          <Tag title={INDEPENDENCE_NOTE}>Three separate measurements</Tag>
          <Pill tone={tone} emphasis={tone !== "POSITIVE"} title={statusDetail(panel)}>
            Accounting {statusWord(panel)}
          </Pill>
        </>
      }
      bodyClassName=""
    >
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric
          label="Daily account P&L"
          title="Account equity now, less the equity baseline stored for this UTC day. Whole account, crypto included. This is the figure the risk engine's daily-loss halt is measured on."
          value={daily === null ? <span className="text-ink-3">—</span> : signedMoney(daily)}
          tone={signTone(daily)}
          context={dailyPnlFraction === null ? "Whole account · UTC day" : `Whole account · UTC day · ${(dailyPnlFraction * 100).toFixed(2)}%`}
        />
        <Metric
          label="Realized today"
          title="What confirmed equity sales released today, from a ledger built out of broker-confirmed executions under weighted-average cost. Not part of the daily figure beside it."
          value={
            summary === null ? (
              <span className="text-ink-3">—</span>
            ) : (
              signedMoney(summary.realized_today)
            )
          }
          tone={summary ? signTone(summary.realized_today) : undefined}
          context={
            summary
              ? `Equity book · ${summary.event_count_today} realized ${summary.event_count_today === 1 ? "event" : "events"} · ${summary.utc_day} UTC`
              : "Ledger unavailable"
          }
        />
        <Metric
          label="Unrealized open"
          title="The broker's own unrealized P&L over open equity positions. Marks, not sales."
          value={unrealized === null ? <span className="text-ink-3">—</span> : signedMoney(unrealized)}
          tone={signTone(unrealized)}
          context="Equity book · broker marks"
        />
        <Metric
          label="Realized since tracking"
          title="Everything the ledger has recorded since its tracking horizon. The horizon is stated below; it is never 'all time' unless the whole history was proven."
          value={
            summary === null ? (
              <span className="text-ink-3">—</span>
            ) : (
              signedMoney(summary.realized_since_tracking)
            )
          }
          tone={summary ? signTone(summary.realized_since_tracking) : undefined}
          context={
            <span className={cn(tone !== "POSITIVE" && toneText(tone))}>
              {status?.tracking_label ?? "Not yet tracked"}
            </span>
          }
        />
      </div>

      {statusWord(panel) !== "CLEAN" ? (
        <p className={cn("mt-3 text-[11.5px] leading-snug", toneText(tone))}>
          {statusDetail(panel)}{" "}
          {panel?.available
            ? `Realized figures on this page may be wrong while this says ${statusWord(panel)}.`
            : ""}
        </p>
      ) : null}

      <p className="mt-3 text-[11px] leading-snug text-ink-3">
        {INDEPENDENCE_NOTE}
        {status?.last_sync_at ? ` Ledger last synchronized ${stampUtc(status.last_sync_at, generatedAt)}.` : ""}
      </p>
    </Card>
  );
}

/** The realized-event table shown inside a symbol's drawer. */
export function RealizedEvents({
  events,
  realized,
  status,
  generatedAt,
}: {
  events: RealizedEventRow[];
  realized: SymbolRealized | null;
  status: AccountingStatusPanel | null;
  generatedAt: string | null;
}) {
  if (events.length === 0) {
    return (
      <Empty
        headline="No realized events"
        detail="Nothing has been sold in this symbol since the ledger's tracking horizon. A purchase realizes nothing."
      />
    );
  }
  return (
    <>
      <div className="scroll-x">
        <table className="w-full min-w-[720px] border-collapse">
          <thead>
            <tr className="border-b border-line">
              <Th>Time</Th>
              <Th>Side</Th>
              <Th align="right">Qty</Th>
              <Th align="right" title="The price this execution filled at.">Fill price</Th>
              <Th align="right" title="Weighted-average cost of the shares this sale released.">Cost basis</Th>
              <Th align="right" title="Proceeds less released cost basis, less any attributable fees.">Realized P&L</Th>
              <Th title="EQUITY RUNTIME: the runtime's own store holds this order. MANUAL OPERATOR: broker-confirmed, minted by this system's tooling, run by hand.">Source</Th>
            </tr>
          </thead>
          <tbody>
            {events.map((event) => (
              <tr key={event.event_id} className="border-b border-line/70 last:border-0">
                <Td className="num">{stampUtc(event.realized_at, generatedAt)}</Td>
                <Td>
                  <Pill tone="MUTED">SELL</Pill>
                </Td>
                <Td align="right" className="num">{event.quantity}</Td>
                <Td align="right" className="num">{money(event.execution_price)}</Td>
                <Td align="right" className="num">{money(event.average_cost_before)}</Td>
                <Td align="right" className={cn("num", toneText(signTone(event.net_realized_pnl)))} title={`Exact: ${event.net_realized_pnl_exact}`}>
                  {signedMoney(event.net_realized_pnl)}
                </Td>
                <Td className="text-[11px] tracking-[0.04em]">{event.provenance.replace("_", " ")}</Td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {realized ? (
        <p className="mt-2 text-[11px] leading-snug text-ink-3">
          {realized.event_count} realized {realized.event_count === 1 ? "event" : "events"} since tracking
          started, {signedMoney(realized.realized_since_tracking)} in total. Rows are rounded to cents; the
          total is summed exactly first, so the two can differ by a cent.
          {status && status.status !== "CLEAN" ? ` Accounting status is ${status.status}.` : ""}
        </p>
      ) : null}
    </>
  );
}
