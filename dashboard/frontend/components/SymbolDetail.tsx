"use client";

/**
 * The symbol detail drawer: what the account holds in one symbol, what the
 * paper runtime wants there, and a larger price chart.
 *
 * Every figure has one source and the drawer names it. Price, quantity,
 * market value, average entry and unrealized P&L are the broker's; weight is
 * the broker's market value over the broker's equity; stance and target are
 * the paper runtime's own recorded decision. The chart is provider bars from
 * the chart layer, with the broker's average entry drawn across it and the
 * book's real EQUITY PAPER fills marked on it - and nothing else.
 */

import { useState } from "react";

import { CHART_RANGES, useChartBatch, type ChartRange } from "@/lib/charts";
import { fillsFor, fillsWithin } from "@/lib/fills";
import { money, percent, quantity, signTone, signedMoney, signedPercent, stampUtc } from "@/lib/format";
import type { AccountOrdersPanel } from "@/lib/orders";
import type { TargetVsActualRow } from "@/lib/portfolio";
import { realizedForOrder, statusTone } from "@/lib/pnl";
import { useSymbolRealized } from "@/lib/realized";
import type { PositionRow } from "@/lib/types";

import { LineChart } from "./charts/LineChart";
import { RealizedEvents } from "./RealizedPnl";
import { Drawer, Field, Pill, RangeSelector, Tag, cn, toneText } from "./ui";

