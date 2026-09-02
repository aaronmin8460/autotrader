"use client";

/**
 * Equity Paper: EDA-1 U10 live paper observability.
 *
 * The header strip says which record this is and states the deployed policy
 * as the runtime announced it. Then what the runtime wants against what the
 * broker holds, per symbol, with a price trend; then the regime and policy
 * cards, the paper order log, and safety.
 *
 * Two records, joined in the browser: the paper API's recorded decisions and
 * the operational API's broker read. The target is never recomputed here from
 * the policy - it is the runtime's own row - and the actual is never taken
 * from a snapshot when the broker can be read.
 *
 * There is no control here. No start, no stop, no stage advance, no cancel -
 * and no endpoint behind any of them.
 */

import { useCallback, useMemo, useState } from "react";

import { PaperExposure, PaperHeaderStrip, PaperOrders, PaperRegime, PaperSafety, TargetVsActual } from "@/components/EquityPaper";
import { RealizedStrip } from "@/components/RealizedPnl";
import { Footer, Nav } from "@/components/Nav";
import { SymbolDetail } from "@/components/SymbolDetail";
import { useOverview } from "@/lib/api";
import { useChartBatch, type ChartRange } from "@/lib/charts";
import { useAccountOrders } from "@/lib/orders";
import { PAPER_POLL_INTERVAL_MS, usePaperOverview } from "@/lib/paper";
import { realizedBySymbol } from "@/lib/pnl";
import { useRealizedPnl } from "@/lib/realized";
import { equityOf, targetVsActual } from "@/lib/portfolio";

export default function EquityPaperPage() {
  const { data, loading, connected, lastSuccessAt } = usePaperOverview();
  const { data: account } = useOverview();
  const { data: orders } = useAccountOrders();
  const { data: realized } = useRealizedPnl();
  const [sparkRange, setSparkRange] = useState<ChartRange>("1D");
  const [selected, setSelected] = useState<string | null>(null);

  const rows = useMemo(
    () => targetVsActual(data?.targets ?? [], account?.positions ?? null, account?.metrics ?? null),
    [data, account],
  );
  const symbols = useMemo(() => rows.map((row) => row.symbol), [rows]);
  const { series: sparklines } = useChartBatch(symbols, sparkRange);
  const equity = equityOf(account?.metrics ?? null);
  const realizedRows = useMemo(() => realizedBySymbol(realized), [realized]);
  // The equity book's open unrealized P&L, from the broker's own per-position
  // figure. Summed over equity rows only, because the realized figure beside
  // it is the equity ledger's - pairing an account-wide unrealized with an
  // equity-only realized would invite exactly the arithmetic the strip says
  // not to do.
  const unrealized = useMemo(() => {
    const rows = (account?.positions?.rows ?? []).filter(
      (row) => row.asset_class === "EQUITY" && row.unrealized_pnl !== null,
    );
    if (rows.length === 0) return null;
    return rows.reduce((total, row) => total + (row.unrealized_pnl ?? 0), 0);
  }, [account]);
  const close = useCallback(() => setSelected(null), []);

  return (
    <div className="min-h-full">
      <Nav
        section="paper"
        verdict={account?.system_state ?? null}
        verdictTone={account?.system_state_tone ?? "MUTED"}
        verdictTitle={account?.attention.join(" ") || "Broker account"}
        badge={{ text: "Paper · no real money", title: "Orders on this page were really submitted, to a paper brokerage account. No real money is involved and this system has no live path." }}
        connected={connected}
        lastSuccessAt={lastSuccessAt}
      />

      <main className="mx-auto max-w-[1720px] space-y-4 px-5 py-5 sm:px-6">
        {loading && !data ? (
          <div className="flex min-h-[30vh] items-center justify-center">
            <p className="text-[13px] text-ink-3">Reading the equity paper record…</p>
          </div>
        ) : (
          <>
            <PaperHeaderStrip service={data?.service ?? null} regime={data?.regime ?? null} policy={data?.policy} generatedAt={data?.generated_at ?? null} />

            <RealizedStrip
              panel={realized}
              dailyPnl={account?.metrics?.daily_pnl ?? null}
              dailyPnlFraction={account?.metrics?.daily_pnl_fraction ?? null}
              unrealized={unrealized}
              generatedAt={account?.generated_at ?? null}
            />

            <TargetVsActual
              rows={rows}
              sparklines={sparklines}
              sparkRange={sparkRange}
              onSparkRange={setSparkRange}
              onSelect={setSelected}
              generatedAt={account?.generated_at ?? data?.generated_at ?? null}
              brokerAvailable={account?.positions?.source === "BROKER"}
              realized={realizedRows}
              accountingStatus={realized?.status ?? null}
            />

            <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,420px)]">
              <PaperExposure exposure={data?.exposure ?? null} policy={data?.policy} />
              <PaperRegime regime={data?.regime ?? null} />
            </div>

            <PaperOrders orders={data?.orders ?? []} generatedAt={data?.generated_at ?? null} />
            <PaperSafety safety={data?.safety ?? null} generatedAt={data?.generated_at ?? null} />
          </>
        )}
        <Footer intervalSeconds={PAPER_POLL_INTERVAL_MS / 1000} />
      </main>

      <SymbolDetail
        symbol={selected}
        onClose={close}
        position={account?.positions?.rows.find((row) => row.symbol === selected) ?? null}
        equity={equity}
        target={rows.find((row) => row.symbol === selected) ?? null}
        orders={orders}
        generatedAt={account?.generated_at ?? null}
      />
    </div>
  );
}
