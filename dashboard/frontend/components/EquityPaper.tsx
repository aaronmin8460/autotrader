"use client";

/**
 * The Equity Paper page's panels.
 *
 * The ordering is by what a reader could get wrong. The header strip says which
 * of the dashboard's records this is before anything is read — the shadow pages
 * and this one look alike and mean opposite things — and states the deployed
 * policy's figures as the runtime announced them: target, hard caps, halt,
 * fractional mode. Then target versus actual per symbol, joined from the
 * runtime's own recorded decisions and the broker's own positions. Then the
 * policy in full, the paper order log, and safety.
 *
 * `PARTICIPATE`, `DEFENSIVE`, `LONG`, `FLAT`, `BUY`, `SELL`, `HOLD`, the
 * policy id, the config hash and every broker status are the runtime's own
 * words and are printed identically in both locales. In Korean a gloss appears
 * beside the regime word; it never replaces it.
 *
 * There is no control anywhere on this page. No start, no stop, no advance the
 * stage, no cancel, no resize — and no endpoint behind any of them.
 */

import { money, percent, signTone } from "@/lib/format";
import type { ChartRange, ChartSeries } from "@/lib/charts";
import { useI18n } from "@/lib/i18n";
import { useFormat } from "@/lib/i18n/useFormat";
import type {
  PaperExposurePanel,
  PaperOrderRow,
  PaperRegimePanel,
  PaperSafetyPanel,
  PaperServicePanel,
  PolicyPanel,
} from "@/lib/paper";
import type { TargetVsActualRow } from "@/lib/portfolio";

import { Sparkline } from "./charts/Sparkline";
import {
  Card,
  DataTable,
  EmptyState,
  Field,
  Pill,
  SegmentedTimeRange,
  Status,
  Surface,
  Tag,
  Td,
  Th,
  Tr,
  cn,
  toneText,
} from "./ui";

function serviceStatus(service: PaperServicePanel | null): {
  word: string;
  tone: "POSITIVE" | "ATTENTION" | "NEGATIVE" | "MUTED";
} {
  if (!service) return { word: "UNAVAILABLE", tone: "MUTED" };
  if (service.unavailable_reason) return { word: service.unavailable_reason, tone: "NEGATIVE" };
  if (service.running && !service.stale) return { word: "RUNNING", tone: "POSITIVE" };
  if (service.running) return { word: "STALE", tone: "ATTENTION" };
  return { word: "STOPPED", tone: "NEGATIVE" };
}

export function PaperHeaderStrip({
  service,
  regime,
  policy,
  generatedAt,
}: {
  service: PaperServicePanel | null;
  regime: PaperRegimePanel | null;
  policy: PolicyPanel | null | undefined;
  generatedAt: string | null;
}) {
  const { t, gloss } = useI18n();
  const format = useFormat();
  const status = serviceStatus(service);
  const participate = regime?.participate;
  const word = participate ? "PARTICIPATE" : "DEFENSIVE";

  return (
    <Surface label="EDA-1 PAPER" className="px-4 py-3.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="text-value-sm leading-none font-semibold tracking-tight text-ink">
          EDA-1 PAPER
        </span>
        <Status tone={status.tone} size="md">
          {status.word}
        </Status>
        <Tag title={t("strategies.noRealMoneyHint")}>{t("strategies.noRealMoney")}</Tag>
        {participate === null || participate === undefined ? null : (
          <span className="inline-flex items-center gap-2">
            <Pill
              tone={participate ? "POSITIVE" : "MUTED"}
              title={t(participate ? "market.participateGloss" : "market.defensiveGloss")}
            >
              {word}
            </Pill>
            {gloss(word) ? <span className="text-meta text-ink-3">{gloss(word)}</span> : null}
          </span>
        )}
        {service?.last_cycle_at ? (
          <span className="num ms-auto text-meta text-ink-3">
            {t("strategies.lastCycle")} {format.stamp(service.last_cycle_at, generatedAt)} UTC
          </span>
        ) : null}
      </div>

      <div className="mt-4 grid grid-cols-2 gap-x-5 gap-y-3.5 sm:grid-cols-4 xl:grid-cols-8">
        <Field label={t("strategies.policy")} className="xl:col-span-2" title={policy?.note ?? undefined}>
          <span className={cn(policy && !policy.authoritative && "text-warn")}>
            {policy?.policy_id ?? service?.sizing_policy ?? "—"}
          </span>
        </Field>
        <Field label="config_hash" title="Logged on every start and every cycle.">
          <span className="num">{policy?.config_hash ?? service?.sizing_config_hash ?? "—"}</span>
        </Field>
        <Field label={t("strategies.universe")}>
          <span className="num">U{service?.execution_universe?.length ?? "—"}</span>
          <span className="text-ink-3">
            {" "}
            · {t("strategies.stage")} {service?.stage ?? "—"}
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
        <Field label={t("risk.dailyLoss")}>
          <span className="num">{policy ? percent(policy.daily_loss_halt, 0) : "—"}</span>
          <span className="text-ink-3"> · </span>
          {policy ? (policy.fractional ? "ON" : "OFF") : "—"}
        </Field>
      </div>
    </Surface>
  );
}

