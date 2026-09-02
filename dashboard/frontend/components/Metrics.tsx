/**
 * The four headline numbers.
 *
 * Four, and no more: equity, cash, the day's P&L, and how much of the account
 * is deployed. Each is one figure with a line of context under it - what the
 * number is measured against - because a percentage with no denominator on
 * screen is a number an operator has to trust rather than read.
 *
 * Exposure and cash carry the deployed policy's target and hard cap beside
 * them, from the risk view. Not from a constant: when the policy cannot be
 * read the context says so instead of naming a limit.
 */

import { amount, money, percent, signTone, signedMoney, signedPercent } from "@/lib/format";
import type { RiskView } from "@/lib/risk";
import type { Amount, PrimaryMetrics } from "@/lib/types";

import { ExposureRail } from "./charts/ExposureRail";
import { Figure, Metric, Status, Unavailable } from "./ui";

function cashCaption(cash: Amount, equity: Amount, reserve: number | null): string {
  if (!cash.available || !equity.available || !equity.value) return "Settled paper cash";
  const share = percent(cash.value! / equity.value, 2);
  return reserve === null ? `${share} of equity` : `${share} of equity · reserve target ${percent(reserve, 0)}`;
}

export function Metrics({ metrics, risk }: { metrics: PrimaryMetrics | null; risk: RiskView }) {
  if (!metrics) {
    return (
      <div className="card px-4 py-6">
        <Unavailable reason="DATABASE_UNREADABLE" />
      </div>
    );
  }

  const pnlTone = metrics.daily_pnl.available ? signTone(metrics.daily_pnl.value) : undefined;
  const baselineDate = metrics.daily_pnl_baseline_date;
  const total = risk.rows.find((row) => row.key === "total") ?? null;
  const cash = risk.rows.find((row) => row.key === "cash") ?? null;

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Metric
        label="Portfolio equity"
        value={<Figure value={metrics.equity} render={money} />}
        context="Broker paper account · positions plus cash"
      />
      <Metric
        label="Cash"
        value={<Figure value={metrics.cash} render={money} />}
        context={cashCaption(metrics.cash, metrics.equity, cash?.target ?? null)}
        title="Settled cash on the paper account. The reserve target is what the policy deliberately leaves undeployed."
      />
      <Metric
        label="Daily P&L"
        tone={pnlTone}
        value={<Figure value={metrics.daily_pnl} render={signedMoney} />}
        context={
          metrics.daily_pnl.available
            ? `${signedPercent(metrics.daily_pnl_fraction)} vs ${amount(metrics.daily_pnl_baseline)} UTC-day baseline`
            : "Needs live equity and a stored UTC baseline"
        }
        title={
          baselineDate
            ? `Measured against the equity first observed on ${baselineDate} UTC.`
            : "Measured against the stored UTC-day baseline equity."
        }
      />
      <Metric
        label="Total exposure"
        value={
          metrics.exposure.available && metrics.exposure_fraction !== null ? (
            <span className="num">{percent(metrics.exposure_fraction, 2)}</span>
          ) : (
            <Figure value={metrics.exposure} render={money} />
          )
        }
        title="Aggregate long market value against account equity, both books counted."
        context={
          total ? (
            <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="num text-ink-2">{money(metrics.exposure.value)}</span>
              {total.target !== null ? <span className="num">target {percent(total.target, 0)}</span> : null}
              {total.cap !== null ? <span className="num">hard cap {percent(total.cap, 0)}</span> : null}
              {cash?.current !== null && cash?.current !== undefined ? (
                <span className="num">cash reserve {percent(cash.current, 2)}</span>
              ) : null}
              <Status tone={total.tone}>{total.status}</Status>
            </span>
          ) : (
            "Policy target and cap unavailable"
          )
        }
      >
        {total?.rail ? (
          <div className="mt-3">
            <ExposureRail current={total.rail.current} target={total.rail.target} cap={total.rail.cap} tone={total.tone} compact />
          </div>
        ) : null}
      </Metric>
    </div>
  );
}
