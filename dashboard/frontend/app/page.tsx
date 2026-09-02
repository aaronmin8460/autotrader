"use client";

/**
 * Operations: account-wide operational truth.
 *
 * The composition answers the operator's questions in the order they are
 * asked: the header says whether anything is wrong, the summary cards say
 * what the account is worth and how deployed it is against the deployed
 * policy, the left column says what is held and what was sent across the
 * whole account, and the right column says whether the machinery underneath
 * is healthy and how much risk is in use.
 *
 * **Four records, one page.** The operational API describes the broker
 * account and the crypto store's trail; the paper API describes the deployed
 * policy, the merged order list and which units are actually up; the chart
 * process supplies price series and nothing else. None of them can act, and
 * a chart failing leaves every account figure exactly as it was.
 *
 * There is no control on this page. No buy, no sell, no close, no start, no
 * stop, no editable limit - and no endpoint behind any of them.
 */

import { useCallback, useMemo, useState } from "react";

import { AccountOrders } from "@/components/AccountOrders";
import { AccountSafety } from "@/components/AccountSafety";
import { Attention } from "@/components/Attention";
import { Header } from "@/components/Header";
import { Metrics } from "@/components/Metrics";
import { Footer } from "@/components/Nav";
import { Portfolio } from "@/components/Portfolio";
import { Positions } from "@/components/Positions";
import { Risk } from "@/components/Risk";
import { Runtimes } from "@/components/Runtime";
import { SymbolDetail } from "@/components/SymbolDetail";
import { SystemHealth } from "@/components/SystemHealth";
import { POLL_INTERVAL_MS, useOverview, useServiceUnits } from "@/lib/api";
import { useChartBatch, type ChartRange } from "@/lib/charts";
import { useAccountOrders } from "@/lib/orders";
import { usePaperOverview } from "@/lib/paper";
import { equityOf, targetVsActual } from "@/lib/portfolio";
import { buildRiskView } from "@/lib/risk";

function Loading() {
  return (
    <div className="flex min-h-[40vh] items-center justify-center">
      <p className="text-[13px] text-ink-3">Reading operational state…</p>
    </div>
  );
}

function Unreachable() {
  return (
    <div className="card px-5 py-8 text-center">
      <p className="text-[13px] text-ink-2">The dashboard API is not answering.</p>
      <p className="mx-auto mt-2 max-w-[52ch] text-[12px] leading-snug text-ink-3">
        Start it with{" "}
        <code className="rounded-[3px] bg-sunken px-1 py-0.5 font-mono text-[11.5px] text-ink-2">python -m autotrader.dashboard</code>. It
        binds 127.0.0.1:8000 and serves GET routes only.
      </p>
    </div>
  );
}

export default function Page() {
  const { data, loading, connected, lastSuccessAt } = useOverview();
  const { services } = useServiceUnits();
  const { data: paper } = usePaperOverview();
  const { data: orders } = useAccountOrders();
  const [sparkRange, setSparkRange] = useState<ChartRange>("1D");
  const [selected, setSelected] = useState<string | null>(null);

  const symbols = useMemo(() => (data?.positions?.rows ?? []).map((row) => row.symbol), [data]);
  const { series: sparklines } = useChartBatch(symbols, sparkRange);

  const policy = paper?.policy ?? null;
  const equity = equityOf(data?.metrics ?? null);
  const risk = useMemo(
    () =>
      buildRiskView(
        data?.metrics ?? null,
        data?.positions ?? null,
        policy,
        data?.risk?.limits.find((limit) => limit.key === "daily_loss") ?? null,
      ),
    [data, policy],
  );
  const targetRows = useMemo(
    () => targetVsActual(paper?.targets ?? [], data?.positions ?? null, data?.metrics ?? null),
    [paper, data],
  );
  const targetWeights = useMemo(() => {
    const out: Record<string, number | null> = {};
    for (const row of targetRows) out[row.symbol] = row.target_weight;
    return out;
  }, [targetRows]);
  const close = useCallback(() => setSelected(null), []);

  return (
    <div className="min-h-full">
      <Header overview={data} connected={connected} lastSuccessAt={lastSuccessAt} />

      <main className="mx-auto max-w-[1720px] px-5 py-5 sm:px-6">
        {loading && !data ? (
          <Loading />
        ) : !data ? (
          <Unreachable />
        ) : (
          <div className="space-y-4">
            <Attention overview={data} />

            <Metrics metrics={data.metrics} risk={risk} />

            <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-12">
              <div className="space-y-4 xl:col-span-8">
                <Positions
                  panel={data.positions}
                  generatedAt={data.generated_at}
                  equity={equity}
                  sparklines={sparklines}
                  sparkRange={sparkRange}
                  onSparkRange={setSparkRange}
                  targetWeights={targetWeights}
                  onSelect={setSelected}
                />
                <AccountOrders panel={orders} generatedAt={orders?.generated_at ?? data.generated_at} />
                <Portfolio positions={data.positions} metrics={data.metrics} policy={policy} />
              </div>

              <div className="space-y-4 xl:col-span-4">
                <SystemHealth components={data.health} services={services} reconciliation={data.reconciliation} generatedAt={data.generated_at} />
                <Risk view={risk} />
                <AccountSafety
                  panel={data.account_safety}
                  budget={data.api_budget}
                  lastFailure={data.last_failure}
                  lastFailureAt={data.last_failure_at}
                  generatedAt={data.generated_at}
                />
                <Runtimes panels={data.runtimes} services={services} generatedAt={data.generated_at} />
              </div>
            </div>
          </div>
        )}

        <Footer intervalSeconds={POLL_INTERVAL_MS / 1000} />
      </main>

      <SymbolDetail
        symbol={selected}
        onClose={close}
        position={data?.positions?.rows.find((row) => row.symbol === selected) ?? null}
        equity={equity}
        target={targetRows.find((row) => row.symbol === selected) ?? null}
        orders={orders}
        generatedAt={data?.generated_at ?? null}
      />
    </div>
  );
}
