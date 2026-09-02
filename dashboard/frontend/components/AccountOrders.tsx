/**
 * Recent orders across the whole account.
 *
 * Merged from the crypto store and the equity paper store by the paper API,
 * sorted by the broker's own submission time, and labelled per row with the
 * store the order came from. An intent the broker never answered for still
 * has a row - it is the row that matters most - and `UNKNOWN` is the only
 * status given extra visual weight.
 *
 * Nothing simulated can appear here: the read model takes no shadow record as
 * input and says so on the payload, and this table renders that statement.
 * There are no action controls, and no endpoint behind one either.
 */

import { money, quantity, stampUtc } from "@/lib/format";
import type { AccountOrdersPanel, OrderSource } from "@/lib/orders";

import { Card, Empty, Pill, Tag, Td, Th, cn } from "./ui";

const SOURCE_TITLE: Record<OrderSource, string> = {
  "CRYPTO PAPER": "Recorded by the crypto paper runtime in its own store.",
  "EQUITY PAPER": "Recorded by the EDA-1 equity paper runtime in its own store.",
};

export function AccountOrders({
  panel,
  generatedAt,
}: {
  panel: AccountOrdersPanel | null;
  generatedAt: string | null;
}) {
  if (!panel) {
    return (
      <Card title="Recent account orders" bodyClassName="">
        <Empty headline="Orders cannot be read" detail="The equity paper API did not answer." />
      </Card>
    );
  }

  const unavailable = panel.stores.filter((store) => !store.available);
  const meta = (
    <>
      <Tag title={panel.note}>Crypto + equity paper</Tag>
      <Tag title="Every row is a real broker order recorded by a trading runtime. No shadow or simulated action is an input to this list.">
        {panel.includes_simulated ? "Contains simulated rows" : "No simulated rows"}
      </Tag>
      {unavailable.length ? (
        <Pill tone="ATTENTION" emphasis>
          {unavailable.map((store) => store.source).join(", ")} unreadable
        </Pill>
      ) : null}
      {panel.total > 0 ? (
        <span className="num text-[11px] text-ink-3">
          {panel.rows.length === panel.total ? `${panel.total} total` : `${panel.rows.length} of ${panel.total}`}
          {panel.duplicates_dropped ? ` · ${panel.duplicates_dropped} duplicate dropped` : ""}
        </span>
      ) : null}
    </>
  );

  if (panel.rows.length === 0) {
    return (
      <Card title="Recent account orders" meta={meta} bodyClassName="">
        <Empty headline="No orders recorded" detail="Nothing has been submitted from either store yet." />
      </Card>
    );
  }

  return (
    <Card title="Recent account orders" meta={meta} bodyClassName="">
      <div className="scroll-x">
        <table className="w-full min-w-[820px] border-collapse">
          <thead>
            <tr className="border-b border-line">
              <Th title="The broker's submission time, or the intent's creation time when the broker never answered.">Time</Th>
              <Th>Asset</Th>
              <Th>Side</Th>
              <Th align="right">Quantity</Th>
              <Th align="right">Filled</Th>
              <Th align="right">Avg fill</Th>
              <Th title="The store the order was recorded in. A fact about provenance, not an inference from the symbol.">Source</Th>
              <Th align="right">Status</Th>
            </tr>
          </thead>
          <tbody>
            {panel.rows.map((row) => (
              <tr
                key={`${row.source}-${row.client_order_id}`}
                className={cn("border-b border-line/70 last:border-0 hover:bg-surface-2", row.needs_attention && "tint-warn")}
              >
                <Td numeric align="left" className="text-ink-2" title={`Created ${row.created_at}${row.filled_at ? ` · filled ${row.filled_at}` : ""}`}>
                  {stampUtc(row.authoritative_at, generatedAt)}
                </Td>
                <Td className="font-medium text-ink">{row.symbol}</Td>
                <Td className={cn("text-[11px] font-medium tracking-[0.06em] uppercase", row.side === "BUY" ? "text-pos" : "text-neg")}>
                  {row.side}
                </Td>
                <Td numeric className="text-ink">
                  {quantity(row.quantity)}
                </Td>
                <Td numeric className={row.filled_quantity === null ? "text-ink-3" : "text-ink-2"}>
                  {quantity(row.filled_quantity)}
                </Td>
                <Td numeric className={row.average_fill_price === null ? "text-ink-3" : "text-ink-2"}>
                  {money(row.average_fill_price)}
                </Td>
                <Td>
                  <Tag tone={row.asset_class === "CRYPTO" ? "ATTENTION" : "NEUTRAL"} title={SOURCE_TITLE[row.source]}>
                    {row.source}
                  </Tag>
                </Td>
                <Td align="right">
                  <Pill
                    tone={row.status_tone}
                    emphasis={row.needs_attention}
                    title={
                      row.needs_attention
                        ? "The broker outcome was never established. Reconciliation resolves this; the dashboard only reports it."
                        : `Reported by ${row.status_source === "BROKER" ? "the broker" : "local state"}. Risk: ${row.risk_reason_code}.`
                    }
                  >
                    {row.status}
                  </Pill>
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
