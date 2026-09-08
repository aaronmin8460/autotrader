"use client";

/**
 * Section 7: operations, health, and the deposit-day guard.
 *
 * The card that answers "can I trust the rest of this page". Two freshness
 * figures are shown, not one: the broker read and the accounting checkpoint age
 * separately, because a green light on one says nothing about the other and a
 * single merged indicator would let a fresh broker read vouch for a stale
 * accounting record.
 *
 * **`NOT INSTALLED` is not a failure.** Prompt 1 prepared the real-money unit
 * and deliberately left installing it as the first step of the first-day
 * runbook, so before the first Live day it is the correct state. It is drawn
 * muted with a line saying so; drawing it red would teach an operator to ignore
 * red on the one page where that must never happen.
 *
 * **The deposit-day guard is a control working, not a fault.** Its own sentence
 * is quoted from the backend rather than paraphrased — it is the line an
 * operator reads at 09:45 on the morning a deposit lands, and "blocked" without
 * it is the kind of message that gets overridden.
 */

import { useT } from "@/lib/i18n";
import { relative, stampUtc } from "@/lib/format";
import type { AccountingView, GuardView, ServiceView } from "@/lib/live";
import type { LiveAccountingSummary } from "@/lib/live-contract";
import type { LiveSafetyPanel } from "@/lib/live-safety";
import { Card, Field, SectionHeader, Status, Tag } from "../ui";
import { Identifier, LiveTag, Note, Statement, Unknown } from "./atoms";