export function SymbolDetail({
  symbol,
  onClose,
  position,
  equity,
  target,
  orders,
  generatedAt,
}: {
  symbol: string | null;
  onClose: () => void;
  position: PositionRow | null;
  equity: number | null;
  target: TargetVsActualRow | null;
  orders: AccountOrdersPanel | null;
  generatedAt: string | null;
}) {
  const [range, setRange] = useState<ChartRange>("1D");
  const symbols = symbol ? [symbol] : [];
  const { series } = useChartBatch(symbols, range);
  const current = symbol ? series[symbol] : undefined;
  const { data: realized } = useSymbolRealized(symbol);

  if (!symbol) return null;

  const weight = position && equity && position.market_value !== null ? position.market_value / equity : null;
  const fills = fillsWithin(fillsFor(orders, symbol), current?.first_at ?? null, current?.last_at ?? null);
  const pnlTone = signTone(position?.unrealized_pnl);
  const crypto = position?.asset_class === "CRYPTO";
  const events = realized?.events ?? [];
  const accounting = realized?.status ?? null;

  return (
    <Drawer
      open={symbol !== null}
      onClose={onClose}
      title={symbol}
      meta={
        <>
          {position ? <Tag tone={crypto ? "ATTENTION" : undefined}>{position.asset_class}</Tag> : null}
          <Tag title="Broker paper account. No real money.">Paper</Tag>
          {accounting ? (
            <Pill tone={statusTone(accounting.status)} title={accounting.message ?? accounting.tracking_label}>
              Accounting {accounting.status}
            </Pill>
          ) : null}
        </>
      }
    >
      <div className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3 lg:grid-cols-4">
        <Field label="Current price" title="Implied by the broker's own market value and quantity.">
          <span className="num">{money(position?.price)}</span>
        </Field>
        <Field label="Quantity">
          <span className="num">{quantity(position?.quantity)}</span>
        </Field>
        <Field label="Market value">
          <span className="num">{money(position?.market_value)}</span>
        </Field>
        <Field label="Portfolio weight" title="Broker market value over broker equity, same read.">
          <span className="num">{percent(weight, 2)}</span>
        </Field>
        <Field label="Average entry" title="The broker's average entry price for the open position.">
          <span className="num">{money(position?.average_entry_price)}</span>
        </Field>
        <Field
          label="Accounting cost"
          title="The ledger's own weighted-average cost, derived from broker-confirmed executions. It is shown beside the broker's figure rather than instead of it: when the two disagree, the accounting status says so and the broker is the one that is right."
        >
          {realized?.realized?.average_cost === null || realized?.realized?.average_cost === undefined ? (
            <span className="text-ink-3">—</span>
          ) : (
            <span className="num">{money(realized.realized.average_cost)}</span>
          )}
        </Field>
        <Field label="Realized today" title="What confirmed sales in this symbol released today. A purchase realizes nothing.">
          {realized?.realized ? (
            <span className={cn("num", toneText(signTone(realized.realized.realized_today)))}>
              {signedMoney(realized.realized.realized_today)}
            </span>
          ) : (
            <span className="text-ink-3">—</span>
          )}
        </Field>
        <Field label="Realized since tracking" title={accounting?.tracking_label ?? "The ledger's tracking horizon."}>
          {realized?.realized ? (
            <span className={cn("num", toneText(signTone(realized.realized.realized_since_tracking)))}>
              {signedMoney(realized.realized.realized_since_tracking)}
            </span>
          ) : (
            <span className="text-ink-3">—</span>
          )}
        </Field>
        <Field label="Unrealized P&L">
          {position?.unrealized_pnl === null || position?.unrealized_pnl === undefined ? (
            <span className="text-ink-3">—</span>
          ) : (
            <span className={cn("num", toneText(pnlTone))}>
              {signedMoney(position.unrealized_pnl)}
              <span className="ml-1.5 text-[11px] text-ink-3">{signedPercent(position.unrealized_pnl_fraction)}</span>
            </span>
          )}
        </Field>
        <Field label="Current stance" title="EDA-1's recorded stance on the latest completed bar.">
          {target?.stance ? (
            <Pill tone={target.stance === "LONG" ? "POSITIVE" : "MUTED"}>{target.stance}</Pill>
          ) : (
            <span className="text-ink-3">{crypto ? "Crypto book" : "N/A"}</span>
          )}
        </Field>
        <Field label="Target vs actual weight" title="Target: the paper runtime's newest recorded decision. Actual: broker weight now.">
          <span className="num">
            {target && target.target_weight !== null ? percent(target.target_weight, 2) : "N/A"}
            <span className="text-ink-3"> / </span>
            {percent(target?.actual_weight ?? weight, 2)}
          </span>
        </Field>
      </div>

      <div className="mt-5 flex items-center justify-between gap-3">
        <h3 className="text-[12.5px] font-semibold text-ink">Price</h3>
        <RangeSelector options={CHART_RANGES} value={range} onChange={setRange} label="Chart range" />
      </div>
      <div className="mt-2">
        <LineChart
          series={current}
          entryPrice={position?.average_entry_price ?? null}
          markers={fills.map((fill) => {
            // A SELL marker carries what that order realized, when the ledger
            // holds an event for it. A BUY never does - a purchase releases
            // nothing, and labelling one with a P&L figure would be a claim
            // about money that did not move.
            const released = realizedForOrder(events, fill.orderId, fill.side);
            return {
              at: fill.at,
              side: fill.side,
              price: fill.price,
              label: released === null ? fill.label : `${fill.label} · Realized ${signedMoney(released)}`,
            };
          })}
        />
      </div>

      {target ? (
        <dl className="mt-4 grid grid-cols-2 gap-x-5 gap-y-3 border-t border-line pt-4 sm:grid-cols-4">
          <Field label="Action (latest bar)" title="The decided side on the latest bar, or HOLD when no order was decided.">
            <span className="text-[12px] font-medium tracking-[0.06em] uppercase">{target.action}</span>
          </Field>
          <Field label="Last decision">
            <span className="num">{target.last_decision_at ? stampUtc(target.last_decision_at, generatedAt) : "—"}</span>
          </Field>
          <Field label="Target value">
            <span className="num">{money(target.target_value)}</span>
          </Field>
          <Field label="Delta vs target">
            <span className={cn("num", toneText(signTone(target.delta_value)))}>{signedMoney(target.delta_value)}</span>
          </Field>
        </dl>
      ) : null}

      <div className="mt-5">
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-[12.5px] font-semibold text-ink">Realized events</h3>
          <span className="text-[11px] text-ink-3">{accounting?.tracking_label ?? ""}</span>
        </div>
        <div className="mt-2">
          <RealizedEvents
            events={events}
            realized={realized?.realized ?? null}
            status={accounting}
            generatedAt={generatedAt}
          />
        </div>
      </div>

      <p className="mt-4 text-[11px] leading-snug text-ink-3">
        Read-only. Nothing on this panel can place, cancel or modify an order.
      </p>
    </Drawer>
  );
}
