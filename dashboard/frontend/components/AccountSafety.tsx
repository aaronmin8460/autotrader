"use client";

/**
 * The shared account safety state, and the shared API budget beside it.
 *
 * There is one brokerage account, so there is one answer to "may anything
 * submit an order right now?" — and it is deliberately not folded into either
 * runtime card. An ambiguous order raised by the equity service stops the
 * crypto service too, and a screen that showed that as a property of one
 * runtime would be describing the wrong thing.
 *
 * The `client_order_id` is shown for a halt caused by an unknown outcome
 * because it is the recovery anchor: it names the exact key to ask the broker
 * about. It is an identifier this system generated, never a credential.
 *
 * Read-only, like everything else. There is no clear-the-halt control here and
 * no endpoint that would accept one — the halt is cleared by a full-universe
 * reconciliation run from the CLI, and by nothing else.
 */

import { useI18n } from "@/lib/i18n";
import { useFormat } from "@/lib/i18n/useFormat";
import type { AccountSafetyPanel, ApiBudgetRow } from "@/lib/types";

import { Card, Dot, Field, Tag, cn, toneText } from "./ui";

export function ApiBudget({ budget }: { budget: ApiBudgetRow[] }) {
  const { t } = useI18n();
  return (
    <>
      {budget.map((row) => (
        <Field key={row.key} label={row.label} title={t("system.apiBudgetHint")}>
          <span className="num">
            <span className={row.remaining === 0 ? "text-warn" : "text-ink"}>{row.used}</span>
            <span className="text-ink-3">
              {" "}
              / {row.limit} {t("system.thisMinute")}
            </span>
          </span>
        </Field>
      ))}
    </>
  );
}

export function AccountSafety({
  panel,
  budget,
  lastFailure,
  lastFailureAt,
  generatedAt,
  showBudget = true,
  showFailure = true,
}: {
  panel: AccountSafetyPanel | null;
  budget: ApiBudgetRow[];
  lastFailure: string | null;
  lastFailureAt: string | null;
  generatedAt: string | null;
  showBudget?: boolean;
  showFailure?: boolean;
}) {
  const { t } = useI18n();
  const format = useFormat();

  if (!panel) {
    return (
      <Card title={t("risk.accountSafety")}>
        <p className="text-table text-ink-3">{t("risk.accountSafetyUnreadable")}</p>
      </Card>
    );
  }

  const meta = (
    <>
      <Tag title={t("risk.accountSafetySharedHint")}>{t("risk.accountSafetyShared")}</Tag>
      <span
        className={cn(
          "inline-flex items-center gap-1.5 text-meta leading-none font-medium",
          "tracking-[0.06em] uppercase",
          toneText(panel.tone),
        )}
      >
        <Dot tone={panel.tone} />
        {panel.state}
      </span>
    </>
  );

  return (
    <Card title={t("risk.accountSafety")} meta={meta} bodyClassName="">
      <div className="px-4 py-3.5">
        <p className={cn("text-table leading-snug", panel.safe_to_trade ? "text-ink-2" : "text-ink")}>
          {panel.detail}
        </p>
        {panel.client_order_id ? (
          <p className="num mt-2 text-meta text-ink-2">
            <span className="text-ink-3">{t("risk.unresolvedClientOrderId")} </span>
            {panel.client_order_id}
          </p>
        ) : null}
        {panel.safe_to_trade ? null : (
          <p className="mt-2 text-meta leading-snug text-ink-3">{t("risk.haltNote")}</p>
        )}
      </div>

      <div className="grid grid-cols-2 gap-x-5 gap-y-3.5 border-t border-subtle px-4 py-3.5 sm:grid-cols-4">
        <Field label={t("risk.setBy")}>{panel.source ?? "—"}</Field>
        <Field label={t("common.updated")}>
          <span className="num" title={format.stampFull(panel.updated_at)}>
            {format.stamp(panel.updated_at, generatedAt)}
            {panel.updated_at ? (
              <span className="ms-1.5 text-meta text-ink-3">
                {format.relative(panel.updated_at, generatedAt)}
              </span>
            ) : null}
          </span>
        </Field>
        {showBudget ? <ApiBudget budget={budget} /> : null}
      </div>

      {showFailure ? (
        <div className="border-t border-subtle px-4 py-3">
          <div className="flex items-baseline justify-between gap-3">
            <h3 className="eyebrow text-ink-3">{t("system.lastFailure")}</h3>
            {lastFailureAt ? (
              <span className="num text-meta text-ink-3">
                {format.stamp(lastFailureAt, generatedAt)} UTC ·{" "}
                {format.relative(lastFailureAt, generatedAt)}
              </span>
            ) : null}
          </div>
          <p className="mt-1.5 text-meta leading-snug text-ink-2">
            {lastFailure ?? <span className="text-ink-3">{t("system.noFailure")}</span>}
          </p>
        </div>
      ) : null}
    </Card>
  );
}
