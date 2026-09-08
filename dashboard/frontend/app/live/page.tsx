"use client";

/**
 * Live — the real-money account, read only.
 *
 * The one page in this application where the numbers are somebody's actual
 * money, and the whole design follows from that. It answers, in the order an
 * operator needs them:
 *
 *   1  is this real money, and can it trade right now
 *   2  how much is there, and how much is exposed
 *   3  what are the ceilings
 *   4  is the accounting trustworthy
 *   5  what did trading actually make, as distinct from what was deposited
 *   6  how far below the high-water mark
 *   7  what is notionally reserved, and is any withdrawal authorized
 *
 * That order is also the source order, so the narrow-viewport stack is the
 * priority order without a second layout to maintain.
 *
 * **Two sources, one page, and neither is this file.** The frozen accounting
 * contract on `/api/live-accounting/summary` owns every money figure derived
 * from flows — flow-adjusted equity, trading P&L, the high-water mark, the
 * reserve, the bucket, the preview. The safety panel on `/api/live-safety/summary`
 * owns the arm switch, the account pin, and the ceilings, which it quotes from
 * the same function the risk engine enforces. **This page computes none of it.**
 * There is no subtraction, no ratio and no reconstruction of a suppressed field
 * anywhere below.
 *
 * **There is no control on this page.** No arm, no disarm, no start, no stop,
 * no withdraw, no deposit. Not hidden, not disabled, not behind a flag: the
 * components do not exist and neither endpoint has a write route to call. Both
 * services are `{GET, HEAD}` asserted against their assembled route tables.
 *
 * **A payload that fails the frozen contract renders nothing.** `validated`
 * runs before any card sees the summary; on a mismatch the page says the
 * contract drifted and shows the problems, because silently rendering a partial
 * payload is exactly the adaptation the freeze exists to prevent.
 */

import { useMemo } from "react";

import { LiveAccountCard, LiveRiskCard } from "@/components/live/LiveAccount";
import { LiveHeader } from "@/components/live/LiveHeader";
import { LiveOpsCard } from "@/components/live/LiveOps";
import {
  LiveFlowsCard,
  LiveHwmCard,
  LivePerformanceCard,
} from "@/components/live/LivePerformance";
import { LiveReserveCard } from "@/components/live/LiveReserve";
import { Note, Statement } from "@/components/live/atoms";
import { PageHeader } from "@/components/shell/PageHeader";
import { Card, ErrorState } from "@/components/ui";
import { useI18n } from "@/lib/i18n";
import {
  accountingView,
  guardView,
  readinessView,
  serviceView,
  useLiveAccounting,
  useLiveSafety,
  validated,
  withdrawalView,
} from "@/lib/live";

export default function LivePage() {
  const { t } = useI18n();
  const accountingPoll = useLiveAccounting();
  const safetyPoll = useLiveSafety();

  const panel = safetyPoll.data;
  const { summary, problems } = useMemo(
    () => validated(accountingPoll.data),
    [accountingPoll.data],
  );

  const readiness = useMemo(() => readinessView(panel, summary), [panel, summary]);
  const accounting = useMemo(() => accountingView(summary), [summary]);
  const withdrawal = useMemo(() => withdrawalView(summary), [summary]);
  const service = useMemo(() => serviceView(panel), [panel]);
  const guard = useMemo(() => guardView(panel), [panel]);

  // Derived money is withheld under any status that is not CLEAN or STALE.
  // The page never reconstructs a suppressed figure; it says why it is absent.
  const suppressed = summary !== null && !accounting.derivedTrustworthy;
  const drifted = accountingPoll.data !== null && summary === null;

  return (
    <div className="space-y-5">
      <PageHeader
        title={t("live.title")}
        context={t("live.paperContrast")}
      />

      {/* 1 — READY / ARMED. Always first, on every viewport. */}
      <LiveHeader
        view={readiness}
        changedAt={panel?.arm.changed_at ?? null}
        generatedAt={panel?.generated_at ?? summary?.generated_at ?? null}
      />

      {panel?.notices.length ? (
        <Card title={t("live.ops")}>
          <ul className="space-y-1.5">
            {panel.notices.map((notice) => (
              <li key={notice}>
                <Statement tone="ATTENTION">{notice}</Statement>
              </li>
            ))}
          </ul>
        </Card>
      ) : null}

      {/* 2 — the account, then 3 — the ceilings. */}
      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-2">
        <LiveAccountCard panel={panel} />
        <LiveRiskCard panel={panel} />
      </div>

      {/* 4 — whether the rest of the page may be believed. */}
      {drifted ? (
        <Card title={t("live.ops.accountingStatus")}>
          <ErrorState
            headline={t("live.status.contractDrift")}
            tone="NEGATIVE"
            detail={
              <span className="block space-y-1 text-start">
                {problems.slice(0, 6).map((problem) => (
                  <span key={problem} className="block">
                    {problem}
                  </span>
                ))}
              </span>
            }
          />
        </Card>
      ) : null}

      {suppressed ? (
        <Card title={t("live.ops.accountingStatus")}>
          <Statement tone="NEGATIVE" emphasis>
            {t("live.status.suppressed")}
          </Statement>
          <Note className="mt-2">{t("live.status.suppressedDetail")}</Note>
          {accounting.detail ? <Note className="mt-2">{accounting.detail}</Note> : null}
        </Card>
      ) : null}

      {accounting.presentation === "STALE" ? (
        <Card title={t("live.ops.accountingStatus")}>
          <Statement tone="ATTENTION" emphasis>
            {t("live.status.stale")}
          </Statement>
          <Note className="mt-2">{t("live.status.staleDetail")}</Note>
          {accounting.detail ? <Note className="mt-2">{accounting.detail}</Note> : null}
        </Card>
      ) : null}

      {/* 5 — performance, and the flows that must not be read as performance. */}
      <LivePerformanceCard summary={summary} suppressed={suppressed} />
      <LiveFlowsCard summary={summary} />

      {/* 6 — the high-water mark. */}
      <LiveHwmCard summary={summary} suppressed={suppressed} />

      {/* 7 — the reserve. Last, because it is the least actionable. */}
      <LiveReserveCard summary={summary} withdrawal={withdrawal} suppressed={suppressed} />

      {/* 8 — operations detail. */}
      <LiveOpsCard
        panel={panel}
        summary={summary}
        accounting={accounting}
        service={service}
        guard={guard}
      />

      <Note>{t("live.readOnlyNotice")}</Note>
    </div>
  );
}
