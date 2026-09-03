"use client";

/**
 * Overview — the executive/operator view, and the page that establishes the
 * visual language for the rest.
 *
 * The composition answers the operator's questions in the order they are
 * asked, and the numeric hierarchy is that order made visible:
 *
 *   1. is the system healthy      the top status bar, then this page's banner,
 *                                 which appears ONLY when something is wrong
 *   2. what is account equity     the one `display`-size figure on the page
 *   3. what happened today        the account-equity change, named as such
 *   4. strategy / market state    EDA-1 PARTICIPATE or DEFENSIVE, with its own
 *                                 drivers - NEW at V3; at V2 this page could
 *                                 not answer the question at all
 *   5. gross exposure             figure, target, hard cap, rail
 *   6. are risk limits healthy    the three policy rows, condensed
 *   7. positions                  the broker table, largest first
 *   8. recent orders              the merged stream, newest eight
 *   9. shadows                    what the observers observe, in violet
 *
 * **Four records, one page.** The operational API describes the broker account
 * and the crypto store's trail; the paper API describes the deployed policy,
 * the regime and the merged order list; the chart process supplies price series
 * and nothing else. None of them can act, and a chart failing leaves every
 * account figure exactly as it was.
 *
 * Process plumbing - API budgets, checkpoints, the last failure event, the
 * reconciliation detail - has moved to System, where it is complete. Nothing
 * was deleted; every panel still exists at a stable address.
 *
 * There is no control on this page. No buy, no sell, no close, no start, no
 * stop, no editable limit - and no endpoint behind any of them.
 */

import Link from "next/link";
import { useCallback, useMemo, useState } from "react";

import { AccountOrders } from "@/components/AccountOrders";
import { AccountSummary } from "@/components/AccountSummary";
import { Attention } from "@/components/Attention";
import { MarketState } from "@/components/MarketState";
import { Positions } from "@/components/Positions";
import { RealizedStrip } from "@/components/RealizedPnl";
import { Risk } from "@/components/Risk";
import { ShadowSummary } from "@/components/Shadows";
import { SymbolDetail } from "@/components/SymbolDetail";
import { PageHeader } from "@/components/shell/PageHeader";
import { ErrorState, SectionHeader, Surface } from "@/components/ui";
import { useChartBatch, type ChartRange } from "@/lib/charts";
import { useDashboard } from "@/lib/dashboard";
import { useI18n } from "@/lib/i18n";
import { useAccountOrders } from "@/lib/orders";
import { equityOf, targetVsActual } from "@/lib/portfolio";
import { useRealizedPnl } from "@/lib/realized";
import { buildRiskView } from "@/lib/risk";

/** How many rows the Overview's condensed tables show before deferring. */
const OVERVIEW_POSITIONS = 8;
const OVERVIEW_ORDERS = 8;

function Unreachable() {
  const { t } = useI18n();
  return (
    <Surface className="px-5 py-8">
      <ErrorState
        headline={t("error.apiDown.title")}
        tone="NEGATIVE"
        detail={t("error.apiDown.detail", { command: "python -m autotrader.dashboard" })}
      />
    </Surface>
  );
}

function MoreLink({ href, label }: { href: string; label: string }) {
  const { t } = useI18n();
  return (
    <Link
      href={href}
      className="rounded-xs px-2 py-1 text-meta font-medium text-accent hover:underline focus-visible:outline-2 focus-visible:outline-accent"
    >
      {t("common.viewAll")} · {label}
    </Link>
  );
}

export default function OverviewPage() {
  const { t } = useI18n();
  const { account, paper, services } = useDashboard();
  const { data, loading } = account;
  const { data: orders } = useAccountOrders();
  const [sparkRange, setSparkRange] = useState<ChartRange>("1D");
  const [selected, setSelected] = useState<string | null>(null);

  const symbols = useMemo(
    () =>
      [...(data?.positions?.rows ?? [])]
        .sort((a, b) => (b.market_value ?? 0) - (a.market_value ?? 0))
        .slice(0, OVERVIEW_POSITIONS)
        .map((row) => row.symbol),
    [data],
  );
  const { series: sparklines } = useChartBatch(symbols, sparkRange);

  const policy = paper.data?.policy ?? null;
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
    () => targetVsActual(paper.data?.targets ?? [], data?.positions ?? null, data?.metrics ?? null),
    [paper.data, data],
  );
  const targetWeights = useMemo(() => {
    const out: Record<string, number | null> = {};
    for (const row of targetRows) out[row.symbol] = row.target_weight;
    return out;
  }, [targetRows]);
  const { data: realized } = useRealizedPnl();
  // Equity-book unrealized only: the realized figure beside it is the equity
  // ledger's, and pairing an account-wide unrealized with an equity-only
  // realized would invite exactly the arithmetic the strip says not to do.
  const unrealized = useMemo(() => {
    const held = (data?.positions?.rows ?? []).filter(
      (row) => row.asset_class === "EQUITY" && row.unrealized_pnl !== null,
    );
    if (held.length === 0) return null;
    return held.reduce((total, row) => total + (row.unrealized_pnl ?? 0), 0);
  }, [data]);
  const close = useCallback(() => setSelected(null), []);

  if (!loading && !data) return <Unreachable />;

  return (
    <div className="space-y-5">
      <PageHeader title={t("nav.overview")} context={t("nav.detail.overview")} />

      {data ? <Attention overview={data} /> : null}

      <AccountSummary metrics={data?.metrics ?? null} risk={risk} loading={loading} />

      <RealizedStrip
        panel={realized}
        dailyPnl={data?.metrics?.daily_pnl ?? null}
        dailyPnlFraction={data?.metrics?.daily_pnl_fraction ?? null}
        unrealized={unrealized}
        generatedAt={data?.generated_at ?? null}
      />

      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-12">
        <div className="xl:col-span-7">
          <MarketState regime={paper.data?.regime ?? null} policy={policy} compact />
        </div>
        <div className="xl:col-span-5">
          <Risk view={risk} dense showNote={false} />
        </div>
      </div>

      <div className="space-y-2">
        <SectionHeader
          title={t("positions.title")}
          action={<MoreLink href="/portfolio" label={t("nav.portfolio")} />}
        />
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
          limit={OVERVIEW_POSITIONS}
        />
      </div>

      <div className="space-y-2">
        <SectionHeader
          title={t("orders.title")}
          action={<MoreLink href="/orders" label={t("nav.orders")} />}
        />
        <AccountOrders
          panel={orders}
          generatedAt={orders?.generated_at ?? data?.generated_at ?? null}
          limit={OVERVIEW_ORDERS}
          loading={loading}
          footnote={false}
        />
      </div>

      <div className="space-y-2">
        <SectionHeader
          title={t("shadows.title")}
          action={<MoreLink href="/shadows" label={t("nav.shadows")} />}
        />
        <ShadowSummary services={services} />
      </div>

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