const ACTION_TONE: Record<string, "POSITIVE" | "NEGATIVE" | "MUTED"> = {
  BUY: "POSITIVE",
  SELL: "NEGATIVE",
  HOLD: "MUTED",
};

export function TargetVsActual({
  rows,
  sparklines,
  sparkRange,
  onSparkRange,
  onSelect,
  generatedAt,
  brokerAvailable,
}: {
  rows: TargetVsActualRow[];
  sparklines: Readonly<Record<string, ChartSeries>>;
  sparkRange: ChartRange;
  onSparkRange: (range: ChartRange) => void;
  onSelect: (symbol: string) => void;
  generatedAt: string | null;
  brokerAvailable: boolean;
}) {
  const { t } = useI18n();
  const format = useFormat();

  if (rows.length === 0) {
    return (
      <Card title={t("portfolio.targetVsActual")}>
        <EmptyState headline={t("empty.noDecision")} detail={t("empty.noDecisionDetail")} />
      </Card>
    );
  }

  return (
    <Card
      title={t("portfolio.targetVsActual")}
      meta={
        <>
          <Tag title={t("portfolio.targetVsActualHint")}>{t("portfolio.recordedVsBroker")}</Tag>
          {brokerAvailable ? null : <Pill tone="ATTENTION">{t("portfolio.brokerUnreadable")}</Pill>}
          <SegmentedTimeRange
            options={["1D", "5D", "1M"] as const}
            value={sparkRange}
            onChange={onSparkRange}
            label={t("chart.trendRange")}
          />
        </>
      }
      bodyClassName=""
    >
      <DataTable
        caption={t("portfolio.targetVsActual")}
        minWidth="min-w-[1000px]"
        head={
          <>
            <Th>{t("orders.col.symbol")}</Th>
            <Th title={t("drawer.currentStanceHint")}>{t("drawer.currentStance")}</Th>
            <Th align="right" title={t("portfolio.targetVsActualHint")}>
              {t("positions.col.target")}
            </Th>
            <Th align="right" title={t("drawer.portfolioWeightHint")}>
              {t("positions.col.weight")}
            </Th>
            <Th align="right">{t("drawer.targetValue")}</Th>
            <Th align="right">{t("drawer.marketValue")}</Th>
            <Th align="right" title={t("drawer.deltaVsTarget")}>
              {t("positions.col.delta")}
            </Th>
            <Th title={t("drawer.actionHint")}>{t("drawer.action")}</Th>
            <Th align="right">{t("drawer.lastDecision")}</Th>
            <Th align="right" title={t("positions.trendHint")}>
              {t("positions.col.trend", { range: sparkRange })}
            </Th>
          </>
        }
      >
        {rows.map((row) => {
          const spark = sparklines[row.symbol];
          return (
            <Tr
              key={row.symbol}
              onOpen={() => onSelect(row.symbol)}
              label={t("drawer.openSymbol", { symbol: row.symbol })}
            >
              <Td className="font-medium text-ink">{row.symbol}</Td>
              <Td>
                {row.stance ? (
                  <Pill tone={row.stance === "LONG" ? "POSITIVE" : "MUTED"}>{row.stance}</Pill>
                ) : (
                  <span className="text-ink-3">—</span>
                )}
              </Td>
              <Td
                numeric
                className={row.target_weight === null ? "text-ink-3" : "text-ink"}
                title={`${t("common.source")}: ${row.target_source}`}
              >
                {row.target_weight === null ? "N/A" : percent(row.target_weight, 2)}
              </Td>
              <Td numeric className="text-ink">
                {percent(row.actual_weight, 2)}
              </Td>
              <Td numeric className="text-ink-2">
                {money(row.target_value)}
              </Td>
              <Td numeric className="text-ink-2">
                {money(row.actual_value)}
              </Td>
              <Td numeric className={cn(toneText(signTone(row.delta_value)))}>
                {format.signedMoney(row.delta_value)}
                <span className="ms-1.5 text-meta text-ink-3">
                  {format.signedPercent(row.delta_weight)}
                </span>
              </Td>
              <Td>
                <Pill tone={ACTION_TONE[row.action] ?? "MUTED"}>{row.action}</Pill>
              </Td>
              <Td numeric className="text-ink-3">
                {row.last_decision_at ? format.stamp(row.last_decision_at, generatedAt) : "—"}
              </Td>
              <Td align="right">
                <span className="inline-flex items-center justify-end gap-2">
                  <Sparkline series={spark} />
                  <span
                    className={cn(
                      "num w-[52px] text-meta",
                      spark?.available ? toneText(signTone(spark.change_fraction)) : "text-ink-3",
                    )}
                  >
                    {spark?.available ? format.signedPercent(spark.change_fraction) : ""}
                  </span>
                </span>
              </Td>
            </Tr>
          );
        })}
      </DataTable>
      <p className="px-4 pt-2 pb-3 text-meta text-ink-3">
        {t("positions.col.quantity")}:{" "}
        {rows.map((row) => `${row.symbol} ${format.quantity(row.quantity)}`).join(" · ")}
      </p>
    </Card>
  );
}