export function LiveOpsCard({
  panel,
  summary,
  accounting,
  service,
  guard,
}: {
  panel: LiveSafetyPanel | null;
  summary: LiveAccountingSummary | null;
  accounting: AccountingView;
  service: ServiceView;
  guard: GuardView;
}) {
  const t = useT();
  const reconciliation = panel?.reconciliation ?? null;

  return (
    <Card title={t("live.ops")} meta={<LiveTag />}>
      {/* --- trust ------------------------------------------------------- */}
      <div className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3 xl:grid-cols-4">
        <Field label={t("live.ops.accountingStatus")}>
          <span className="inline-flex flex-wrap items-center gap-2">
            <Identifier value={accounting.statusLabel} />
            <Status tone={accounting.tone}>
              {accounting.presentation === "TRUSTED"
                ? "OK"
                : accounting.presentation === "STALE"
                  ? t("live.status.stale")
                  : accounting.presentation === "UNAVAILABLE"
                    ? t("live.status.unavailable")
                    : t("live.status.suppressed")}
            </Status>
          </span>
        </Field>
        <Field
          label={t("live.ops.dataFreshness")}
          title={t("live.ops.horizon", { seconds: accounting.stalenessHorizonSeconds })}
        >
          <span className="inline-flex flex-wrap items-center gap-2">
            <Identifier value={accounting.freshness} />
            {accounting.freshnessSeconds === null ? null : (
              <span className="num text-meta text-ink-3">{accounting.freshnessSeconds}s</span>
            )}
          </span>
        </Field>
        <Field label={t("live.ops.brokerFreshness")}>
          <span className="num">
            {panel?.account.read_at
              ? relative(panel.account.read_at, panel.generated_at)
              : "—"}
          </span>
        </Field>
        <Field label={t("live.ops.reconciliation")}>
          {reconciliation?.available ? (
            <span className="inline-flex flex-wrap items-center gap-2">
              <Identifier value={reconciliation.status} />
              {reconciliation.safe_to_trade === null ? null : (
                <Tag tone={reconciliation.safe_to_trade ? undefined : "ATTENTION"}>
                  {reconciliation.safe_to_trade ? "SAFE" : "UNSAFE"}
                </Tag>
              )}
            </span>
          ) : (
            <Unknown />
          )}
        </Field>
        <Field label={t("live.ops.accountSafety")}>
          {panel?.account_safety.available ? (
            <span className="inline-flex flex-wrap items-center gap-2">
              <Identifier value={panel.account_safety.state} />
              {panel.account_safety.established ? null : <Tag tone="ATTENTION">NOT ESTABLISHED</Tag>}
            </span>
          ) : (
            <Unknown />
          )}
        </Field>

        <Field label={t("live.ops.lastCheckpoint")}>
          <span className="num">
            {summary?.last_accounting_checkpoint_at
              ? stampUtc(summary.last_accounting_checkpoint_at, summary.generated_at)
              : "—"}
          </span>
        </Field>
        <Field label={t("live.ops.lastCompletedDay")}>
          <span className="num">
            {summary?.last_completed_day_checkpoint_at
              ? stampUtc(summary.last_completed_day_checkpoint_at, summary.generated_at)
              : "—"}
          </span>
        </Field>
        <Field label={t("live.account.lastSync")}>
          <span className="num">
            {panel?.account.read_at ? stampUtc(panel.account.read_at, panel.generated_at) : "—"}
          </span>
        </Field>
        <Field label={t("live.ops.rebuilds")}>
          {summary === null ? (
            <Unknown />
          ) : (
            <span className="num">
              {summary.rebuild_count}
              {summary.last_rebuild_at
                ? ` · ${stampUtc(summary.last_rebuild_at, summary.generated_at)}`
                : ""}
            </span>
          )}
        </Field>
      </div>

      {accounting.detail ? <Note className="mt-4">{accounting.detail}</Note> : null}

      {/* --- identity and provenance -------------------------------------- */}
      <div className="mt-5 border-t border-subtle pt-4">
        <SectionHeader title={t("live.ops.identityPin")} level={3} className="mb-3" />
        <div className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3 xl:grid-cols-4">
          <Field label={t("live.account.fingerprint")} title={t("live.account.fingerprintHint")}>
            <Identifier value={panel?.identity.fingerprint_short} />
          </Field>
          <Field label={t("live.ops.policyId")} wrap className="col-span-2 sm:col-span-1">
            <Identifier value={panel?.risk.policy_id} />
          </Field>
          <Field label={t("live.ops.policyHash")} wrap>
            <Identifier value={panel?.risk.policy_config_hash?.slice(0, 12)} />
          </Field>
          <Field label={t("live.ops.codeSha")}>
            <Identifier value={panel?.code_sha} />
          </Field>
          <Field label={t("live.ops.contractVersion")}>
            <Identifier value={summary?.contract_version} />
          </Field>
          <Field label={t("live.performance.twrConvention")} className="col-span-2">
            <Identifier value={summary?.twr_convention} />
          </Field>
        </div>
      </div>

      {/* --- the Live service --------------------------------------------- */}
      <div className="mt-5 border-t border-subtle pt-4">
        <SectionHeader title={t("live.ops.service")} level={3} className="mb-2" />
        <p className="mb-2 min-w-0">
          <Identifier value={service.unit} wrap className="text-ink-3" />
        </p>
        <Status tone={service.tone}>{t(service.labelKey)}</Status>
        {service.state === "NOT_INSTALLED" ? (
          <Note className="mt-2">{t("live.service.notInstalledIsExpected")}</Note>
        ) : null}
        {service.detail ? <Note className="mt-2">{service.detail}</Note> : null}
      </div>

      {/* --- the deposit-day guard ---------------------------------------- */}
      <div className="mt-5 border-t border-subtle pt-4">
        <SectionHeader
          title={t("live.guard")}
          level={3}
          meta={
            guard.riskDay ? (
              <span className="text-meta text-ink-3">
                {t("live.guard.riskDay")} <span className="num">{guard.riskDay}</span>
              </span>
            ) : null
          }
          className="mb-3"
        />
        <Statement tone={guard.tone}>{t(guard.labelKey)}</Statement>
        {guard.status === "ACTIVE" ? (
          <Note className="mt-2">{t("live.guard.exitsAvailable")}</Note>
        ) : null}
        {guard.reason ? <Note className="mt-2">{guard.reason}</Note> : null}
      </div>
    </Card>
  );
}
