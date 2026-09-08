"use client";

/**
 * Sections 1 and 2: the account, and the ceilings it may trade inside.
 *
 * **The account number is never here.** Only the 32-character fingerprint
 * Prompt 1 pinned, shown as its first eight characters — enough to tell two
 * accounts apart in a runbook, and not the account number under any length.
 *
 * **The ceilings are quoted, never computed.** Every figure in the risk card
 * arrives already resolved by `live.budget.effective_ceilings`, the same
 * function the risk engine's arithmetic lives beside. Nothing in this file
 * multiplies an equity by a percentage; a dashboard that derived its own
 * ceilings would eventually draw limits nothing is enforcing.
 *
 * **Cash is not withdrawable cash.** The live account is a cash account, so
 * `cash` includes sale proceeds that are unsettled for T+1. The card says so
 * rather than letting an operator read the number as spendable.
 */

import { useT } from "@/lib/i18n";
import { stampUtc } from "@/lib/format";
import type { LiveSafetyPanel } from "@/lib/live-safety";
import { bindingIsAuthorization, ceilingsResolved } from "@/lib/live-safety";
import { Card, Field, MetricBlock, Tag } from "../ui";
import { Identifier, LiveTag, Money, Note, Percent, Statement, Unknown, YesNo } from "./atoms";

export function LiveAccountCard({ panel }: { panel: LiveSafetyPanel | null }) {
  const t = useT();
  const account = panel?.account ?? null;
  const identity = panel?.identity ?? null;
  const readable = account?.status === "OK";

  return (
    <Card title={t("live.account")} meta={<LiveTag />}>
      {!readable ? (
        <Statement tone="ATTENTION">
          {account?.status === "NOT_CONFIGURED"
            ? t("live.account.notConfigured")
            : account?.status === "ACCOUNT_MISMATCH"
              ? t("live.account.mismatch")
              : t("live.account.unreadable")}
        </Statement>
      ) : null}

      <div className="mt-3 grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3 xl:grid-cols-4">
        <MetricBlock
          label={t("live.account.brokerEquity")}
          value={<Money value={account?.equity} />}
          size="sm"
        />
        <MetricBlock
          label={t("live.account.cash")}
          value={<Money value={account?.cash} />}
          size="sm"
          title={t("live.account.cashSettlement")}
        />
        <MetricBlock
          label={t("live.account.buyingPower")}
          value={<Money value={account?.buying_power} />}
          size="sm"
        />
        <Field label={t("live.account.accountType")}>
          {account?.account_type ? <Identifier value={account.account_type} /> : <Unknown />}
        </Field>

        <Field label={t("live.account.status")}>
          <Identifier value={account?.account_status} />
        </Field>
        <Field label={t("live.account.multiplier")}>
          <Identifier value={account?.multiplier} />
        </Field>
        <Field label={t("live.account.shorting")}>
          <YesNo value={account?.shorting_enabled} invertTone />
        </Field>
        <Field label={t("live.account.positions")}>
          {account?.position_count === null || account?.position_count === undefined ? (
            <Unknown />
          ) : (
            <span className="num">{account.position_count}</span>
          )}
        </Field>

        <Field label={t("live.account.openOrders")}>
          {account?.open_order_count === null || account?.open_order_count === undefined ? (
            <Unknown />
          ) : (
            <span className="num">{account.open_order_count}</span>
          )}
        </Field>
        <Field label={t("live.account.tradingBlocked")}>
          <YesNo value={account?.trading_blocked} invertTone />
        </Field>
        <Field label={t("live.account.accountBlocked")}>
          <YesNo value={account?.account_blocked} invertTone />
        </Field>
        <Field label={t("live.account.transfersBlocked")}>
          <YesNo value={account?.transfers_blocked} invertTone />
        </Field>

        <Field
          label={t("live.account.fingerprint")}
          title={t("live.account.fingerprintHint")}
          className="sm:col-span-2"
        >
          <span className="inline-flex flex-wrap items-center gap-2">
            <Identifier value={identity?.fingerprint_short} />
            <Tag tone={identity?.status === "PINNED" ? undefined : "ATTENTION"}>
              {identity?.status === "PINNED"
                ? t("live.identity.pinned")
                : identity?.status === "MISMATCH"
                  ? t("live.identity.mismatch")
                  : identity?.status === "NOT_PINNED"
                    ? t("live.identity.notPinned")
                    : t("live.identity.unknown")}
            </Tag>
          </span>
        </Field>
        <Field label={t("live.account.lastSync")} className="sm:col-span-2">
          <span className="num">
            {account?.read_at ? stampUtc(account.read_at, panel?.generated_at) : "—"}
          </span>
        </Field>
      </div>

      <Note className="mt-4">{t("live.account.cashSettlement")}</Note>
      {identity?.detail ? <Note className="mt-2">{identity.detail}</Note> : null}
    </Card>
  );
}

