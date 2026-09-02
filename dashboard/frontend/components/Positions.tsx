"use client";

/**
 * What the account holds - the whole broker account, not one book.
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
 * from an entry price - which would be a number that looks live and is not.
 *
 * Weight is market value over account equity, both from the same broker read.
 * The trend column is a price sparkline from the chart layer and nothing
 * else: no signal, no target, no stance. Rows open the symbol detail; they are
 * focusable and answer Enter and Space, and they issue no request of their own.
 */

import type { KeyboardEvent } from "react";

import type { ChartRange, ChartSeries } from "@/lib/charts";
import {
  money,
  percent,
  quantity,
  signTone,
  signedMoney,
  signedPercent,
  stampUtc,
  unavailableLabel,
} from "@/lib/format";
import type { PositionsPanel } from "@/lib/types";

import { Sparkline } from "./charts/Sparkline";
import { Card, Empty, RangeSelector, Tag, Td, Th, cn, toneText } from "./ui";

export function Positions({
  panel,
  generatedAt,
  equity,
  sparklines,
  sparkRange,
  onSparkRange,
  targetWeights,
  onSelect,
}: {
  panel: PositionsPanel | null;
  generatedAt: string | null;
  equity: number | null;
  sparklines: Readonly<Record<string, ChartSeries>>;
  sparkRange: ChartRange;
  onSparkRange: (range: ChartRange) => void;
  /** The paper runtime's recorded target weight per equity symbol, for context. */
  targetWeights: Readonly<Record<string, number | null>>;
  onSelect: (symbol: string) => void;
}) {
  if (!panel || panel.source === "UNAVAILABLE") {
    return (
      <Card title="Broker account positions" bodyClassName="">
        <Empty
          headline="Positions cannot be read"
          detail="Neither the broker nor the local operational database answered."
        />
      </Card>
    );
  }

  const meta = (
    <>
      <Tag title="Every position on the broker paper account, crypto and equity together. Not one strategy's book.">
        Alpaca paper account
      </Tag>
      <Tag title={panel.note ?? "Read live from the broker paper account."}>{panel.source}</Tag>
      {panel.as_of ? (
        <span className="num text-[11px] text-ink-3">{stampUtc(panel.as_of, generatedAt)} UTC</span>
      ) : null}
      <RangeSelector options={["1D", "5D", "1M"] as const} value={sparkRange} onChange={onSparkRange} label="Trend range" />
    </>
  );

  if (panel.rows.length === 0) {
    const fromBroker = panel.source === "BROKER";
    return (
      <Card title="Broker account positions" meta={meta} bodyClassName="">
        <Empty
          headline={fromBroker ? "No open positions" : "No local position snapshot"}
          detail={
            fromBroker ? (
              panel.flat_symbols.length ? (
                `${panel.flat_symbols.join(", ")} ${panel.flat_symbols.length === 1 ? "is" : "are"} flat.`
              ) : (
                "The paper account holds nothing."
              )
            ) : (
              <>
                {unavailableLabel(panel.unavailable_reason)}, so what the account currently holds is
                unknown. Nothing has been written to the local snapshot either.
              </>
            )
          }
        />
      </Card>
    );
  }

  const open = (symbol: string) => (event: KeyboardEvent<HTMLTableRowElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onSelect(symbol);
    }
  };

  return (
    <Card title="Broker account positions" meta={meta} bodyClassName="">
      {panel.note ? <p className="px-4 pb-2 text-[11.5px] text-ink-3">{panel.note}</p> : null}
      <div className="scroll-x">
        <table className="w-full min-w-[860px] border-collapse">
          <thead>
            <tr className="border-b border-line">
              <Th>Asset</Th>
              <Th>Class</Th>
              <Th align="right">Quantity</Th>
              <Th align="right">Price</Th>
              <Th align="right">Market value</Th>
              <Th align="right" title="Market value over account equity, from the same broker read. The small figure is the paper runtime's recorded target for the symbol.">
                Weight
              </Th>
              <Th align="right">Unrealized P&L</Th>
              <Th align="right" title="Price only. Close prices from the chart layer; no signal, stance or target is drawn.">
                Trend {sparkRange}
              </Th>
              <Th align="right">Updated</Th>
            </tr>
          </thead>
          <tbody>
            {panel.rows.map((row) => {
              const tone = signTone(row.unrealized_pnl);
              const weight = equity && row.market_value !== null ? row.market_value / equity : null;
              const target = targetWeights[row.symbol] ?? null;
              const crypto = row.asset_class === "CRYPTO";
              return (
                <tr
                  key={row.symbol}
                  role="button"
                  tabIndex={0}
                  aria-label={`Open ${row.symbol} detail`}
                  onClick={() => onSelect(row.symbol)}
                  onKeyDown={open(row.symbol)}
                  className="row-link border-b border-line/70 last:border-0 hover:bg-surface-2"
                >
                  <Td className="font-medium text-ink">{row.symbol}</Td>
                  <Td>
                    <Tag tone={crypto ? "ATTENTION" : undefined} title={crypto ? "Crypto book. Traded 24/7 by the crypto paper runtime." : "Equity book. Traded by the EDA-1 paper runtime."}>
                      {row.asset_class}
                    </Tag>
                  </Td>
                  <Td numeric className="text-ink">
                    {quantity(row.quantity)}
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
                      <span className="ml-1.5 text-[10.5px] text-ink-3" title="Recorded target weight">
                        / {percent(target, 2)}
                      </span>
                    ) : null}
                  </Td>
                  <Td numeric>
                    {row.unrealized_pnl === null ? (
                      <span className="text-ink-3">—</span>
                    ) : (
                      <span className={cn(toneText(tone))}>
                        {signedMoney(row.unrealized_pnl)}
                        <span className="ml-1.5 text-[11px] text-ink-3">{signedPercent(row.unrealized_pnl_fraction)}</span>
                      </span>
                    )}
                  </Td>
                  <Td align="right">
                    <span className="inline-flex items-center justify-end gap-2">
                      <Sparkline series={sparklines[row.symbol]} />
                      <span
                        className={cn(
                          "num w-[52px] text-[11px]",
                          sparklines[row.symbol]?.change_fraction === undefined
                            ? "text-ink-3"
                            : toneText(signTone(sparklines[row.symbol]?.change_fraction ?? null)),
                        )}
                      >
                        {sparklines[row.symbol]?.available ? signedPercent(sparklines[row.symbol]?.change_fraction) : ""}
                      </span>
                    </span>
                  </Td>
                  <Td numeric className="text-ink-3">
                    {stampUtc(row.updated_at, generatedAt)}
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="px-4 pt-2 pb-3 text-[11px] text-ink-3">
        Select a row for the symbol detail and a larger chart.
      </p>
    </Card>
  );
}
