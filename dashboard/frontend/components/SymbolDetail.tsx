"use client";

/**
 * The symbol detail drawer: what the account holds in one symbol, what the
 * paper runtime wants there, and a larger price chart.
 *
 * Every figure has one source and the drawer names it. Price, quantity, market
 * value, average entry and unrealized P&L are the broker's; weight is the
 * broker's market value over the broker's equity; stance and target are the
 * paper runtime's own recorded decision. The chart is provider bars from the
 * chart layer, with the broker's average entry drawn across it and the book's
 * real EQUITY PAPER fills marked on it — and nothing else.
 *
 * `LONG`, `FLAT`, `BUY`, `SELL`, `HOLD` and the symbol are authoritative and
 * are printed identically in both locales; in Korean a gloss appears beside a
 * stance rather than in place of it.
 */

import { useState } from "react";

import { CHART_RANGES, useChartBatch, type ChartRange } from "@/lib/charts";
import { fillsFor, fillsWithin } from "@/lib/fills";
import { money, percent, signTone } from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { useFormat } from "@/lib/i18n/useFormat";
import type { AccountOrdersPanel } from "@/lib/orders";
import type { TargetVsActualRow } from "@/lib/portfolio";
import { statusTone } from "@/lib/pnl";
import { useSymbolRealized } from "@/lib/realized";
import type { PositionRow } from "@/lib/types";

