"use client";

/**
 * Sections 3, 4 and 5: performance, external flows, and the high-water mark.
 *
 * The performance card exists to separate two things a single equity number
 * confuses. **Broker account value** is what the account holds; **trading
 * performance** is what trading did. Moving money changes the first and never
 * the second, and the card draws them as two groups with the rule written
 * between them rather than leaving a reader to infer it.
 *
 * The worked case this is designed for: a $50 deposit takes broker equity from
 * $50 to $100 while flow-adjusted equity stays at $50 and trading P&L stays at
 * $0. If those two columns ever move together on a transfer, the bug is
 * visible on this card.
 *
 * **Nothing here subtracts.** `flow_adjusted_equity`,
 * `trading_pnl_since_inception` and `current_drawdown_from_adjusted_hwm` are
 * read from the payload. The formulas are printed in the contract and
 * implemented in the backend; recomputing them in the browser would create a
 * second answer that could disagree with the first.
 */

import { useT } from "@/lib/i18n";
import { stampUtc } from "@/lib/format";
import type { LiveAccountingSummary } from "@/lib/live-contract";
import { Card, Field, MetricBlock, SectionHeader, Tag } from "../ui";
import { Identifier, LiveTag, Money, Note, Percent, SignedMoney, Unknown } from "./atoms";

/** Section 3 — broker value beside trading performance, never merged. */
export function LivePerformanceCard({
  summary,
  suppressed,
}: {
  summary: LiveAccountingSummary | null;
  suppressed: boolean;
}) {
  const t = useT();
  const bounded = summary && summary.twr_bounded_subperiods > 0;

  return (
    <Card title={t("live.performance")} meta={<LiveTag />}>
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        {/* Left: what the account holds. */}
        <div className="min-w-0">
          <SectionHeader title={t("live.performance.brokerValue")} level={3} className="mb-3" />
          <div className="grid grid-cols-2 gap-x-5 gap-y-4">
            <MetricBlock
              label={t("live.account.brokerEquity")}
              value={<Money value={summary?.current_broker_equity} />}
              size="sm"
            />
            <MetricBlock
              label={t("live.account.cash")}
              value={<Money value={summary?.current_cash} />}
              size="sm"
              title={t("live.account.cashSettlement")}
            />
          </div>
        </div>

        {/* Right: what trading did. The two never share a group. */}
        <div className="min-w-0 lg:border-s lg:border-subtle lg:ps-5">
          <SectionHeader
            title={t("live.performance.tradingPerformance")}
            level={3}
            className="mb-3"
          />
          <div className="grid grid-cols-2 gap-x-5 gap-y-4">
            <MetricBlock
              label={t("live.performance.flowAdjustedEquity")}
              value={<Money value={suppressed ? null : summary?.flow_adjusted_equity} />}
              size="sm"
            />
            <MetricBlock
              label={t("live.performance.tradingPnl")}
              value={<SignedMoney value={suppressed ? null : summary?.trading_pnl_since_inception} />}
              size="sm"
            />
            <Field label={t("live.performance.realizedPnl")}>
              <SignedMoney value={suppressed ? null : summary?.realized_pnl} />
            </Field>
            <Field label={t("live.performance.unrealizedPnl")}>
              <SignedMoney value={suppressed ? null : summary?.unrealized_pnl} />
            </Field>
            <Field label={t("live.performance.twr")}>
              <Percent value={suppressed ? null : summary?.time_weighted_return} signed />
            </Field>
            <Field label={t("live.performance.basisStatus")}>
              <Identifier value={summary?.realized_pnl_basis_status} />
            </Field>
          </div>
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Tag>{summary?.twr_convention ?? "—"}</Tag>
        <span className="text-meta text-ink-3">
          {summary === null
            ? null
            : bounded
              ? t("live.performance.twrBoundedHint", {
                  bounded: summary.twr_bounded_subperiods,
                  total: summary.twr_subperiods,
                })
              : t("live.performance.twrExact")}
        </span>
      </div>

      <Note className="mt-3">{t("live.performance.separation")}</Note>
      <Note className="mt-2">{t("live.performance.flowRule")}</Note>
    </Card>
  );
}

