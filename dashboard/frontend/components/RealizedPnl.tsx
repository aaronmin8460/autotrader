"use client";

/**
 * Realized trade P&L on screen, with the rules that make it readable.
 *
 * **Three metrics that must not look like a sum.** Daily account P&L,
 * realized trade P&L and unrealized open P&L are measured over different
 * windows, from different sources, over different scopes — the first is the
 * whole account against a UTC-day baseline, the last two are the equity book.
 * They are placed side by side because an operator wants all three, and the
 * strip says in words that they are not required to add up. There is no
 * arithmetic between them anywhere in this file.
 *
 * **Status is never optional.** The accounting verdict renders in the same
 * strip as the figures, and when it is anything but CLEAN it renders loudly. A
 * precise wrong dollar amount is worse than an admitted unknown, so a ledger
 * that has not reconciled shows its figures *and* the reason they may be
 * wrong — it does not quietly show them alone.
 *
 * `BASIS_DIVERGENCE` is the one non-CLEAN verdict that does **not** carry the
 * "these figures may be wrong" line, because it is the one verdict that says
 * they are not: quantities match the broker exactly, every fill is accounted
 * for, and the two sides differ only in which shares they consider sold. It
 * renders in its own words, and neutral rather than amber.
 *
 * **The horizon is always stated.** The "since tracking" caption comes from
 * the ledger's own metadata. Nothing here ever says "all time".
 *
 * `CLEAN`, `BASIS_DIVERGENCE`, `DEGRADED`, `MISMATCH`, `UNKNOWN`, `SELL`,
 * `EQUITY_RUNTIME` and `MANUAL_OPERATOR` are the ledger's own words and are
 * printed identically in both locales; only the labels and explanations around
 * them are translated.
 */

import { money, signTone } from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { useFormat } from "@/lib/i18n/useFormat";
import { statusTone } from "@/lib/pnl";
import type {
  AccountingStatusPanel,
  RealizedEventRow,
  RealizedPnlPanel,
  SymbolRealized,
} from "@/lib/realized";
import type { Amount } from "@/lib/types";

import {
  Card,
  DataTable,
  EmptyState,
  MetricBlock,
  Pill,
  Surface,
  Tag,
  Td,
  Th,
  Tr,
  cn,
  toneText,
} from "./ui";

/**
 * The word beside the figures. `UNKNOWN` when there is no ledger to ask, never
 * a blank and never an implied CLEAN — a missing ledger is a state an operator
 * has to see, not the absence of one.
 */
function statusWord(panel: RealizedPnlPanel | null): string {
  return panel?.status?.status ?? "UNKNOWN";
}

function useStatusDetail(): (panel: RealizedPnlPanel | null) => string {
  const { t } = useI18n();
  return (panel) => {
    if (!panel) return t("pnl.statusUnread");
    if (!panel.available) {
      return t("pnl.statusUnavailable", { reason: panel.unavailable_reason ?? "—" });
    }
    const status = panel.status;
    if (!status) return t("pnl.statusNone");
    if (status.status === "CLEAN") {
      return t("pnl.statusClean", { count: status.symbols_checked });
    }
    if (status.status === "BASIS_DIVERGENCE") {
      return `${status.message ?? t("pnl.statusDivergence")} ${t("pnl.statusDivergenceDetail", { count: status.symbols_checked })}`;
    }
    return status.message ?? t("pnl.statusDisagree");
  };
}

