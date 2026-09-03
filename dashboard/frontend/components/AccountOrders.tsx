"use client";

/**
 * Recent orders across the whole account.
 *
 * Merged from the crypto store and the equity paper store by the paper API,
 * sorted by the broker's own submission time, and labelled per row with the
 * store the order came from. An intent the broker never answered for still has
 * a row — it is the row that matters most — and `UNKNOWN` is the only status
 * given extra visual weight.
 *
 * ORDER STATUS IS NEVER SOFTENED. `PENDING_NEW`, `NEW` and `ACCEPTED` mean the
 * order exists and has not settled; they render in their own neutral/amber
 * treatment and never in the colour a fill gets. The words themselves are the
 * broker's and are identical in both locales.
 *
 * Nothing simulated can appear here: the read model takes no shadow record as
 * input and says so on the payload, and this table renders that statement.
 * There are no action controls, and no endpoint behind one either.
 */

import { useI18n } from "@/lib/i18n";
import { useFormat } from "@/lib/i18n/useFormat";
import { money } from "@/lib/format";
import type { AccountOrdersPanel, OrderSource } from "@/lib/orders";

import { Card, DataTable, EmptyState, ErrorState, Pill, TableSkeleton, Tag, Td, Th, Tr, cn } from "./ui";

const SOURCE_HINT: Record<OrderSource, string> = {
  "CRYPTO PAPER": "Recorded by the crypto paper runtime in its own store.",
  "EQUITY PAPER": "Recorded by the EDA-1 equity paper runtime in its own store.",
};

export function AccountOrders({
  panel,
  generatedAt,
  limit,
  loading = false,
  title,
  footnote = true,
}: {
  panel: AccountOrdersPanel | null;
  generatedAt: string | null;
  /** Show only the newest `limit` rows. The Overview's condensed form. */
  limit?: number;
  loading?: boolean;
  title?: string;
  footnote?: boolean;
}) {
  const { t } = useI18n();
  const format = useFormat();
  const heading = title ?? t("orders.recent");

  if (loading && !panel) {
    return (
      <Card title={heading} bodyClassName="">
        <TableSkeleton rows={6} columns={8} />
      </Card>
    );
  }

  if (!panel) {
    return (
      <Card title={heading} bodyClassName="">
        <ErrorState headline={t("empty.ordersUnreadable")} detail={t("empty.ordersUnreadableDetail")} />
      </Card>
    );
  }

  const unavailable = panel.stores.filter((store) => !store.available);
  const rows = limit ? panel.rows.slice(0, limit) : panel.rows;
  const meta = (
    <>
      <Tag title={panel.note}>{t("orders.scope")}</Tag>
      <Tag title={t("orders.simulatedHint")}>
        {panel.includes_simulated ? t("orders.containsSimulated") : t("orders.noSimulated")}
      </Tag>
      {unavailable.length ? (
        <Pill tone="ATTENTION" emphasis>
          {t("orders.unreadableStores", {
            stores: unavailable.map((store) => store.source).join(", "),
          })}
        </Pill>
      ) : null}
      {panel.total > 0 ? (
        <span className="num text-meta text-ink-3">
          {rows.length === panel.total
            ? `${panel.total} ${t("common.total")}`
            : `${rows.length} ${t("common.of")} ${panel.total}`}
          {panel.duplicates_dropped
            ? ` · ${t("orders.duplicatesDropped", { count: panel.duplicates_dropped })}`
            : ""}
        </span>
      ) : null}
    </>
  );

  if (rows.length === 0) {
    return (
      <Card title={heading} meta={meta} bodyClassName="">
        <EmptyState headline={t("empty.noOrders")} detail={t("empty.noOrdersDetail")} />
      </Card>
    );
  }

  return (
    <Card title={heading} meta={meta} bodyClassName="">
      <DataTable
        caption={heading}
        minWidth="min-w-[860px]"
        head={
          <>
            <Th title={t("orders.col.timeHint")}>{t("orders.col.time")}</Th>
            <Th title={t("orders.col.sourceHint")}>{t("orders.col.source")}</Th>
            <Th>{t("orders.col.symbol")}</Th>
            <Th>{t("orders.col.side")}</Th>
            <Th align="right">{t("orders.col.quantity")}</Th>
            <Th align="right">{t("orders.col.filled")}</Th>
            <Th align="right">{t("orders.col.avgFill")}</Th>
            <Th align="right">{t("orders.col.status")}</Th>
          </>
        }
      >
        {rows.map((row) => (
          <Tr
            key={`${row.source}-${row.client_order_id}`}
            tint={row.needs_attention ? "warn" : undefined}
          >
            <Td
              numeric
              align="left"
              className="text-ink-2"
              title={format.stampFull(row.authoritative_at)}
            >
              {format.stamp(row.authoritative_at, generatedAt)}
            </Td>
            <Td>
              <Tag
                tone={row.asset_class === "CRYPTO" ? "ATTENTION" : "NEUTRAL"}
                title={SOURCE_HINT[row.source]}
              >
                {row.source}
              </Tag>
            </Td>
            <Td className="font-medium text-ink">{row.symbol}</Td>
            <Td
              className={cn(
                "text-meta font-medium tracking-[0.06em] uppercase",
                row.side === "BUY" ? "text-pos" : "text-neg",
              )}
            >
              {row.side}
            </Td>
            <Td numeric className="text-ink">
              {format.quantity(row.quantity)}
            </Td>
            <Td numeric className={row.filled_quantity === null ? "text-ink-3" : "text-ink-2"}>
              {format.quantity(row.filled_quantity)}
            </Td>
            <Td numeric className={row.average_fill_price === null ? "text-ink-3" : "text-ink-2"}>
              {money(row.average_fill_price)}
            </Td>
            <Td align="right">
              <Pill
                tone={row.status_tone}
                emphasis={row.needs_attention}
                title={
                  row.needs_attention
                    ? t("orders.statusHint.attention")
                    : t("orders.statusHint.normal", {
                        source: t(
                          row.status_source === "BROKER"
                            ? "orders.statusSource.broker"
                            : "orders.statusSource.local",
                        ),
                        code: row.risk_reason_code,
                      })
                }
              >
                {row.status}
              </Pill>
            </Td>
          </Tr>
        ))}
      </DataTable>
      {footnote ? (
        <p className="px-4 pt-2 pb-3 text-meta leading-snug text-ink-3">
          {t("orders.pendingNotFilled")}
        </p>
      ) : null}
    </Card>
  );
}
