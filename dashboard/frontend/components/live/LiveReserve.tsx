"use client";

/**
 * Section 6: the Profit Reserve, the withdrawal bucket, and the preview.
 *
 * The card that most needs to not be believed too much. Everything on it is an
 * accounting earmark: no money moved, no broker cash is segregated, and no
 * position size changed. The heading says OBSERVE ONLY beside the title rather
 * than in a footnote.
 *
 * **Two reserves, two names, never abbreviated to one.** The Trading Cash
 * Reserve is liquidity the allocation policy holds back and it changes position
 * sizing. The Profit Reserve is a notional earmark that changes nothing. They
 * share no field, no table and no module in the backend, and this card names
 * both so a reader who has seen the word "reserve" elsewhere is not left to
 * assume they are the same thing.
 *
 * **The withdrawal preview is not withdrawable cash.** It is informational, it
 * is not an authorization, and underneath the policy sits a stronger fact the
 * card states: this broker's Trading API exposes no transfer endpoint, and this
 * system contains no ACH, wire or scheduled transfer call at all.
 *
 * A zero preview is explained rather than left bare. Below the high-water mark,
 * before the monthly harvest checkpoint, and an empty bucket are three different
 * zeros, and the reason comes from the backend's own fields — never from local
 * arithmetic comparing two equity figures.
 */

import { useT } from "@/lib/i18n";
import { stampUtc } from "@/lib/format";
import type { LiveAccountingSummary } from "@/lib/live-contract";
import type { WithdrawalView } from "@/lib/live";
import { Card, Field, MetricBlock, SectionHeader, Tag } from "../ui";
import {
  Identifier,
  LiveTag,
  Money,
  NotAvailable,
  Note,
  Percent,
  Statement,
  Unknown,
  YesNo,
} from "./atoms";

export function LiveReserveCard({
  summary,
  withdrawal,
  suppressed,
}: {
  summary: LiveAccountingSummary | null;
  withdrawal: WithdrawalView;
  suppressed: boolean;
}) {
  const t = useT();
  const hide = suppressed;

  return (
    <Card
      title={t("live.reserve")}
      meta={
        <div className="flex flex-wrap items-center gap-2">
          <LiveTag />
          <Tag tone="ATTENTION">{t("live.reserve.observeOnly")}</Tag>
        </div>
      }
    >
      {/* --- the earmark ------------------------------------------------- */}
      <div className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-4">
        <MetricBlock
          label={t("live.reserve.accrued")}
          value={<Money value={hide ? null : summary?.profit_reserve_accrued} />}
          size="sm"
        />
        <MetricBlock
          label={t("live.reserve.bucket")}
          value={<Money value={hide ? null : summary?.withdrawal_bucket_balance} />}
          size="sm"
        />
        <MetricBlock
          label={t("live.withdrawal.preview")}
          value={<Money value={hide ? null : summary?.withdrawal_preview} />}
          size="sm"
          title={t("live.withdrawal.informational")}
        />
        <MetricBlock
          label={t("live.hwm.newProfit")}
          value={<Money value={hide ? null : summary?.new_hwm_profit} signed />}
          size="sm"
        />

        <Field label={t("live.reserve.policy")} wrap className="col-span-2 sm:col-span-1">
          <Identifier value={summary?.profit_reserve_policy_id} />
        </Field>
        <Field label={t("live.reserve.rate")} title={t("live.reserve.rateProvisional")}>
          <Percent value={summary?.profit_reserve_rate} digits={0} />
        </Field>
        <Field label={t("live.reserve.unreserved")} title={t("live.reserve.unreservedHint")}>
          <Money value={hide ? null : summary?.unreserved_withdrawal_total} />
        </Field>
        <Field label={t("live.ops.policyHash")} wrap>
          <Identifier value={summary?.profit_reserve_policy_hash?.slice(0, 12)} />
        </Field>
      </div>

      <Note className="mt-4">{t("live.reserve.notCash")}</Note>
      <Note className="mt-2">{t("live.reserve.rateProvisional")}</Note>

      {/* --- the two reserves, named apart -------------------------------- */}
      <div className="mt-5 border-t border-subtle pt-4">
        <SectionHeader
          title={`${t("live.reserve.profitReserve")} · ${t("live.reserve.tradingCashReserve")}`}
          level={3}
          className="mb-2"
        />
        <Note>{t("live.reserve.vsTradingCash")}</Note>
      </div>

      {/* --- withdrawal authorization ------------------------------------- */}
      <div className="mt-5 border-t border-subtle pt-4">
        <SectionHeader title={t("live.withdrawal")} level={3} className="mb-3" />

        <div className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-4">
          <Field label={t("live.withdrawal.mode")}>
            <Identifier value={withdrawal.mode} />
          </Field>
          <Field label={t("live.withdrawal.authorized")}>
            <YesNo value={withdrawal.authorized} invertTone />
          </Field>
          <Field label={t("live.withdrawal.automatic")}>
            <YesNo value={withdrawal.automatic} invertTone />
          </Field>
          <Field label={t("live.withdrawal.harvestEligible")}>
            <YesNo value={summary?.harvest_eligible} />
          </Field>

          <Field label={t("live.withdrawal.nextHarvest")} className="sm:col-span-2">
            <span className="num">
              {summary?.next_harvest_checkpoint_at
                ? stampUtc(summary.next_harvest_checkpoint_at, summary.generated_at)
                : "—"}
            </span>
          </Field>
          <Field
            label={t("live.withdrawal.brokerCash")}
            title={t("live.withdrawal.notExposed")}
            className="col-span-2"
          >
            {/*
              Declared always-null by the contract. `NOT VERIFIED` rather than
              UNKNOWN, and never replaced by `cash`: on a cash account `cash`
              includes unsettled proceeds, and showing it here would invite a
              request the broker would refuse.
            */}
            {summary?.broker_withdrawable_cash === null ||
            summary?.broker_withdrawable_cash === undefined ? (
              <span className="inline-flex flex-wrap items-center gap-2">
                <NotAvailable label={t("live.withdrawal.notVerified")} />
                <Tag>{summary?.broker_withdrawable_cash_status ?? "UNKNOWN"}</Tag>
              </span>
            ) : (
              <Money value={summary.broker_withdrawable_cash} />
            )}
          </Field>
        </div>

        {/* Why the preview reads what it reads. Never left to be guessed. */}
        {withdrawal.reasonKey ? (
          <Statement tone="MUTED" className="mt-3">
            {t(withdrawal.reasonKey)}
          </Statement>
        ) : null}

        <Note className="mt-3">{t("live.withdrawal.informational")}</Note>
        <Note className="mt-2">{t("live.withdrawal.notExposed")}</Note>
        <Note className="mt-2">{t("live.withdrawal.noTransferEndpoint")}</Note>
      </div>
    </Card>
  );
}

/** The unknown marker, re-exported for the suppressed case. */
export { Unknown };