export function PaperExposure({
  exposure,
  policy,
}: {
  exposure: PaperExposurePanel | null;
  policy: PolicyPanel | null | undefined;
}) {
  const { t } = useI18n();
  return (
    <Card
      title={t("strategies.policy")}
      meta={
        policy ? (
          <Tag tone={policy.authoritative ? undefined : "ATTENTION"}>
            {policy.source.replace(/_/g, " ").toLowerCase()}
          </Tag>
        ) : null
      }
    >
      <div className="grid gap-x-5 gap-y-3.5 sm:grid-cols-3">
        <Field label={t("market.targetGross")}>{exposure?.target_account_gross ?? "—"}</Field>
        <Field label={t("risk.cashReserve")}>{exposure?.cash_reserve_target ?? "—"}</Field>
        <Field label="fractional">{exposure ? (exposure.fractional_mode ? "ON" : "OFF") : "—"}</Field>
        <Field label={t("risk.perSymbol")}>{exposure?.per_symbol_cap ?? "—"}</Field>
        <Field label={t("risk.hardCap")} title="Account-wide. The crypto book counts against it.">
          {exposure?.total_account_cap ?? "—"}
        </Field>
        <Field label={t("risk.dailyLoss")}>{exposure?.daily_loss_halt ?? "—"}</Field>
        <Field
          label={t("risk.nonEquityPositions")}
          className="sm:col-span-3"
          title={t("risk.nonEquityHint")}
        >
          <span className="num">{exposure?.crypto_positions?.join("  ") || t("common.none")}</span>
        </Field>
      </div>
      <p className="mt-4 max-w-[94ch] text-meta leading-relaxed text-ink-3">
        {policy?.note ?? exposure?.equity_exposure_note}
      </p>
    </Card>
  );
}