/** Section 4 — what moved in and out, and what has not been counted. */
export function LiveFlowsCard({ summary }: { summary: LiveAccountingSummary | null }) {
  const t = useT();
  const pending = summary?.pending_flows_excluded_count ?? null;
  const unknown = summary?.unknown_external_flow_count ?? null;

  return (
    <Card title={t("live.flows")} meta={<LiveTag />}>
      <div className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3 xl:grid-cols-4">
        <Field label={t("live.flows.baseline")} title={t("live.flows.baselineHint")}>
          <Money value={summary?.baseline_equity} />
        </Field>
        <Field label={t("live.flows.deposits")}>
          <Money value={summary?.confirmed_deposits} />
        </Field>
        <Field label={t("live.flows.withdrawals")}>
          <Money value={summary?.confirmed_withdrawals} />
        </Field>
        <Field label={t("live.flows.net")}>
          <SignedMoney value={summary?.net_external_flows} />
        </Field>

        <Field label={t("live.flows.inception")}>
          <span className="num">
            {summary?.accounting_inception_at
              ? stampUtc(summary.accounting_inception_at, summary.generated_at)
              : "—"}
          </span>
        </Field>
        <Field label={t("live.flows.last")}>
          <span className="num">
            {summary?.last_external_flow_at
              ? stampUtc(summary.last_external_flow_at, summary.generated_at)
              : "—"}
          </span>
        </Field>
        <Field label={t("live.flows.pending")} title={t("live.flows.pendingHint")}>
          {pending === null ? <Unknown /> : <span className="num">{pending}</span>}
        </Field>
        <Field label={t("live.flows.unknown")} title={t("live.flows.unknownHint")}>
          {unknown === null ? (
            <Unknown />
          ) : (
            <span className={unknown > 0 ? "num text-warn" : "num"}>{unknown}</span>
          )}
        </Field>
      </div>

      <Note className="mt-4">{t("live.flows.baselineHint")}</Note>
      {pending !== null && pending > 0 ? (
        <Note className="mt-2">{t("live.flows.pendingHint")}</Note>
      ) : null}
    </Card>
  );
}

/** Section 5 — the high-water mark, and how far below it the account sits. */
export function LiveHwmCard({
  summary,
  suppressed,
}: {
  summary: LiveAccountingSummary | null;
  suppressed: boolean;
}) {
  const t = useT();
  const hide = suppressed;

  return (
    <Card title={t("live.hwm")} meta={<LiveTag />}>
      <div className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-4">
        <MetricBlock
          label={t("live.hwm.adjusted")}
          value={<Money value={hide ? null : summary?.adjusted_equity_hwm} />}
          size="sm"
        />
        <MetricBlock
          label={t("live.hwm.current")}
          value={<Money value={hide ? null : summary?.flow_adjusted_equity} />}
          size="sm"
        />
        <MetricBlock
          label={t("live.hwm.drawdown")}
          value={
            <Percent value={hide ? null : summary?.current_drawdown_from_adjusted_hwm} signed />
          }
          size="sm"
        />
        <MetricBlock
          label={t("live.hwm.newProfit")}
          value={<SignedMoney value={hide ? null : summary?.new_hwm_profit} />}
          size="sm"
        />

        <Field label={t("live.hwm.at")}>
          <span className="num">
            {summary?.adjusted_equity_hwm_at && !hide
              ? stampUtc(summary.adjusted_equity_hwm_at, summary.generated_at)
              : "—"}
          </span>
        </Field>
        <Field label={t("live.hwm.intradayPeak")} title={t("live.hwm.intradayHint")}>
          <Money value={hide ? null : summary?.observed_intraday_peak} />
        </Field>
        <Field label={t("live.ops.lastCompletedDay")} className="sm:col-span-2">
          <span className="num">
            {summary?.last_completed_day_checkpoint_at
              ? stampUtc(summary.last_completed_day_checkpoint_at, summary.generated_at)
              : "—"}
          </span>
        </Field>
      </div>

      <Note className="mt-4">{t("live.hwm.completedDayOnly")}</Note>
      <Note className="mt-2">{t("live.hwm.intradayHint")}</Note>
    </Card>
  );
}
