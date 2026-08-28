/**
 * What was actually sent, and what became of it.
 *
 * Driven by durable order intents joined to broker snapshots, so an order the
 * broker never answered for still has a row - which is precisely the row that
 * matters. `UNKNOWN` is the only status given extra visual weight, because it
 * is the only one that means nobody knows what happened at the broker.
 *
 * There are no action controls in this table, and there is no endpoint behind
 * one either. Cancelling an order is not something this product can do.
 */

import { money, quantity, stampUtc } from "@/lib/format";
import type { OrdersPanel } from "@/lib/types";

import { Card, Empty, Pill, Td, Th, cn } from "./ui";

export function Orders({
  panel,
  generatedAt,
}: {
  panel: OrdersPanel | null;
  generatedAt: string | null;
}) {
  if (!panel || panel.unavailable_reason) {
    return (
      <Card title="Recent orders" bodyClassName="">
        <Empty
          headline="Orders cannot be read"
          detail="The local operational database did not answer."
        />
      </Card>
    );
  }

  const meta =
    panel.total > 0 ? (
      <span className="num text-[11px] text-ink-3">
        {panel.rows.length === panel.total
          ? `${panel.total} total`
          : `${panel.rows.length} of ${panel.total}`}
      </span>
    ) : null;

  if (panel.rows.length === 0) {
    return (
      <Card title="Recent orders" bodyClassName="">
        <Empty
          headline="No orders recorded"
          detail="Nothing has been submitted against this database yet."
        />
      </Card>
    );
  }

  return (
    <Card title="Recent orders" meta={meta} bodyClassName="">
      <div className="scroll-x">
        <table className="w-full min-w-[680px] border-collapse">
          <thead>
            <tr className="border-b border-line">
              <Th>Time</Th>
              <Th>Asset</Th>
              <Th>Side</Th>
              <Th align="right">Quantity</Th>
              <Th align="right">Filled</Th>
              <Th align="right">Avg fill</Th>
              <Th align="right">Status</Th>
            </tr>
          </thead>
          <tbody>
            {panel.rows.map((row) => (
              <tr
                key={row.client_order_id}
                className={cn(
                  "border-b border-line last:border-0 hover:bg-sunken",
                  row.needs_attention && "tint-warn",
                )}
              >
                <Td numeric align="left" className="text-ink-2">
                  {stampUtc(row.created_at, generatedAt)}
                </Td>
                <Td className="font-medium text-ink">{row.symbol}</Td>
                <Td className="text-[11px] font-medium tracking-[0.06em] text-ink-2 uppercase">
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