export function PaperOrders({
  orders,
  generatedAt,
}: {
  orders: PaperOrderRow[];
  generatedAt: string | null;
}) {
  const { t } = useI18n();
  const format = useFormat();
  return (
    <Card
      title={t("orders.paperOrders")}
      meta={<Tag title={t("orders.pendingNotFilled")}>{t("orders.brokerTruth")}</Tag>}
      bodyClassName=""
    >
      {orders.length === 0 ? (
        <EmptyState headline={t("empty.noOrders")} detail={t("empty.noOrdersDetail")} />
      ) : (
        <>
          <DataTable
            caption={t("orders.paperOrders")}
            minWidth="min-w-[920px]"
            head={
              <>
                <Th>{t("orders.col.created")}</Th>
                <Th>{t("orders.col.symbol")}</Th>
                <Th>{t("orders.col.side")}</Th>
                <Th align="right">{t("orders.col.requested")}</Th>
                <Th align="right">{t("orders.col.approved")}</Th>
                <Th>{t("orders.col.risk")}</Th>
                <Th>{t("orders.col.intent")}</Th>
                <Th>{t("orders.col.broker")}</Th>
                <Th align="right">{t("orders.col.filled")}</Th>
                <Th align="right">{t("orders.col.avgPrice")}</Th>
              </>
            }
          >
            {orders.map((row) => (
              <Tr key={row.client_order_id}>
                <Td numeric align="left" className="text-ink-2">
                  {format.stamp(row.created_at, generatedAt)}
                </Td>
                <Td className="font-medium text-ink">{row.symbol}</Td>
                <Td
                  className={cn(
                    "text-meta font-medium tracking-[0.06em] uppercase",
                    row.side === "BUY" ? "text-pos" : "text-neg",
                  )}
                >
                  {row.side}
                </Td>
                <Td numeric>{row.requested_quantity}</Td>
                <Td numeric>{row.approved_quantity}</Td>
                <Td>
                  <span
                    className={cn(
                      "num text-meta",
                      row.risk_reason_code !== "APPROVED" && "text-warn",
                    )}
                  >
                    {row.risk_reason_code}
                  </span>
                </Td>
                <Td className="text-meta text-ink-2">{row.status}</Td>
                <Td>
                  <span className={cn("text-meta", row.broker_status !== "filled" && "text-warn")}>
                    {row.broker_status ?? "—"}
                  </span>
                </Td>
                <Td numeric>{row.filled_quantity ?? "—"}</Td>
                <Td numeric>{money(row.filled_average_price)}</Td>
              </Tr>
            ))}
          </DataTable>
          <p className="px-4 pt-2 pb-3 text-meta leading-snug text-ink-3">
            {t("orders.pendingNotFilled")}
          </p>
        </>
      )}
    </Card>
  );
}

export function PaperSafety({
  safety,
  generatedAt,
}: {
  safety: PaperSafetyPanel | null;
  generatedAt: string | null;
}) {
  const { t } = useI18n();
  const format = useFormat();
  const safe = safety?.account_safety === "SAFE";
  const clean = safety?.reconciliation_status === "CLEAN";
  return (
    <Card
      title={t("risk.operationalSafety")}
      meta={
        <Status tone={safe && clean ? "POSITIVE" : safe ? "ATTENTION" : "NEGATIVE"}>
          {safety?.account_safety ?? "—"}
        </Status>
      }
    >
      <div className="grid gap-x-5 gap-y-3.5 sm:grid-cols-2 lg:grid-cols-4">
        <Field label={t("risk.accountSafety")} title={t("risk.accountSafetySharedHint")}>
          <span className={cn("font-medium", safe ? "text-pos" : "text-neg")}>
            {safety?.account_safety ?? "—"}
          </span>
        </Field>
        <Field label={t("system.reconciliation")}>
          <span className={cn(clean ? "text-pos" : "text-warn")}>
            {safety?.reconciliation_status ?? "—"}
          </span>
        </Field>
        <Field label={t("common.updated")}>
          <span className="num">
            {safety?.reconciliation_at ? format.stamp(safety.reconciliation_at, generatedAt) : "—"}
          </span>
        </Field>
        <Field label={t("system.unresolved")}>
          <span className={cn("num", (safety?.reconciliation_unresolved ?? 0) > 0 && "text-neg")}>
            {safety?.reconciliation_unresolved ?? "—"}
          </span>
        </Field>
        <Field
          label={t("shadows.parity")}
          className="sm:col-span-2"
          title="A symbol whose two independently computed EDA-1 answers disagree is excluded from mutation for that bar. Counted since this store was created."
        >
          <span className={cn("num", (safety?.parity_mismatches ?? 0) > 0 && "text-warn")}>
            {safety?.parity_mismatches ?? 0}
          </span>
        </Field>
        <Field label={t("risk.blockedTargets")} className="sm:col-span-2">
          <span className="num">{safety?.risk_blocked_recent?.join("  ") || t("common.none")}</span>
        </Field>
      </div>
      {safety?.account_safety_reason ? (
        <p className="mt-4 max-w-[94ch] text-meta leading-relaxed text-ink-3">
          {safety.account_safety_reason}
        </p>
      ) : null}
    </Card>
  );
}
