"use client";

/**
 * Equity Paper: EDA-1 U10 live paper observability.
 *
 * The header strip says which record this is and states the deployed policy as
 * the runtime announced it. Then what the runtime wants against what the broker
 * holds, per symbol, with a price trend; then the regime, the paper order log
 * and safety.
 *
 * Two records, joined in the browser: the paper API's recorded decisions and
 * the operational API's broker read. The target is never recomputed here from
 * the policy — it is the runtime's own row — and the actual is never taken from
 * a snapshot when the broker can be read.
 *
 * There is no control here. No start, no stop, no stage advance, no cancel —
 * and no endpoint behind any of them.
 */

import { useCallback, useMemo, useState } from "react";

import {
  PaperExposure,
  PaperHeaderStrip,
  PaperOrders,
  PaperSafety,
  TargetVsActual,
} from "@/components/EquityPaper";
import { MarketState } from "@/components/MarketState";
import { RealizedStrip } from "@/components/RealizedPnl";
import { SymbolDetail } from "@/components/SymbolDetail";
import { PageHeader } from "@/components/shell/PageHeader";
import { StrategyBadge } from "@/components/ui";
import { useChartBatch, type ChartRange } from "@/lib/charts";
import { useDashboard } from "@/lib/dashboard";
import { useI18n } from "@/lib/i18n";
import { useAccountOrders } from "@/lib/orders";
import { realizedBySymbol } from "@/lib/pnl";
import { equityOf, targetVsActual } from "@/lib/portfolio";
import { useRealizedPnl } from "@/lib/realized";

export default function EquityPaperPage() {
  const { t } = useI18n();
  const { account, paper } = useDashboard();
  const data = paper.data;
  const { data: orders } = useAccountOrders();
  const { data: realized } = useRealizedPnl();
  const [sparkRange, setSparkRange] = useState<ChartRange>("1D");
  const [selected, setSelected] = useState<string | null>(null);

  const rows = useMemo(
    () =>
      targetVsActual(data?.targets ?? [], account.data?.positions ?? null, account.data?.metrics ?? null),
    [data, account.data],
  );
  const symbols = useMemo(() => rows.map((row) => row.symbol), [rows]);
  const { series: sparklines } = useChartBatch(symbols, sparkRange);
  const equity = equityOf(account.data?.metrics ?? null);
  const realizedRows = useMemo(() => realizedBySymbol(realized), [realized]);
  // The equity book's open unrealized P&L, from the broker's own per-position
  // figure. Summed over equity rows only, because the realized figure beside
  // it is the equity ledger's — pairing an account-wide unrealized with an
  // equity-only realized would invite exactly the arithmetic the strip says
  // not to do.
  const unrealized = useMemo(() => {
    const held = (account.data?.positions?.rows ?? []).filter(
      (row) => row.asset_class === "EQUITY" && row.unrealized_pnl !== null,
    );
    if (held.length === 0) return null;
    return held.reduce((total, row) => total + (row.unrealized_pnl ?? 0), 0);
  }, [account.data]);
  const close = useCallback(() => setSelected(null), []);

  return (
    <div className="space-y-5">
      <PageHeader
        title={t("nav.equityPaper")}
        context={t("nav.detail.equityPaper")}
        actions={<StrategyBadge kind="PAPER" />}
      />

      <PaperHeaderStrip
        service={data?.service ?? null}
        regime={data?.regime ?? null}
        policy={data?.policy}
        generatedAt={data?.generated_at ?? null}
      />

      <RealizedStrip
        panel={realized}
        dailyPnl={account.data?.metrics?.daily_pnl ?? null}
        dailyPnlFraction={account.data?.metrics?.daily_pnl_fraction ?? null}
        unrealized={unrealized}
        generatedAt={account.data?.generated_at ?? null}
      />

      <TargetVsActual
        rows={rows}
        sparklines={sparklines}
        sparkRange={sparkRange}
        onSparkRange={setSparkRange}
        onSelect={setSelected}
        generatedAt={account.data?.generated_at ?? data?.generated_at ?? null}
        brokerAvailable={account.data?.positions?.source === "BROKER"}
        realized={realizedRows}
        accountingStatus={realized?.status ?? null}
      />

      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,440px)]">
        <PaperExposure exposure={data?.exposure ?? null} policy={data?.policy} />
        <MarketState regime={data?.regime ?? null} policy={data?.policy} />
      </div>

      <PaperOrders orders={data?.orders ?? []} generatedAt={data?.generated_at ?? null} />
      <PaperSafety safety={data?.safety ?? null} generatedAt={data?.generated_at ?? null} />

      <SymbolDetail
        symbol={selected}
        onClose={close}
        position={account.data?.positions?.rows.find((row) => row.symbol === selected) ?? null}
        equity={equity}
        target={rows.find((row) => row.symbol === selected) ?? null}
        orders={orders}
        generatedAt={account.data?.generated_at ?? null}
      />
    </div>
  );
}
