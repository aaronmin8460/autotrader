/**
 * What the account holds.
 *
 * The broker is the authority and the panel says so in its header. When the
 * broker cannot be read it falls back to the local snapshot, labels it
 * `LOCAL`, and leaves price and P&L empty rather than deriving a market value
 * from an entry price - which would be a number that looks live and is not.
 *
 * A flat account gets a real empty state naming the symbols that are flat, not
 * a table of zero rows.
 */

import {
  money,
  quantity,
  signTone,
  signedMoney,
  signedPercent,
  stampUtc,
  unavailableLabel,
} from "@/lib/format";
import type { PositionsPanel } from "@/lib/types";

import { Card, Empty, Tag, Td, Th, cn, toneText } from "./ui";

export function Positions({
  panel,
  generatedAt,
}: {
  panel: PositionsPanel | null;
  generatedAt: string | null;
}) {
  if (!panel || panel.source === "UNAVAILABLE") {
    return (
      <Card title="Positions" bodyClassName="">
        <Empty
          headline="Positions cannot be read"
          detail="Neither the broker nor the local operational database answered."
        />
      </Card>
    );
  }

  const meta = (
    <>
      <Tag title={panel.note ?? "Read live from the Alpaca paper account."}>{panel.source}</Tag>
      {panel.as_of ? (
        <span className="num text-[11px] text-ink-3">{stampUtc(panel.as_of, generatedAt)} UTC</span>
      ) : null}
    </>
  );

  // An empty table means two different things depending on where the answer
  // came from. The broker saying "nothing" is an account that is flat; the
  // local snapshot saying "nothing" while the broker is unreadable is not a
  // claim about the account at all, and must not be worded as one.
  if (panel.rows.length === 0) {
    const fromBroker = panel.source === "BROKER";
    return (
      <Card title="Positions" meta={meta} bodyClassName="">
        <Empty
          headline={fromBroker ? "No open positions" : "No local position snapshot"}
          detail={
            fromBroker ? (
              panel.flat_symbols.length ? (
                `${panel.flat_symbols.join(", ")} ${
                  panel.flat_symbols.length === 1 ? "is" : "are"
                } flat.`
              ) : (
                "The paper account holds nothing."
              )
            ) : (
              <>
                {unavailableLabel(panel.unavailable_reason)}, so what the account currently holds
                is unknown. Nothing has been written to the local snapshot either.
              </>
            )
          }
        />
      </Card>
    );
  }

  return (
    <Card title="Positions" meta={meta} bodyClassName="">
      {panel.note ? (
        <p className="border-b border-line px-4 py-2 text-[11.5px] text-ink-3">{panel.note}</p>
      ) : null}
      <div className="scroll-x">
        <table className="w-full min-w-[640px] border-collapse">
          <thead>
            <tr className="border-b border-line">
              <Th>Asset</Th>
              <Th>Class</Th>
              <Th align="right">Quantity</Th>
              <Th align="right">Price</Th>
              <Th align="right">Market value</Th>
              <Th align="right">Unrealized P&L</Th>
              <Th align="right">Updated</Th>
            </tr>
          </thead>
          <tbody>
            {panel.rows.map((row) => {
              const tone = signTone(row.unrealized_pnl);
              return (
                <tr
                  key={row.symbol}
                  className="border-b border-line last:border-0 hover:bg-sunken"
                >
                  <Td className="font-medium text-ink">{row.symbol}</Td>
                  <Td>
                    <Tag>{row.asset_class}</Tag>
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
                  <Td numeric>
                    {row.unrealized_pnl === null ? (
                      <span className="text-ink-3">—</span>
                    ) : (
                      <span className={cn(toneText(tone))}>
                        {signedMoney(row.unrealized_pnl)}
                        <span className="ml-1.5 text-[11px] text-ink-3">
                          {signedPercent(row.unrealized_pnl_fraction)}
                        </span>
                      </span>
                    )}
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
    </Card>
  );
}
