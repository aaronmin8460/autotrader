"use client";

/**
 * The Equity Paper page's components.
 *
 * The ordering is by what a reader could get wrong. The header strip says which
 * of the dashboard's records this is before anything is read - because the
 * shadow pages and this one look alike and mean opposite things - and states
 * the deployed policy's figures as the runtime announced them: target, hard
 * caps, halt, fractional mode. Then target versus actual per symbol, joined
 * from the runtime's own recorded decisions and the broker's own positions.
 * Then regime, orders, safety.
 *
 * There is no control anywhere on this page. No start, no stop, no advance the
 * stage, no cancel, no resize - and no endpoint behind any of them.
 */

import type { KeyboardEvent } from "react";

import type { ChartRange, ChartSeries } from "@/lib/charts";
import { money, percent, quantity, signTone, signedMoney, signedPercent, stampUtc } from "@/lib/format";
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
import { Card, Empty, Field, Pill, RangeSelector, Status, Tag, Td, Th, cn, toneText } from "./ui";

function serviceStatus(service: PaperServicePanel | null): { word: string; tone: "POSITIVE" | "ATTENTION" | "NEGATIVE" | "MUTED" } {
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
  const status = serviceStatus(service);
  const participate = regime?.participate;
  return (
    <section aria-label="Equity Paper runtime" className="card px-4 py-3.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="text-[15px] leading-none font-semibold tracking-tight text-ink">EDA-1 PAPER</span>
        <Status tone={status.tone} size="md">
          {status.word}
        </Status>
        <Tag title="Orders on this page were really submitted to a paper brokerage account. No real money is involved and there is no live path in this system.">
          Paper only · no real money
        </Tag>
        {participate === null || participate === undefined ? null : (
          <Pill tone={participate ? "POSITIVE" : "MUTED"} title="EDA-1's participation state for the current session. DEFENSIVE hands the stance back to V3.">
            {participate ? "PARTICIPATE" : "DEFENSIVE"}
          </Pill>
        )}
        {service?.last_cycle_at ? (
          <span className="num ml-auto text-[11px] text-ink-3">Last cycle {stampUtc(service.last_cycle_at, generatedAt)} UTC</span>
        ) : null}
      </div>
      <div className="mt-4 grid grid-cols-2 gap-x-5 gap-y-3.5 sm:grid-cols-4 xl:grid-cols-9">
        <Field label="Policy" className="xl:col-span-2" title={policy?.note ?? "The sizing policy the runtime announced when it started."}>
          <span className={cn(policy && !policy.authoritative && "text-warn")}>{policy?.policy_id ?? service?.sizing_policy ?? "—"}</span>
        </Field>
        <Field label="Policy hash" title="Logged on every start and every cycle.">
          <span className="num">{policy?.config_hash ?? service?.sizing_config_hash ?? "—"}</span>
        </Field>
        <Field label="Universe" title="Execution universe at the current rollout stage over the ten-symbol decision universe.">
          U{service?.execution_universe?.length ?? "—"}
          <span className="text-ink-3"> · stage {service?.stage ?? "—"}</span>
        </Field>
        <Field label="Decision clock" title="Decisions are taken on completed 15-minute bars during US regular sessions.">
          15m
        </Field>
        <Field label="Target gross" title="What the allocator aims for while every reserved slot is active.">
          <span className="num">{policy ? percent(policy.target_gross, 0) : "—"}</span>
        </Field>
        <Field label="Hard gross cap" title="New exposure-increasing orders are blocked above this level, both books counted.">
          <span className="num">{policy ? percent(policy.hard_gross_cap, 0) : "—"}</span>
        </Field>
        <Field label="Hard symbol cap" title="Risk refuses any order that would project one symbol past this share of equity.">
          <span className="num">{policy ? percent(policy.hard_symbol_cap, 0) : "—"}</span>
        </Field>
        <Field label="Daily halt · fractional" title="UTC-day loss halt: entries pause, exits stay free. Fractional: share targets are fractional quantities.">
          <span className="num">{policy ? percent(policy.daily_loss_halt, 0) : "—"}</span>
          <span className="text-ink-3"> · </span>
          {policy ? (policy.fractional ? "ON" : "OFF") : "—"}
        </Field>
      </div>
    </section>
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
  const open = (symbol: string) => (event: KeyboardEvent<HTMLTableRowElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onSelect(symbol);
    }
  };
  if (rows.length === 0) {
    return (
      <Card title="Target vs actual">
        <Empty headline="No decision recorded yet" detail="The runtime records a target per decided order on completed 15-minute bars." />
      </Card>
    );
  }
  return (
    <Card
      title="Target vs actual"
      meta={
        <>
          <Tag title="Target: the runtime's newest recorded decision per symbol, sized under the exposure it saw. Actual: the broker's position and market value now.">
            Recorded decision vs broker
          </Tag>
          {brokerAvailable ? null : <Pill tone="ATTENTION">Broker unreadable · actuals missing</Pill>}
          <RangeSelector options={["1D", "5D", "1M"] as const} value={sparkRange} onChange={onSparkRange} label="Trend range" />
        </>
      }
      bodyClassName=""
    >
      <div className="scroll-x">
        <table className="w-full min-w-[980px] border-collapse">
          <thead>
            <tr className="border-b border-line">
              <Th>Symbol</Th>
              <Th title="EDA-1's recorded stance on the latest completed bar.">Stance</Th>
              <Th align="right" title="The newest recorded decision's target weight. Zero for FLAT; N/A when no decision has been recorded.">Target wt</Th>
              <Th align="right" title="Broker market value over broker equity, same read.">Actual wt</Th>
              <Th align="right" title="Target weight times current account equity.">Target MV</Th>
              <Th align="right">Actual MV</Th>
              <Th align="right" title="Actual minus target. Inside the policy deadband no order is placed.">Delta</Th>
              <Th title="The decided side on the latest bar, or HOLD when no order was decided.">Action</Th>
              <Th align="right">Last decision</Th>
              <Th align="right" title="Price only. No signal or target is drawn.">Trend {sparkRange}</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const spark = sparklines[row.symbol];
              return (
                <tr
                  key={row.symbol}
                  role="button"
                  tabIndex={0}
                  aria-label={`Open ${row.symbol} detail`}
                  onClick={() => onSelect(row.symbol)}
                  onKeyDown={open(row.symbol)}
                  className="row-link border-b border-line/70 last:border-0 hover:bg-surface-2"
                >
                  <Td className="font-medium text-ink">{row.symbol}</Td>
                  <Td>
                    {row.stance ? (
                      <Pill tone={row.stance === "LONG" ? "POSITIVE" : "MUTED"}>{row.stance}</Pill>
                    ) : (
                      <span className="text-ink-3">—</span>
                    )}
                  </Td>
                  <Td numeric className={row.target_weight === null ? "text-ink-3" : "text-ink"} title={`Source: ${row.target_source}`}>
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
                    {signedMoney(row.delta_value)}
                    <span className="ml-1.5 text-[11px] text-ink-3">{signedPercent(row.delta_weight)}</span>
                  </Td>
                  <Td>
                    <Pill tone={ACTION_TONE[row.action] ?? "MUTED"}>{row.action}</Pill>
                  </Td>
                  <Td numeric className="text-ink-3">
                    {row.last_decision_at ? stampUtc(row.last_decision_at, generatedAt) : "—"}
                  </Td>
                  <Td align="right">
                    <span className="inline-flex items-center justify-end gap-2">
                      <Sparkline series={spark} />
                      <span className={cn("num w-[52px] text-[11px]", spark?.available ? toneText(signTone(spark.change_fraction)) : "text-ink-3")}>
                        {spark?.available ? signedPercent(spark.change_fraction) : ""}
                      </span>
                    </span>
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="px-4 pt-2 pb-3 text-[11px] text-ink-3">
        Quantities: {rows.map((row) => `${row.symbol} ${quantity(row.quantity)}`).join(" · ")}
      </p>
    </Card>
  );
}

export function PaperRegime({ regime }: { regime: PaperRegimePanel | null }) {
  const on = regime?.participate === true;
  return (
    <Card title="EDA-1 regime" meta={<Pill tone={on ? "POSITIVE" : "MUTED"}>{on ? "Participate" : "Defensive"}</Pill>}>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Session">{regime?.session_date ?? "—"}</Field>
        <Field label="Reference">{regime?.reference_symbol ?? "—"}</Field>
        <Field label="Close vs SMA" title="Participation requires the reference symbol's completed-session close above its own long moving average.">
          <span className="num">
            {regime?.info_close?.toFixed(2) ?? "—"} / {regime?.info_sma?.toFixed(2) ?? "—"}
          </span>
        </Field>
        <Field label="Trailing drawdown" title="And a trailing-peak drawdown above the calm threshold.">
          <span className="num">{regime?.info_drawdown !== null && regime?.info_drawdown !== undefined ? signedPercent(regime.info_drawdown) : "—"}</span>
        </Field>
        <Field label="Sessions observed">
          <span className="num">{regime?.sessions_observed ?? "—"}</span>
        </Field>
        <Field label="Router" title="Zero fitted parameters; both are external conventions.">
          <span className="num">
            sma {regime?.spec?.sma_sessions ?? "—"} · calm {regime?.spec?.calm_threshold ?? "—"} · lag {regime?.spec?.lag_sessions ?? "—"}
          </span>
        </Field>
      </div>
    </Card>
  );
}

export function PaperExposure({ exposure, policy }: { exposure: PaperExposurePanel | null; policy: PolicyPanel | null | undefined }) {
  return (
    <Card title="Policy & exposure" meta={policy ? <Tag tone={policy.authoritative ? undefined : "ATTENTION"}>{policy.source.replace(/_/g, " ").toLowerCase()}</Tag> : null}>
      <div className="grid gap-4 sm:grid-cols-3">
        <Field label="Target gross">{exposure?.target_account_gross ?? "—"}</Field>
        <Field label="Cash reserve target">{exposure?.cash_reserve_target ?? "—"}</Field>
        <Field label="Fractional mode">{exposure ? (exposure.fractional_mode ? "ON" : "OFF") : "—"}</Field>
        <Field label="Hard per-symbol cap">{exposure?.per_symbol_cap ?? "—"}</Field>
        <Field label="Hard account cap" title="Account-wide. The crypto book counts against it.">
          {exposure?.total_account_cap ?? "—"}
        </Field>
        <Field label="UTC-day loss halt">{exposure?.daily_loss_halt ?? "—"}</Field>
        <Field label="Non-equity positions" className="sm:col-span-3" title="Read from the crypto store, filtered to what is not one of the ten equities.">
          <span className="num">{exposure?.crypto_positions?.join("  ") || "none"}</span>
        </Field>
      </div>
      <p className="mt-3 max-w-[92ch] text-[11px] leading-relaxed text-ink-3">{policy?.note ?? exposure?.equity_exposure_note}</p>
    </Card>
  );
}

export function PaperOrders({ orders, generatedAt }: { orders: PaperOrderRow[]; generatedAt: string | null }) {
  return (
    <Card
      title="Paper orders"
      meta={<Tag title="Accepted is not filled. A broker status other than filled means the order exists and has not settled.">Broker truth</Tag>}
      bodyClassName=""
    >
      {orders.length === 0 ? (
        <Empty headline="No order intent has been created yet." />
      ) : (
        <div className="scroll-x">
          <table className="w-full min-w-[900px] border-collapse">
            <thead>
              <tr className="border-b border-line">
                <Th>Created</Th>
                <Th>Symbol</Th>
                <Th>Side</Th>
                <Th align="right">Requested</Th>
                <Th align="right">Approved</Th>
                <Th>Risk</Th>
                <Th>Intent</Th>
                <Th>Broker</Th>
                <Th align="right">Filled</Th>
                <Th align="right">Avg price</Th>
              </tr>
            </thead>
            <tbody>
              {orders.map((row) => (
                <tr key={row.client_order_id} className="border-b border-line/70 last:border-0 hover:bg-surface-2">
                  <Td numeric align="left" className="text-ink-2">
                    {stampUtc(row.created_at, generatedAt)}
                  </Td>
                  <Td className="font-medium text-ink">{row.symbol}</Td>
                  <Td className={cn("text-[11px] font-medium tracking-[0.06em] uppercase", row.side === "BUY" ? "text-pos" : "text-neg")}>{row.side}</Td>
                  <Td numeric>{row.requested_quantity}</Td>
                  <Td numeric>{row.approved_quantity}</Td>
                  <Td>
                    <span className={cn("num text-[11.5px]", row.risk_reason_code !== "APPROVED" && "text-warn")}>{row.risk_reason_code}</span>
                  </Td>
                  <Td className="text-[11.5px] text-ink-2">{row.status}</Td>
                  <Td>
                    <span className={cn("text-[11.5px]", row.broker_status !== "filled" && "text-warn")}>{row.broker_status ?? "—"}</span>
                  </Td>
                  <Td numeric>{row.filled_quantity ?? "—"}</Td>
                  <Td numeric>{money(row.filled_average_price)}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

export function PaperSafety({ safety, generatedAt }: { safety: PaperSafetyPanel | null; generatedAt: string | null }) {
  const safe = safety?.account_safety === "SAFE";
  const clean = safety?.reconciliation_status === "CLEAN";
  return (
    <Card title="Safety" meta={<Status tone={safe && clean ? "POSITIVE" : safe ? "ATTENTION" : "NEGATIVE"}>{safe && clean ? "Safe · clean" : safe ? "Safe" : "Blocked"}</Status>}>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="Account safety" title="Account-wide. An ambiguous order raised by either book halts this one.">
          <span className={cn("font-medium", safe ? "text-pos" : "text-neg")}>{safety?.account_safety ?? "—"}</span>
        </Field>
        <Field label="Reconciliation">
          <span className={cn(clean ? "text-pos" : "text-warn")}>{safety?.reconciliation_status ?? "—"}</span>
        </Field>
        <Field label="Reconciled at">
          <span className="num">{safety?.reconciliation_at ? stampUtc(safety.reconciliation_at, generatedAt) : "—"}</span>
        </Field>
        <Field label="Unresolved">
          <span className={cn("num", (safety?.reconciliation_unresolved ?? 0) > 0 && "text-neg")}>{safety?.reconciliation_unresolved ?? "—"}</span>
        </Field>
        <Field
          label="Shadow/Paper mismatches (cumulative)"
          className="sm:col-span-2"
          title="A symbol whose two independently computed EDA-1 answers disagree is excluded from mutation for that bar. Counted since this store was created."
        >
          <span className={cn("num", (safety?.parity_mismatches ?? 0) > 0 && "text-warn")}>{safety?.parity_mismatches ?? 0}</span>
        </Field>
        <Field label="Risk-blocked targets" className="sm:col-span-2">
          <span className="num">{safety?.risk_blocked_recent?.join("  ") || "none"}</span>
        </Field>
      </div>
      {safety?.account_safety_reason ? <p className="mt-3 max-w-[92ch] text-[11px] leading-relaxed text-ink-3">{safety.account_safety_reason}</p> : null}
    </Card>
  );
}