/** The accounting status pill. Rendered wherever a realized figure is. */
export function AccountingStatusPill({ panel }: { panel: RealizedPnlPanel | null }) {
  const { t } = useI18n();
  const detail = useStatusDetail();
  const tone = statusTone(panel && panel.available ? panel.status?.status : "UNKNOWN");
  return (
    <Pill tone={tone} emphasis={tone !== "POSITIVE"} title={detail(panel)}>
      {t("pnl.accounting")} {statusWord(panel)}
    </Pill>
  );
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
  const { t } = useI18n();
  const format = useFormat();
  const detail = useStatusDetail();
  const summary = panel?.summary ?? null;
  const status = panel?.status ?? null;
  const tone = statusTone(panel && panel.available ? status?.status : "UNKNOWN");
  const daily = dailyPnl?.available ? dailyPnl.value : null;
  const dash = <span className="text-ink-3">—</span>;

  return (
    <Card
      title={t("pnl.title")}
      meta={
        <>
          <Tag title={t("pnl.independenceNote")}>{t("pnl.threeMeasurements")}</Tag>
          <AccountingStatusPill panel={panel} />
        </>
      }
      bodyClassName=""
    >
      <div className="grid gap-x-6 gap-y-5 px-4 pb-4 sm:grid-cols-2 xl:grid-cols-4">
        <MetricBlock
          size="sm"
          label={t("pnl.dailyAccount")}
          title={t("pnl.dailyAccountHint")}
          value={daily === null ? dash : format.signedMoney(daily)}
          tone={signTone(daily)}
          context={
            dailyPnlFraction === null
              ? t("pnl.wholeAccountUtcDay")
              : `${t("pnl.wholeAccountUtcDay")} · ${format.signedPercent(dailyPnlFraction)}`
          }
        />
        <MetricBlock
          size="sm"
          label={t("pnl.realizedToday")}
          title={t("pnl.realizedTodayHint")}
          value={summary === null ? dash : format.signedMoney(summary.realized_today)}
          tone={summary ? signTone(summary.realized_today) : undefined}
          context={
            summary
              ? `${t("pnl.equityBook")} · ${summary.event_count_today} ${t(
                  summary.event_count_today === 1 ? "pnl.event" : "pnl.events",
                )} · ${summary.utc_day} UTC`
              : t("pnl.ledgerUnavailable")
          }
        />
        <MetricBlock
          size="sm"
          label={t("pnl.unrealizedOpen")}
          title={t("pnl.unrealizedOpenHint")}
          value={unrealized === null ? dash : format.signedMoney(unrealized)}
          tone={signTone(unrealized)}
          context={`${t("pnl.equityBook")} · ${t("pnl.brokerMarks")}`}
        />
        <MetricBlock
          size="sm"
          label={t("pnl.realizedSince")}
          title={t("pnl.realizedSinceHint")}
          value={summary === null ? dash : format.signedMoney(summary.realized_since_tracking)}
          tone={summary ? signTone(summary.realized_since_tracking) : undefined}
          context={
            <span className={cn(tone !== "POSITIVE" && toneText(tone))}>
              {status?.tracking_label ?? t("pnl.notYetTracked")}
            </span>
          }
        />
      </div>

      {statusWord(panel) !== "CLEAN" ? (
        <div className="border-t border-subtle px-4 py-3">
          <p className={cn("text-meta leading-snug", toneText(tone))}>
            {detail(panel)}{" "}
            {panel?.available && statusWord(panel) !== "BASIS_DIVERGENCE"
              ? t("pnl.mayBeWrong", { status: statusWord(panel) })
              : ""}
          </p>
        </div>
      ) : null}

      <div className="border-t border-subtle px-4 py-3">
        <p className="max-w-[110ch] text-meta leading-relaxed text-ink-3">
          {t("pnl.independenceNote")}
          {status?.last_sync_at
            ? ` ${t("pnl.lastSynced", { at: `${format.stamp(status.last_sync_at, generatedAt)} UTC` })}`
            : ""}
        </p>
      </div>
    </Card>
  );
}

/**
 * The same four figures without their own surface, for a page that already
 * groups them under a heading.
 */
export function RealizedSummaryRow({
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
  return (
    <Surface className="overflow-hidden">
      <RealizedStrip
        panel={panel}
        dailyPnl={dailyPnl}
        dailyPnlFraction={dailyPnlFraction}
        unrealized={unrealized}
        generatedAt={generatedAt}
      />
    </Surface>
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
  const { t } = useI18n();
  const format = useFormat();

  if (events.length === 0) {
    return (
      <EmptyState
        headline={t("pnl.noRealizedEvents")}
        detail={t("pnl.noRealizedEventsDetail")}
      />
    );
  }

  return (
    <>
      <DataTable
        caption={t("pnl.col.realized")}
        minWidth="min-w-[740px]"
        sticky={false}
        head={
          <>
            <Th>{t("orders.col.time")}</Th>
            <Th>{t("orders.col.side")}</Th>
            <Th align="right">{t("orders.col.quantity")}</Th>
            <Th align="right" title={t("pnl.col.fillPriceHint")}>
              {t("pnl.col.fillPrice")}
            </Th>
            <Th align="right" title={t("pnl.col.costBasisHint")}>
              {t("pnl.col.costBasis")}
            </Th>
            <Th align="right" title={t("pnl.col.realizedHint")}>
              {t("pnl.col.realized")}
            </Th>
            <Th title={t("pnl.col.provenanceHint")}>{t("common.source")}</Th>
          </>
        }
      >
        {events.map((event) => (
          <Tr key={event.event_id}>
            <Td className="num" title={format.stampFull(event.realized_at)}>
              {format.stamp(event.realized_at, generatedAt)}
            </Td>
            <Td>
              <Pill tone="MUTED">SELL</Pill>
            </Td>
            <Td align="right" className="num">
              {event.quantity}
            </Td>
            <Td align="right" className="num">
              {money(event.execution_price)}
            </Td>
            <Td align="right" className="num">
              {money(event.average_cost_before)}
            </Td>
            <Td
              align="right"
              className={cn("num", toneText(signTone(event.net_realized_pnl)))}
              title={`exact: ${event.net_realized_pnl_exact}`}
            >
              {format.signedMoney(event.net_realized_pnl)}
            </Td>
            <Td className="text-meta tracking-[0.04em] text-ink-3">
              {event.provenance.replace("_", " ")}
            </Td>
          </Tr>
        ))}
      </DataTable>
      {realized ? (
        <p className="mt-2 text-meta leading-snug text-ink-3">
          {t("pnl.eventsSince", {
            count: realized.event_count,
            total: format.signedMoney(realized.realized_since_tracking),
          })}
          {status && status.status !== "CLEAN"
            ? ` ${t("pnl.accountingStatusIs", { status: status.status })}`
            : ""}
        </p>
      ) : null}
    </>
  );
}
