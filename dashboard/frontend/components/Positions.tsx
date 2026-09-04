"use client";

/**
 * What the account holds — the whole broker account, not one book.
 *
 * The scope label is load-bearing. This table is the paper brokerage account in
 * full, so crypto rows and Equity Paper rows appear in it together, and a
 * reader who assumes it is the crypto book will misread every total on it. The
 * `Class` column already distinguishes the rows; the header says what the set
 * of rows is, and a crypto row is tagged distinctly.
 *
 * The broker is the authority and the panel says so in its header. When the
 * broker cannot be read it falls back to the local snapshot, labels it
 * `LOCAL`, and leaves price and P&L empty rather than deriving a market value
 * from an entry price — which would be a number that looks live and is not.
 *
 * Weight is market value over account equity, both from the same broker read.
 * The trend column is a price sparkline from the chart layer and nothing else:
 * no signal, no target, no stance. Rows open the symbol detail; they are
 * focusable, answer Enter and Space, and issue no request of their own.
 *
 * Symbols, `EQUITY`/`CRYPTO` and every figure are rendered identically in both
 * locales. Only the column headings and the hints are translated.
 */

import type { ChartRange, ChartSeries } from "@/lib/charts";
import { useI18n } from "@/lib/i18n";
import { useFormat } from "@/lib/i18n/useFormat";
import { money, percent, signTone } from "@/lib/format";
import type { PositionsPanel } from "@/lib/types";

import { Sparkline } from "./charts/Sparkline";
import {
  Card,
  DataTable,
  EmptyState,
  ErrorState,
  SegmentedTimeRange,
  TableSkeleton,
  Tag,
  Td,
  Th,
  Tr,
  cn,
  toneText,
  useUnavailableLabel,
} from "./ui";

