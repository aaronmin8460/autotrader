/**
 * The portfolio pictures: where the account is, and what each position has
 * done since the broker's recorded entry.
 *
 * The allocation is every broker position plus settled cash, largest first,
 * with a structure strip that makes the deployed-versus-reserve shape obvious
 * and marks the policy's target and hard cap on it. The contribution chart is
 * the broker's own unrealized P&L per position - not a history, not realized,
 * and labelled as such. Nothing here is inferred from an order log.
 */

import { allocationSlices, pnlContributions } from "@/lib/portfolio";
import type { PolicyPanel } from "@/lib/paper";
import type { PositionsPanel, PrimaryMetrics } from "@/lib/types";

import { AllocationBars, StructureStrip } from "./charts/AllocationBars";
import { ContributionBars } from "./charts/ContributionBars";
import { Card, Empty, Tag } from "./ui";

export function Portfolio({
  positions,
  metrics,
  policy,
}: {
  positions: PositionsPanel | null;
  metrics: PrimaryMetrics | null;
  policy: PolicyPanel | null | undefined;
}) {
  const fromBroker = positions?.source === "BROKER";
  const slices = fromBroker ? allocationSlices(positions, metrics) : [];
  const contributions = fromBroker ? pnlContributions(positions) : [];

  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
      <Card
        title="Allocation"
        meta={
          <Tag title="Broker market value per position and settled cash, each as a share of account equity.">
            Share of equity
          </Tag>
        }
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
          <Empty headline="No broker allocation to show" detail="The allocation needs a live broker read of positions and cash." />
        )}
      </Card>

      <Card
        title="Unrealized P&L by position"
        meta={
          <Tag title="Broker market value minus the broker's average entry cost, per position. Not realized, and not a history.">
            Broker entry basis
          </Tag>
        }
      >
        {contributions.length ? (
          <ContributionBars rows={contributions} />
        ) : (
          <Empty headline="No unrealized P&L to show" detail="Needs a live broker read with an average entry price per position." />
        )}
      </Card>
    </div>
  );
}
