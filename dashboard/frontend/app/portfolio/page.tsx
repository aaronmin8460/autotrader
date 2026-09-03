"use client";

/**
 * Portfolio — everything about what the account holds, in one place.
 *
 * The full positions table (not the Overview's condensed eight), the runtime's
 * target against the broker's actual per symbol, the allocation with the
 * policy's target and cap marked on it, and unrealized P&L per position.
 *
 * Two records joined in the browser and only here: the paper API's recorded
 * decisions and the operational API's broker read. The target is never
 * recomputed from the policy — it is the runtime's own row — and the actual is
 * never taken from a snapshot when the broker can be read.
 *
 * The panel a portfolio page would normally lead with, an account-equity
 * curve, is present as an explicit `Not tracked` statement: no runtime
 * persists that series and no endpoint serves it. See `components/Portfolio`.
 */

import { useCallback, useMemo, useState } from "react";

import { TargetVsActual } from "@/components/EquityPaper";
import { Allocation, EquityHistory, UnrealizedByPosition } from "@/components/Portfolio";
import { Positions } from "@/components/Positions";
import { SymbolDetail } from "@/components/SymbolDetail";
import { PageHeader } from "@/components/shell/PageHeader";
import { useChartBatch, type ChartRange } from "@/lib/charts";
import { useDashboard } from "@/lib/dashboard";
import { useI18n } from "@/lib/i18n";
import { useAccountOrders } from "@/lib/orders";
import { equityOf, targetVsActual } from "@/lib/portfolio";

export default function PortfolioPage() {
  const { t } = useI18n();
  const { account, paper } = useDashboard();
  const { data, loading } = account;
  const { data: orders } = useAccountOrders();
  const [sparkRange, setSparkRange] = useState<ChartRange>("1D");
  const [selected, setSelected] = useState<string | null>(null);

  const rows = useMemo(
    () => targetVsActual(paper.data?.targets ?? [], data?.positions ?? null, data?.metrics ?? null),
    [paper.data, data],
  );
  const symbols = useMemo(() => (data?.positions?.rows ?? []).map((row) => row.symbol), [data]);
  const { series: sparklines } = useChartBatch(symbols, sparkRange);
  const equity = equityOf(data?.metrics ?? null);
  const targetWeights = useMemo(() => {
    const out: Record<string, number | null> = {};
    for (const row of rows) out[row.symbol] = row.target_weight;
    return out;
  }, [rows]);
  const close = useCallback(() => setSelected(null), []);

  return (
    <div className="space-y-5">
      <PageHeader title={t("portfolio.title")} context={t("nav.detail.portfolio")} />

      <Positions
        panel={data?.positions ?? null}
        generatedAt={data?.generated_at ?? null}
        equity={equity}
        sparklines={sparklines}
        sparkRange={sparkRange}
        onSparkRange={setSparkRange}
        targetWeights={targetWeights}
        onSelect={setSelected}
        loading={loading}
      />

      <TargetVsActual
        rows={rows}
        sparklines={sparklines}
        sparkRange={sparkRange}
        onSparkRange={setSparkRange}
        onSelect={setSelected}
        generatedAt={data?.generated_at ?? paper.data?.generated_at ?? null}
        brokerAvailable={data?.positions?.source === "BROKER"}
      />

      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-2">
        <Allocation
          positions={data?.positions ?? null}
          metrics={data?.metrics ?? null}
          policy={paper.data?.policy ?? null}
        />
        <UnrealizedByPosition positions={data?.positions ?? null} />
      </div>

      <EquityHistory />

      <SymbolDetail
        symbol={selected}
        onClose={close}
        position={data?.positions?.rows.find((row) => row.symbol === selected) ?? null}
        equity={equity}
        target={rows.find((row) => row.symbol === selected) ?? null}
        orders={orders}
        generatedAt={data?.generated_at ?? null}
      />
    </div>
  );
}