export function LiveRiskCard({ panel }: { panel: LiveSafetyPanel | null }) {
  const t = useT();
  const risk = panel?.risk ?? null;
  const resolved = ceilingsResolved(panel);
  const authorizationBinds = bindingIsAuthorization(panel);

  return (
    <Card
      title={t("live.risk")}
      meta={
        <div className="flex flex-wrap items-center gap-2">
          <LiveTag />
          <Tag>{risk?.policy_id ?? "—"}</Tag>
        </div>
      }
    >
      {!resolved ? (
        <Statement tone="ATTENTION">{risk?.detail ?? t("live.risk.notVerified")}</Statement>
      ) : null}

      <div className="mt-3 grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3 xl:grid-cols-4">
        <MetricBlock
          label={t("live.risk.verifiedEquity")}
          value={<Money value={risk?.verified_equity} />}
          size="sm"
        />
        <MetricBlock
          label={t("live.risk.targetGross")}
          value={<Money value={risk?.target_gross} />}
          size="sm"
        />
        <MetricBlock
          label={t("live.risk.hardGross")}
          value={<Money value={risk?.hard_gross} />}
          size="sm"
        />
        <MetricBlock
          label={t("live.risk.absoluteCap")}
          value={<Money value={risk?.exposure_bound} />}
          size="sm"
        />

        <Field label={t("live.risk.perSymbol")}>
          <Money value={risk?.per_symbol} />
        </Field>
        <Field label={t("live.risk.slot")}>
          <Money value={risk?.slot} />
        </Field>
        <Field label={t("live.risk.currentGross")}>
          <Money value={risk?.current_gross_exposure} />
        </Field>
        <Field label={t("live.risk.remaining")}>
          <Money value={risk?.remaining_gross_capacity} />
        </Field>

        <Field label={t("live.risk.dailyLossHalt")}>
          <Percent value={risk?.daily_loss_halt_fraction} />
        </Field>
        <Field label={t("live.risk.binding")}>
          {authorizationBinds === null ? (
            <Unknown />
          ) : (
            <span className="num">
              {authorizationBinds
                ? t("live.risk.binding.authorization")
                : t("live.risk.binding.balance")}
            </span>
          )}
        </Field>
        <Field label={t("live.risk.universe")}>
          {risk?.universe_size ? <span className="num">{risk.universe_size}</span> : <Unknown />}
        </Field>
        <Field label={t("live.ops.policyHash")} wrap className="col-span-2 sm:col-span-1">
          <Identifier value={risk?.policy_config_hash?.slice(0, 12)} />
        </Field>
      </div>

      <Note className="mt-4">
        {t("live.risk.fundingScale", { bound: risk?.capital_bound ? `$${risk.capital_bound}` : "$100" })}
      </Note>
      <Note className="mt-2">{t("live.risk.fundingExample")}</Note>
    </Card>
  );
}