import { LineChart } from "./charts/LineChart";
import { RealizedEvents } from "./RealizedPnl";
import { Drawer, Field, Pill, SegmentedTimeRange, Tag, cn, toneText } from "./ui";

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
  const { t, gloss } = useI18n();
  const format = useFormat();
  const [range, setRange] = useState<ChartRange>("1D");
  const symbols = symbol ? [symbol] : [];
  const { series } = useChartBatch(symbols, range);
  const current = symbol ? series[symbol] : undefined;
  const { data: realized } = useSymbolRealized(symbol);

  if (!symbol) return null;

  const weight =
    position && equity && position.market_value !== null ? position.market_value / equity : null;
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
          <Tag title={t("strategies.noRealMoneyHint")}>Paper</Tag>
          {accounting ? (
            <Pill
              tone={statusTone(accounting.status)}
              emphasis={accounting.status !== "CLEAN"}
              title={accounting.message ?? accounting.tracking_label}
            >
              {t("pnl.accounting")} {accounting.status}
            </Pill>
          ) : null}
        </>
      }
    >
      <div className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3 lg:grid-cols-4">
        <Field label={t("drawer.currentPrice")} title={t("drawer.currentPriceHint")}>
          <span className="num">{money(position?.price)}</span>
        </Field>
        <Field label={t("positions.col.quantity")}>
          <span className="num">{format.quantity(position?.quantity)}</span>
        </Field>
        <Field label={t("drawer.marketValue")}>
          <span className="num">{money(position?.market_value)}</span>
        </Field>
        <Field label={t("drawer.portfolioWeight")} title={t("drawer.portfolioWeightHint")}>
          <span className="num">{percent(weight, 2)}</span>
        </Field>
        <Field label={t("positions.col.avgEntry")} title={t("drawer.avgEntryHint")}>
          <span className="num">{money(position?.average_entry_price)}</span>
        </Field>
        <Field label={t("pnl.accountingCost")} title={t("pnl.accountingCostHint")}>
          {realized?.realized?.average_cost === null || realized?.realized?.average_cost === undefined ? (
            <span className="text-ink-3">—</span>
          ) : (
            <span className="num">{money(realized.realized.average_cost)}</span>
          )}
        </Field>
        <Field label={t("pnl.realizedToday")} title={t("pnl.symbolRealizedTodayHint")}>
          {realized?.realized ? (
            <span className={cn("num", toneText(signTone(realized.realized.realized_today)))}>
              {format.signedMoney(realized.realized.realized_today)}
            </span>
          ) : (
            <span className="text-ink-3">—</span>
          )}
        </Field>
        <Field
          label={t("pnl.realizedSince")}
          title={accounting?.tracking_label ?? t("pnl.trackingHorizon")}
        >
          {realized?.realized ? (
            <span className={cn("num", toneText(signTone(realized.realized.realized_since_tracking)))}>
              {format.signedMoney(realized.realized.realized_since_tracking)}
            </span>
          ) : (
            <span className="text-ink-3">—</span>
          )}
        </Field>
        <Field label={t("pnl.unrealized")}>
          {position?.unrealized_pnl === null || position?.unrealized_pnl === undefined ? (
            <span className="text-ink-3">—</span>
          ) : (
            <span className={cn("num", toneText(pnlTone))}>
              {format.signedMoney(position.unrealized_pnl)}
              <span className="ms-1.5 text-meta text-ink-3">
                {format.signedPercent(position.unrealized_pnl_fraction)}
              </span>
            </span>
          )}
        </Field>
        <Field label={t("drawer.currentStance")} title={t("drawer.currentStanceHint")}>
          {target?.stance ? (
            <span className="inline-flex items-center gap-2">
              <Pill tone={target.stance === "LONG" ? "POSITIVE" : "MUTED"}>{target.stance}</Pill>
              {gloss(target.stance) ? (
                <span className="text-meta text-ink-3">{gloss(target.stance)}</span>
              ) : null}
            </span>
          ) : (
            <span className="text-ink-3">{crypto ? t("drawer.cryptoBook") : "N/A"}</span>
          )}
        </Field>
        <Field label={t("drawer.targetVsActualWeight")} title={t("drawer.targetVsActualWeightHint")}>
          <span className="num">
            {target && target.target_weight !== null ? percent(target.target_weight, 2) : "N/A"}
            <span className="text-ink-3"> / </span>
            {percent(target?.actual_weight ?? weight, 2)}
          </span>
        </Field>
      </div>

      <div className="mt-6 flex items-center justify-between gap-3">
        <h3 className="text-table font-semibold text-ink">{t("drawer.price")}</h3>
        <SegmentedTimeRange
          options={CHART_RANGES}
          value={range}
          onChange={setRange}
          label={t("chart.range")}
        />
      </div>
      <div className="mt-2">
        <LineChart
          series={current}
          entryPrice={position?.average_entry_price ?? null}
          markers={fills.map((fill) => ({
            at: fill.at,
            side: fill.side,
            price: fill.price,
            label: fill.label,
          }))}
        />
      </div>

      {target ? (
        <dl className="mt-5 grid grid-cols-2 gap-x-5 gap-y-3 border-t border-subtle pt-4 sm:grid-cols-4">
          <Field label={t("drawer.action")} title={t("drawer.actionHint")}>
            <span className="text-table font-medium tracking-[0.06em] uppercase">{target.action}</span>
          </Field>
          <Field label={t("drawer.lastDecision")}>
            <span className="num">
              {target.last_decision_at ? format.stamp(target.last_decision_at, generatedAt) : "—"}
            </span>
          </Field>
          <Field label={t("drawer.targetValue")}>
            <span className="num">{money(target.target_value)}</span>
          </Field>
          <Field label={t("drawer.deltaVsTarget")}>
            <span className={cn("num", toneText(signTone(target.delta_value)))}>
              {format.signedMoney(target.delta_value)}
            </span>
          </Field>
        </dl>
      ) : null}

      <div className="mt-6 border-t border-subtle pt-4">
        <h3 className="text-table font-semibold text-ink">{t("pnl.col.realized")}</h3>
        <div className="mt-2">
          <RealizedEvents
            events={events}
            realized={realized?.realized ?? null}
            status={accounting}
            generatedAt={generatedAt}
          />
        </div>
      </div>

      <p className="mt-5 text-meta leading-snug text-ink-3">{t("drawer.readOnly")}</p>
    </Drawer>
  );
}
