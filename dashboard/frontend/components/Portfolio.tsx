"use client";

/**
 * The portfolio pictures: where the account is, and what each position has
 * done since the broker's recorded entry.
 *
 * The allocation is every broker position plus settled cash, largest first,
 * with a structure strip that makes the deployed-versus-reserve shape obvious
 * and marks the policy's target and hard cap on it.
 *
 * The contribution chart is the broker's own **unrealized** P&L per position —
 * not a history, not realized, and labelled as such in both places. Beside it
 * the realized figure is reported as `Not tracked` with the reason, because
 * this build deploys no realized-P&L accounting and a reconstructed one would
 * be a number no runtime has ever computed.
 */

import { useI18n } from "@/lib/i18n";
import { allocationSlices, pnlContributions } from "@/lib/portfolio";
import type { PolicyPanel } from "@/lib/paper";
import type { PositionsPanel, PrimaryMetrics } from "@/lib/types";

import { AllocationBars, StructureStrip } from "./charts/AllocationBars";
import { ContributionBars } from "./charts/ContributionBars";
import { Card, EmptyState, NotTracked, Tag } from "./ui";

export function Allocation({
  positions,
  metrics,
  policy,
}: {
  positions: PositionsPanel | null;
  metrics: PrimaryMetrics | null;
  policy: PolicyPanel | null | undefined;
}) {
  const { t } = useI18n();
  const fromBroker = positions?.source === "BROKER";
  const slices = fromBroker ? allocationSlices(positions, metrics) : [];

  return (
    <Card
      title={t("portfolio.allocation")}
      meta={<Tag title={t("portfolio.allocationHint")}>{t("portfolio.shareOfEquity")}</Tag>}
    >
      {slices.length ? (
        <>
          <StructureStrip
            slices={slices}
            targetGross={policy?.target_gross ?? null}
            hardCap={policy?.hard_gross_cap ?? null}
          />
          <div className="mt-4">
            <AllocationBars slices={slices} />
          </div>
        </>
      ) : (
        <EmptyState headline={t("empty.noAllocation")} detail={t("empty.noAllocationDetail")} />
      )}
    </Card>
  );
}

export function UnrealizedByPosition({ positions }: { positions: PositionsPanel | null }) {
  const { t } = useI18n();
  const fromBroker = positions?.source === "BROKER";
  const contributions = fromBroker ? pnlContributions(positions) : [];

  return (
    <Card
      title={t("pnl.byPosition")}
      meta={<Tag title={t("pnl.unrealizedHint")}>{t("pnl.brokerEntryBasis")}</Tag>}
      bodyClassName=""
    >
      <div className="px-4 pb-4">
        {contributions.length ? (
          <ContributionBars rows={contributions} />
        ) : (
          <EmptyState headline={t("empty.noUnrealized")} detail={t("empty.noUnrealizedDetail")} />
        )}
      </div>
      {/* The distinction this dashboard must never blur. */}
      <div className="border-t border-subtle">
        <NotTracked headline={t("pnl.realized")} detail={t("pnl.realizedNotTracked")} />
      </div>
    </Card>
  );
}

/**
 * The account-equity curve a portfolio page would normally lead with.
 *
 * There is no such series. No runtime persists account equity over time and no
 * read-only endpoint serves one — the only stored figure is today's UTC-day
 * opening baseline. Reconstructing a curve from today's price bars would be
 * wrong as well as invented, because the positions themselves changed during
 * the day. The panel says exactly that instead of drawing something.
 */
export function EquityHistory() {
  const { t } = useI18n();
  return (
    <Card title={t("portfolio.equityCurve")} bodyClassName="">
      <NotTracked
        headline={t("portfolio.equityCurve")}
        detail={t("portfolio.equityCurveNotTracked")}
      />
    </Card>
  );
}

/** Both pictures side by side. Kept for the Overview's condensed row. */
export function Portfolio({
  positions,
  metrics,
  policy,
}: {
  positions: PositionsPanel | null;
  metrics: PrimaryMetrics | null;
  policy: PolicyPanel | null | undefined;
}) {
  return (
    <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-2">
      <Allocation positions={positions} metrics={metrics} policy={policy} />
      <UnrealizedByPosition positions={positions} />
    </div>
  );
}