export function Positions({
  panel,
  generatedAt,
  equity,
  sparklines,
  sparkRange,
  onSparkRange,
  targetWeights,
  onSelect,
  loading = false,
  limit,
}: {
  panel: PositionsPanel | null;
  generatedAt: string | null;
  equity: number | null;
  sparklines: Readonly<Record<string, ChartSeries>>;
  sparkRange: ChartRange;
  onSparkRange: (range: ChartRange) => void;
  /** The paper runtime's recorded target weight per equity symbol, for context. */
  targetWeights: Record<string, number | null>;
  onSelect: (symbol: string) => void;
  loading?: boolean;
  /** Show only the largest `limit` rows. The Overview's condensed form. */
  limit?: number;
}) {
  const { t } = useI18n();
  const format = useFormat();
  const unavailableLabel = useUnavailableLabel();

  if (loading && !panel) {
    return (
      <Card title={t("positions.brokerTitle")} bodyClassName="">
        <TableSkeleton rows={6} columns={7} />
      </Card>
    );
  }

  if (!panel || panel.source === "UNAVAILABLE") {
    return (
      <Card title={t("positions.brokerTitle")} bodyClassName="">
        <ErrorState
          headline={t("empty.positionsUnreadable")}
          detail={t("empty.positionsUnreadableDetail")}
        />
      </Card>
    );
  }

  const meta = (
    <>
      <Tag title={t("positions.scopeHint")}>{t("positions.scope")}</Tag>
      <Tag title={panel.note ?? undefined}>{panel.source}</Tag>
      {panel.as_of ? (
        <span className="num text-meta text-ink-3" title={format.stampFull(panel.as_of)}>
          {format.stamp(panel.as_of, generatedAt)} UTC
        </span>
      ) : null}
      <SegmentedTimeRange
        options={["1D", "5D", "1M"] as const}
        value={sparkRange}
        onChange={onSparkRange}
        label={t("chart.trendRange")}
      />
    </>
  );

  if (panel.rows.length === 0) {
    const fromBroker = panel.source === "BROKER";
    return (
      <Card title={t("positions.brokerTitle")} meta={meta} bodyClassName="">
        <EmptyState
          headline={fromBroker ? t("empty.noPositions") : t("empty.noLocalSnapshot")}
          detail={
            fromBroker
              ? panel.flat_symbols.length
                ? t("empty.flatSymbols", {
                    symbols: panel.flat_symbols.join(", "),
                    verb: t(
                      panel.flat_symbols.length === 1 ? "empty.flatVerbOne" : "empty.flatVerbMany",
                    ),
                  })
                : t("empty.noPositionsDetail")
              : unavailableLabel(panel.unavailable_reason)
          }
        />
      </Card>
    );
  }

  const rows = limit
    ? [...panel.rows].sort((a, b) => (b.market_value ?? 0) - (a.market_value ?? 0)).slice(0, limit)
    : panel.rows;

  return (
    <Card title={t("positions.brokerTitle")} meta={meta} bodyClassName="">
      {panel.note ? <p className="px-4 pb-2 text-meta text-ink-3">{panel.note}</p> : null}
      <DataTable
        caption={t("positions.brokerTitle")}
        minWidth="min-w-[880px]"
        head={
          <>
            <Th>{t("positions.col.asset")}</Th>
            <Th>{t("positions.col.class")}</Th>
            <Th align="right">{t("positions.col.quantity")}</Th>
            <Th align="right">{t("positions.col.price")}</Th>
            <Th align="right">{t("positions.col.marketValue")}</Th>
            <Th align="right" title={t("positions.weightHint")}>
              {t("positions.col.weight")}
            </Th>
            <Th align="right">{t("positions.col.unrealized")}</Th>
            <Th align="right" title={t("positions.trendHint")}>
              {t("positions.col.trend", { range: sparkRange })}
            </Th>
            <Th align="right">{t("positions.col.updated")}</Th>
          </>
        }
      >
        {rows.map((row) => {
          const tone = signTone(row.unrealized_pnl);
          const weight = equity && row.market_value !== null ? row.market_value / equity : null;
          const target = targetWeights[row.symbol] ?? null;
          const crypto = row.asset_class === "CRYPTO";
          const spark = sparklines[row.symbol];
          return (
            <Tr
              key={row.symbol}
              onOpen={() => onSelect(row.symbol)}
              label={t("drawer.openSymbol", { symbol: row.symbol })}
            >
              <Td className="font-medium text-ink">{row.symbol}</Td>
              <Td>
                <Tag
                  tone={crypto ? "ATTENTION" : undefined}
                  title={crypto ? t("positions.cryptoHint") : t("positions.equityHint")}
                >
                  {row.asset_class}
                </Tag>
              </Td>
              <Td numeric className="text-ink">
                {format.quantity(row.quantity)}
              </Td>
              <Td numeric className={row.price === null ? "text-ink-3" : "text-ink-2"}>
                {money(row.price)}
              </Td>
              <Td numeric className={row.market_value === null ? "text-ink-3" : "text-ink"}>
                {money(row.market_value)}
              </Td>
              <Td numeric className="text-ink">
                {percent(weight, 2)}
                {target !== null ? (
                  <span className="ms-1.5 text-eyebrow text-ink-3" title={t("positions.targetRecorded")}>
                    / {percent(target, 2)}
                  </span>
                ) : null}
              </Td>
              <Td numeric>
                {row.unrealized_pnl === null ? (
                  <span className="text-ink-3">—</span>
                ) : (
                  <span className={cn(toneText(tone))}>
                    {format.signedMoney(row.unrealized_pnl)}
                    <span className="ms-1.5 text-meta text-ink-3">
                      {format.signedPercent(row.unrealized_pnl_fraction)}
                    </span>
                  </span>
                )}
              </Td>
              <Td align="right">
                <span className="inline-flex items-center justify-end gap-2">
                  <Sparkline series={spark} />
                  <span
                    className={cn(
                      "num w-[52px] text-meta",
                      spark?.available
                        ? toneText(signTone(spark.change_fraction ?? null))
                        : "text-ink-3",
                    )}
                  >
                    {spark?.available ? format.signedPercent(spark.change_fraction) : ""}
                  </span>
                </span>
              </Td>
              <Td numeric className="text-ink-3" title={format.stampFull(row.updated_at)}>
                {format.stamp(row.updated_at, generatedAt)}
              </Td>
            </Tr>
          );
        })}
      </DataTable>
      <p className="px-4 pt-2 pb-3 text-meta text-ink-3">{t("common.selectRowHint")}</p>
    </Card>
  );
}
