"use client";

/**
 * Risk — the active policy, and only the active policy.
 *
 * Every figure on this page comes from `/api/equity-paper/policy`, which the
 * paper API resolves from the running process's own start event. There is no
 * constant on this page, and specifically none of the retired policy's numbers:
 * the operational API's own risk panel still reports the crypto engine's own,
 * much lower per-symbol and account ceilings and currently marks both as
 * breached, and rendering those would paint a book sitting exactly where its
 * policy aims as a failure. `lib/risk` reads that panel for exactly one field
 * — the UTC-day loss halt, which it measures against a stored baseline nothing
 * else can see.
 *
 * A guard runs beside the panel: if the view ever carries a figure the
 * fractional policy retired while claiming to be that policy, the page says so
 * rather than drawing it.
 */

import { AccountSafety } from "@/components/AccountSafety";
import { MarketState } from "@/components/MarketState";
import { Risk } from "@/components/Risk";
import { PageHeader } from "@/components/shell/PageHeader";
import { Card, Field, SectionHeader, Status, Tag } from "@/components/ui";
import { useDashboard } from "@/lib/dashboard";
import { useI18n } from "@/lib/i18n";
import { percent } from "@/lib/format";
import { buildRiskView, carriesStaleLegacyLimit } from "@/lib/risk";
import { useMemo } from "react";

export default function RiskPage() {
  const { t } = useI18n();
  const { account, paper } = useDashboard();
  const data = account.data;
  const policy = paper.data?.policy ?? null;
  const exposure = paper.data?.exposure ?? null;

  const view = useMemo(
    () =>
      buildRiskView(
        data?.metrics ?? null,
        data?.positions ?? null,
        policy,
        data?.risk?.limits.find((limit) => limit.key === "daily_loss") ?? null,
      ),
    [data, policy],
  );
  const stale = carriesStaleLegacyLimit(view);

  return (
    <div className="space-y-5">
      <PageHeader title={t("risk.title")} context={t("nav.detail.risk")} />

      {stale ? (
        <Card title={t("risk.limits")}>
          <Status tone="NEGATIVE">{t("risk.fallbackNote")}</Status>
        </Card>
      ) : null}

      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-2">
        <Risk view={view} />

        <Card
          title={t("strategies.policy")}
          meta={
            policy ? (
              <Tag tone={policy.authoritative ? undefined : "ATTENTION"}>
                {policy.source.replace(/_/g, " ").toLowerCase()}
              </Tag>
            ) : (
              <Tag tone="ATTENTION">{t("risk.policyUnavailable")}</Tag>
            )
          }
        >
          <div className="grid grid-cols-2 gap-x-5 gap-y-3.5 sm:grid-cols-3">
            <Field label={t("strategies.policy")} className="sm:col-span-2" wrap>
              <span className="num text-table">{policy?.policy_id ?? "—"}</span>
            </Field>
            <Field label="config_hash">
              <span className="num">{policy?.config_hash ?? "—"}</span>
            </Field>
            <Field label={t("strategies.universe")}>
              <span className="num">
                {policy ? t("strategies.symbols", { count: policy.universe_size }) : "—"}
              </span>
            </Field>
            <Field label={t("market.targetGross")}>
              <span className="num">{policy ? percent(policy.target_gross, 0) : "—"}</span>
            </Field>
            <Field label={t("risk.hardCap")}>
              <span className="num">{policy ? percent(policy.hard_gross_cap, 0) : "—"}</span>
            </Field>
            <Field label={t("risk.perSymbol")}>
              <span className="num">{policy ? percent(policy.hard_symbol_cap, 0) : "—"}</span>
            </Field>
            <Field label={t("risk.cashReserve")}>
              <span className="num">{policy ? percent(policy.cash_reserve_target, 0) : "—"}</span>
            </Field>
            <Field label={t("risk.dailyLoss")}>
              <span className="num">{policy ? percent(policy.daily_loss_halt, 0) : "—"}</span>
            </Field>
            <Field label="fractional">{policy ? (policy.fractional ? "ON" : "OFF") : "—"}</Field>
            <Field
              label={t("risk.nonEquityPositions")}
              className="sm:col-span-3"
              title={t("risk.nonEquityHint")}
            >
              <span className="num">{exposure?.crypto_positions?.join("  ") || t("common.none")}</span>
            </Field>
          </div>
          {policy?.note ? (
            <p className="mt-4 max-w-[94ch] text-meta leading-relaxed text-ink-3">{policy.note}</p>
          ) : null}
        </Card>
      </div>

      <div className="space-y-2">
        <SectionHeader title={t("risk.operationalSafety")} />
        <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-2">
          <AccountSafety
            panel={data?.account_safety ?? null}
            budget={data?.api_budget ?? []}
            lastFailure={data?.last_failure ?? null}
            lastFailureAt={data?.last_failure_at ?? null}
            generatedAt={data?.generated_at ?? null}
            showBudget={false}
            showFailure={false}
          />
          <MarketState regime={paper.data?.regime ?? null} policy={policy} />
        </div>
      </div>
    </div>
  );
}
