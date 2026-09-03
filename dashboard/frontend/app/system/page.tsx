"use client";

/**
 * System — everything operational, in full, and away from the account pages.
 *
 * At V2 this material sat in the Overview's right rail at the same visual
 * weight as account exposure: API budget counters, processed-bar checkpoints,
 * the last failure event and the crypto runtime's trail. None of it was
 * deleted in the move; it is all here, with more room than it had.
 *
 * The service rows come from the service manager, once, and are the same rows
 * the top status bar renders. When that endpoint cannot be reached every unit
 * reads UNKNOWN — it never falls back to a store-derived guess, because the
 * only equity service that ever wrote to the operational store is the masked
 * legacy one.
 *
 * The dashboard API table below is a description of this deployment, not a
 * probe: it lists the five loopback processes, their prefixes, their identities
 * and their poll intervals. No credential appears anywhere on this page.
 */

import { AccountSafety } from "@/components/AccountSafety";
import { Reconciliation, SystemHealth } from "@/components/SystemHealth";
import { Runtimes } from "@/components/Runtime";
import { PageHeader } from "@/components/shell/PageHeader";
import { Card, DataTable, FreshnessIndicator, SectionHeader, Td, Th, Tr, freshnessOf } from "@/components/ui";
import { PAPER_POLL_INTERVAL_MS } from "@/lib/paper";
import { useDashboard } from "@/lib/dashboard";
import { useI18n } from "@/lib/i18n";
import { useFormat } from "@/lib/i18n/useFormat";

/**
 * The five read models this dashboard is built from.
 *
 * Static because it describes the deployment's shape rather than its state:
 * five processes, five identities, five records, every route a GET. The live
 * part — whether each answered — is the freshness column beside it.
 */
const RECORDS = [
  {
    key: "dashboard",
    record: "Broker account · crypto store",
    prefix: "/api/dashboard/*",
    identity: "autotrader",
  },
  {
    key: "equity-paper",
    record: "Equity paper · policy · merged orders · units",
    prefix: "/api/equity-paper/*",
    identity: "ateqpaper",
  },
  {
    key: "equity-shadow",
    record: "V3 + EDA-1 observation record",
    prefix: "/api/equity-shadow/*",
    identity: "atshadow",
  },
  {
    key: "equity-a1b-shadow",
    record: "A1-B U30 observation record",
    prefix: "/api/equity-a1b-shadow/*",
    identity: "ata1bshadow",
  },
  {
    key: "market-charts",
    record: "Provider price bars only",
    prefix: "/api/market-charts/*",
    identity: "ateqpaper",
  },
] as const;

export default function SystemPage() {
  const { t } = useI18n();
  const format = useFormat();
  const { account, paper, services, accountIntervalMs } = useDashboard();
  const data = account.data;

  const paperFreshness = freshnessOf(
    paper.data?.generated_at ?? null,
    paper.connected,
    PAPER_POLL_INTERVAL_MS,
  );

  return (
    <div className="space-y-5">
      <PageHeader
        title={t("system.title")}
        context={t("nav.detail.system")}
        actions={
          <FreshnessIndicator
            state={account.connected ? freshnessOf(data?.generated_at ?? null, true, accountIntervalMs).state : "OFFLINE"}
            ageSeconds={freshnessOf(data?.generated_at ?? null, account.connected, accountIntervalMs).ageSeconds}
            label={t("status.data")}
          />
        }
      />

      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-2">
        <SystemHealth
          components={data?.health ?? []}
          services={services}
          reconciliation={data?.reconciliation ?? null}
          generatedAt={data?.generated_at ?? null}
        />

        <Card title={t("system.dashboardApis")} bodyClassName="">
          <DataTable
            caption={t("system.dashboardApis")}
            minWidth="min-w-[620px]"
            head={
              <>
                <Th>{t("system.record")}</Th>
                <Th>{t("system.endpoint")}</Th>
                <Th>{t("system.identity")}</Th>
                <Th align="right">{t("system.pollInterval")}</Th>
              </>
            }
          >
            {RECORDS.map((row) => (
              <Tr key={row.key}>
                <Td className="text-ink-2">{row.record}</Td>
                <Td className="num text-ink">{row.prefix}</Td>
                <Td className="num text-ink-3">{row.identity}</Td>
                <Td numeric className="text-ink-3">
                  {row.key === "equity-paper" || row.key === "dashboard"
                    ? `${accountIntervalMs / 1000}s`
                    : row.key === "market-charts"
                      ? "TTL"
                      : `${PAPER_POLL_INTERVAL_MS / 1000}s`}
                </Td>
              </Tr>
            ))}
          </DataTable>
          <p className="px-4 pt-3 pb-2 text-meta leading-snug text-ink-3">
            {t("system.dashboardApisNote")}
          </p>
          <div className="border-t border-subtle px-4 py-3.5">
            <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
              <span className="eyebrow text-ink-3">{t("system.dataFreshness")}</span>
              <span className="flex flex-wrap items-center gap-4">
                <FreshnessIndicator
                  state={freshnessOf(data?.generated_at ?? null, account.connected, accountIntervalMs).state}
                  ageSeconds={freshnessOf(data?.generated_at ?? null, account.connected, accountIntervalMs).ageSeconds}
                  label="/api/dashboard"
                />
                <FreshnessIndicator
                  state={paperFreshness.state}
                  ageSeconds={paperFreshness.ageSeconds}
                  label="/api/equity-paper"
                />
              </span>
            </div>
            <p className="mt-2 num text-meta text-ink-3" title={format.stampFull(data?.generated_at)}>
              {t("status.lastSync")} {format.stamp(data?.generated_at, data?.generated_at)} UTC
            </p>
          </div>
          <div className="border-t border-subtle px-4 py-3.5">
            <p className="text-meta leading-snug text-ink-3">{t("system.accessNote")}</p>
          </div>
        </Card>
      </div>

      <div className="space-y-2">
        <SectionHeader title={t("risk.operationalSafety")} />
        <AccountSafety
          panel={data?.account_safety ?? null}
          budget={data?.api_budget ?? []}
          lastFailure={data?.last_failure ?? null}
          lastFailureAt={data?.last_failure_at ?? null}
          generatedAt={data?.generated_at ?? null}
        />
      </div>

      <div className="space-y-2">
        <SectionHeader title={t("system.reconciliation")} />
        <Card title={t("system.reconciliation")}>
          <Reconciliation
            reconciliation={data?.reconciliation ?? null}
            generatedAt={data?.generated_at ?? null}
          />
        </Card>
      </div>

      <div className="space-y-2">
        <SectionHeader title={t("system.runtimeTrail")} />
        <Runtimes
          panels={data?.runtimes ?? []}
          services={services}
          generatedAt={data?.generated_at ?? null}
        />
      </div>
    </div>
  );
}
