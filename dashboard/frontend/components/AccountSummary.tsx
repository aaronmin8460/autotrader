"use client";

/**
 * The account's four headline figures.
 *
 * Four, and no more: equity, cash, today's change, and how much of the account
 * is deployed. Equity is set at `display` size and is the only figure on the
 * application allowed to be — the numeric hierarchy is the scan order.
 *
 * **The second figure is named carefully.** It is `Today's change`, and its
 * caption and tooltip both say it is an account-equity change against the
 * stored UTC-day baseline. It is NOT realized P&L: this build deploys no
 * realized-P&L accounting, so there is no realized figure to show and none is
 * implied. Relabelling an equity delta as realized P&L is the single most
 * dangerous thing this panel could do, and the wording exists to prevent it.
 *
 * Exposure carries the deployed policy's target and hard cap beside it, from
 * the risk view — never from a constant. When the policy cannot be read the
 * caption says so and names no limit.
 */

import { useI18n } from "@/lib/i18n";
import { useFormat } from "@/lib/i18n/useFormat";
import type { RiskView } from "@/lib/risk";
import { amount, money, percent, signTone } from "@/lib/format";
import type { Amount, PrimaryMetrics } from "@/lib/types";

import { ExposureRail } from "./charts/ExposureRail";
import { Figure, MetricBlock, MetricSkeleton, Status, Surface, Unavailable } from "./ui";

function Tile({ children, className }: { children: React.ReactNode; className?: string }) {
  return <Surface className={`px-4 py-3.5 ${className ?? ""}`}>{children}</Surface>;
}

export function AccountSummary({
  metrics,
  risk,
  loading,
}: {
  metrics: PrimaryMetrics | null;
  risk: RiskView;
  loading: boolean;
}) {
  const { t } = useI18n();
  const format = useFormat();

  if (loading && !metrics) return <MetricSkeleton count={4} />;

  if (!metrics) {
    return (
      <Surface className="px-4 py-6">
        <Unavailable reason="DATABASE_UNREADABLE" />
      </Surface>
    );
  }

  const changeTone = metrics.daily_pnl.available ? signTone(metrics.daily_pnl.value) : undefined;
  const baselineDate = metrics.daily_pnl_baseline_date;
  const total = risk.rows.find((row) => row.key === "total") ?? null;
  const cash = risk.rows.find((row) => row.key === "cash") ?? null;

  const cashCaption = (value: Amount, equity: Amount, reserve: number | null): string => {
    if (!value.available || !equity.available || !equity.value) return t("account.cashSettled");
    const share = t("account.cashOfEquity", { share: percent(value.value! / equity.value, 2) });
    return reserve === null
      ? share
      : `${share} · ${t("account.cashReserveTarget", { target: percent(reserve, 0) })}`;
  };

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Tile className="xl:col-span-1">
        <MetricBlock
          size="display"
          label={t("account.equity")}
          value={<Figure value={metrics.equity} render={money} />}
          context={t("account.equityContext")}
        />
      </Tile>

      <Tile>
        <MetricBlock
          label={t("account.cash")}
          value={<Figure value={metrics.cash} render={money} />}
          context={cashCaption(metrics.cash, metrics.equity, cash?.target ?? null)}
          title={t("account.cashHint")}
        />
      </Tile>

      <Tile>
        <MetricBlock
          label={t("account.dayChange")}
          tone={changeTone}
          value={<Figure value={metrics.daily_pnl} render={format.signedMoney} />}
          context={
            metrics.daily_pnl.available
              ? t("account.dayChangeContext", {
                  percent: format.signedPercent(metrics.daily_pnl_fraction),
                  baseline: amount(metrics.daily_pnl_baseline),
                })
              : t("account.dayChangeNeeds")
          }
          title={
            baselineDate
              ? t("account.dayChangeHint", { date: baselineDate })
              : t("account.dayChangeHintNoDate")
          }
        >
          <p className="mt-1.5 text-eyebrow tracking-[0.05em] text-ink-3 uppercase">
            {t("pnl.accountEquityChange")}
          </p>
        </MetricBlock>
      </Tile>

      <Tile>
        <MetricBlock
          label={t("account.grossExposure")}
          title={t("account.grossExposureHint")}
          value={
            metrics.exposure.available && metrics.exposure_fraction !== null ? (
              <span className="num">{percent(metrics.exposure_fraction, 2)}</span>
            ) : (
              <Figure value={metrics.exposure} render={money} />
            )
          }
          context={
            total ? (
              <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="num text-ink-2">{money(metrics.exposure.value)}</span>
                {total.target !== null ? (
                  <span className="num">
                    {t("account.target")} {percent(total.target, 0)}
                  </span>
                ) : null}
                {total.cap !== null ? (
                  <span className="num">
                    {t("account.hardCap")} {percent(total.cap, 0)}
                  </span>
                ) : null}
                <Status tone={total.tone}>{t(RISK_STATUS_KEY[total.status])}</Status>
              </span>
            ) : (
              t("account.policyUnavailable")
            )
          }
        >
          {total?.rail ? (
            <div className="mt-3">
              <ExposureRail
                current={total.rail.current}
                target={total.rail.target}
                cap={total.rail.cap}
                tone={total.tone}
                compact
              />
            </div>
          ) : null}
        </MetricBlock>
      </Tile>
    </div>
  );
}

/** The risk verdict words, by message key. Values themselves stay in English:
 *  they are the risk engine's own vocabulary and both catalogues keep them. */
export const RISK_STATUS_KEY = {
  "ON TARGET": "risk.status.onTarget",
  "BELOW TARGET": "risk.status.belowTarget",
  "ABOVE TARGET": "risk.status.aboveTarget",
  "NEAR CAP": "risk.status.nearCap",
  "OVER CAP": "risk.status.overCap",
  UNAVAILABLE: "risk.status.unavailable",
} as const;
