"use client";

/**
 * The Live summary card, for Overview.
 *
 * Seven figures and a link. Deliberately not the whole Live page: the profit
 * reserve, the withdrawal bucket and the preview are the least actionable and
 * the easiest to misread out of context, so they live on `/live` where the
 * OBSERVE ONLY framing sits beside them. Putting a "withdrawal preview" figure
 * on the landing page would be the single most misreadable number in this
 * application.
 *
 * What is here is what an operator glancing at Overview needs: whether real
 * money can trade, how much there is, how much is exposed against the ceiling,
 * what trading actually made, and whether the two trust signals are clean.
 *
 * **It is labelled LIVE · REAL MONEY at the top, in text.** Every other card on
 * Overview describes the paper account. This one does not, and the distinction
 * cannot rest on a colour.
 */

import Link from "next/link";

import { useI18n } from "@/lib/i18n";
import type { AccountingView, ReadinessView } from "@/lib/live";
import type { LiveAccountingSummary } from "@/lib/live-contract";
import type { LiveSafetyPanel } from "@/lib/live-safety";
import { Card, Field, MetricBlock, Status, cn } from "../ui";
import { Identifier, LiveTag, Money, SignedMoney, Statement, Unknown } from "./atoms";

export function LiveSummaryCard({
  panel,
  summary,
  readiness,
  accounting,
  suppressed,
}: {
  panel: LiveSafetyPanel | null;
  summary: LiveAccountingSummary | null;
  readiness: ReadinessView;
  accounting: AccountingView;
  suppressed: boolean;
}) {
  const { t } = useI18n();
  const armed = readiness.arm === "ARMED";

  return (
    <Card
      title={t("live.overview.title")}
      meta={
        <div className="flex flex-wrap items-center gap-2">
          <LiveTag />
          <Link
            href="/live"
            className={cn(
              "rounded-xs px-2 py-1 text-meta font-medium text-accent hover:underline",
              "focus-visible:outline-2 focus-visible:outline-accent",
            )}
          >
            {t("live.overview.open")}
          </Link>
        </div>
      }
      className={armed ? "tint-neg" : undefined}
    >
      {/* The two states, in words, before any figure. */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <Status tone={readiness.readyTone} size="md">
          {t(readiness.readyKey)}
        </Status>
        <Status tone={readiness.armTone} size="md">
          {t(readiness.armKey)}
        </Status>
      </div>
      {/* The sentence wraps; the two status WORDS above do not. */}
      <Statement tone={readiness.mutationTone} className="mt-2">
        {t(readiness.mutationKey)}
      </Statement>

      <div className="mt-4 grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3 xl:grid-cols-5">
        <MetricBlock
          label={t("live.account.brokerEquity")}
          value={<Money value={panel?.account.equity ?? summary?.current_broker_equity} />}
          size="sm"
        />
        <MetricBlock
          label={t("live.risk.currentGross")}
          value={<Money value={panel?.risk.current_gross_exposure} />}
          size="sm"
        />
        <MetricBlock
          label={t("live.risk.targetGross")}
          value={<Money value={panel?.risk.target_gross} />}
          size="sm"
        />
        <MetricBlock
          label={t("live.performance.tradingPnl")}
          value={<SignedMoney value={suppressed ? null : summary?.trading_pnl_since_inception} />}
          size="sm"
        />
        <Field label={t("live.ops.accountingStatus")}>
          <span className="inline-flex flex-wrap items-center gap-2">
            <Identifier value={accounting.statusLabel} />
          </span>
        </Field>

        <Field label={t("live.ops.reconciliation")}>
          {panel?.reconciliation.available ? (
            <Identifier value={panel.reconciliation.status} />
          ) : (
            <Unknown />
          )}
        </Field>
        <Field label={t("live.ops.accountSafety")}>
          {panel?.account_safety.available ? (
            <Identifier value={panel.account_safety.state} />
          ) : (
            <Unknown />
          )}
        </Field>
        <Field label={t("live.ops.service")}>
          <Identifier value={panel?.service.state} />
        </Field>
        <Field label={t("live.guard")}>
          <Identifier value={panel?.deposit_day_guard.status} />
        </Field>
        <Field label={t("live.ops.dataFreshness")}>
          <Identifier value={accounting.freshness} />
        </Field>
        <Field label={t("live.account.fingerprint")} title={t("live.account.fingerprintHint")}>
          <Identifier value={panel?.identity.fingerprint_short} />
        </Field>
      </div>
    </Card>
  );
}
