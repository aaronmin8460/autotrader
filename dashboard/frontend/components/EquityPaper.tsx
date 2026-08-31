"use client";

/**
 * The Equity Paper page's components.
 *
 * The ordering is by what a reader could get wrong. The banner says which of
 * the dashboard's three records this is before anything is read - because the
 * shadow page and this one look alike and mean opposite things. The service
 * card says which stage is live and which sizing policy is frozen, because
 * "EDA-1 is running" is ambiguous until you know it is running on one symbol
 * or on ten. Exposure comes next and is stated ACCOUNT-WIDE, since the ceiling
 * it is measured against is an account ceiling and the crypto book is using
 * part of it. Targets, then orders, then safety.
 *
 * There is no control anywhere on this page. No start, no stop, no advance the
 * stage, no cancel, no resize - and no endpoint behind any of them.
 */

import type {
  PaperExposurePanel,
  PaperOrderRow,
  PaperRegimePanel,
  PaperSafetyPanel,
  PaperServicePanel,
  PaperTargetRow,
} from "@/lib/paper";

import { Card, Dot, Field, Pill, Tag, Td, Th, cn } from "./ui";

export function PaperBanner() {
  return (
    <section
      aria-label="What this page is"
      className="rounded-card border border-line bg-surface px-4 py-3"
    >
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-[13px] leading-none font-semibold text-ink">
          Equity EDA-1 — Alpaca Paper
        </span>
        <Tag title="Orders on this page were really submitted to a paper brokerage account. No real money is involved and there is no live path in this system.">
          Paper · no real money
        </Tag>
      </div>
      <p className="mt-2 max-w-[92ch] text-[12px] leading-relaxed text-ink-2">
        These are <strong className="font-medium text-ink">real broker facts</strong>: real paper
        orders, real fills, real positions, on the same account the crypto book trades. They are
        not the Equity Shadow&rsquo;s hypothetical curve and the two must never be added together —
        the shadow records what an engine <em>would</em> have done and can submit nothing.
      </p>
    </section>
  );
}

export function PaperService({
  service,
  generatedAt,
}: {
  service: PaperServicePanel | null;
  generatedAt: string | null;
}) {
  const running = Boolean(service?.running) && !service?.stale;
  return (
    <Card
      title="Service"
      meta={
        <>
          <Dot tone={running ? "POSITIVE" : service?.running ? "ATTENTION" : "NEGATIVE"} />
          <span className="text-[11px] leading-none text-ink-2">
            {service?.unavailable_reason
              ? service.unavailable_reason
              : running
                ? "Running"
                : service?.running
                  ? "No recent cycle"
                  : "Stopped"}
          </span>
        </>
      }
    >
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="Environment" title="There is no live path in this repository.">
          <span className="font-medium">{service?.environment ?? "—"}</span>
        </Field>
        <Field
          label="Rollout stage"
          title="A = SPY. B = SPY, QQQ, IWM. C = all ten. The decision universe is always all ten."
        >
          {service?.stage ?? "—"}
        </Field>
        <Field label="Execution universe" title="The symbols this stage may mutate.">
          {service?.execution_universe?.join(", ") || "—"}
        </Field>
        <Field label="Decision universe" title="EDA-1 is evaluated for all ten on every bar.">
          {service?.decision_universe?.length ?? 0} symbols
        </Field>
        <Field
          label="Sizing policy"
          title="Frozen by the shared-account sizing study. The runtime has no default and refuses to start without one."
        >
          {service?.sizing_policy ?? "—"}
        </Field>
        <Field label="Policy hash" title="Logged on every start and every cycle.">
          <span className="num">{service?.sizing_config_hash ?? "—"}</span>
        </Field>
        <Field label="Last cycle" title="Cycles land every fifteen minutes during a session.">
          <span className="num">{service?.last_cycle_at ?? "—"}</span>
        </Field>
        <Field
          label="Unsettled intents"
          title="An intent with no settled broker outcome blocks every new target until reconciliation settles it."
        >
          <span className={cn("num", (service?.unresolved_intents ?? 0) > 0 && "text-neg")}>
            {service?.unresolved_intents ?? "—"}
          </span>
        </Field>
      </div>
      {generatedAt ? (
        <p className="mt-3 text-[11px] text-ink-3">Read at {generatedAt}</p>
      ) : null}
    </Card>
  );
}

export function PaperRegime({ regime }: { regime: PaperRegimePanel | null }) {
  const on = regime?.participate === true;
  return (
    <Card
      title="EDA-1 regime"
      meta={
        <Pill tone={on ? "POSITIVE" : "MUTED"}>{on ? "Participate" : "Defensive"}</Pill>
      }
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Session" >{regime?.session_date ?? "—"}</Field>
        <Field label="Reference">{regime?.reference_symbol ?? "—"}</Field>
        <Field
          label="Close vs SMA"
          title="Participation requires the reference symbol's completed-session close above its own long moving average."
        >
          <span className="num">
            {regime?.info_close?.toFixed(2) ?? "—"} / {regime?.info_sma?.toFixed(2) ?? "—"}
          </span>
        </Field>
        <Field label="Trailing drawdown" title="And a trailing-peak drawdown above the calm threshold.">
          <span className="num">
            {regime?.info_drawdown !== null && regime?.info_drawdown !== undefined
              ? `${(regime.info_drawdown * 100).toFixed(2)}%`
              : "—"}
          </span>
        </Field>
        <Field label="Sessions observed">
          <span className="num">{regime?.sessions_observed ?? "—"}</span>
        </Field>
        <Field label="Router" title="Zero fitted parameters; both are external conventions.">
          <span className="num">
            sma {regime?.spec?.sma_sessions ?? "—"} · calm {regime?.spec?.calm_threshold ?? "—"} ·
            lag {regime?.spec?.lag_sessions ?? "—"}
          </span>
        </Field>
      </div>
    </Card>
  );
}

export function PaperExposure({ exposure }: { exposure: PaperExposurePanel | null }) {
  return (
    <Card title="Exposure — account-wide">
      <div className="grid gap-4 sm:grid-cols-3">
        <Field label="Per-symbol cap">{exposure?.per_symbol_cap ?? "—"}</Field>
        <Field label="Total account cap" title="Account-wide. The crypto book counts against it.">
          {exposure?.total_account_cap ?? "—"}
        </Field>
        <Field label="UTC-day loss halt">{exposure?.daily_loss_halt ?? "—"}</Field>
        <Field label="Crypto positions" className="sm:col-span-3">
          <span className="num">{exposure?.crypto_positions?.join("  ") || "none"}</span>
        </Field>
        <Field label="Equity positions" className="sm:col-span-3">
          <span className="num">{exposure?.equity_positions?.join("  ") || "none"}</span>
        </Field>
      </div>
      <p className="mt-3 max-w-[92ch] text-[11px] leading-relaxed text-ink-3">
        {exposure?.equity_exposure_note}
      </p>
    </Card>
  );
}

export function PaperTargets({ targets }: { targets: PaperTargetRow[] }) {
  return (
    <Card title="Targets" bodyClassName="overflow-x-auto">
      <table className="w-full min-w-[860px] border-collapse text-[12.5px]">
        <thead>
          <tr>
            <Th>Symbol</Th>
            <Th>In stage</Th>
            <Th>Bar</Th>
            <Th>Regime</Th>
            <Th>EDA-1</Th>
            <Th>Stance</Th>
            <Th>V3</Th>
            <Th>Held</Th>
            <Th>Last risk</Th>
          </tr>
        </thead>
        <tbody>
          {targets.map((row) => (
            <tr key={row.symbol} className="border-t border-line">
              <Td>
                <span className="font-medium text-ink">{row.symbol}</span>
              </Td>
              <Td>
                {row.in_execution_universe ? (
                  <Pill tone="POSITIVE">yes</Pill>
                ) : (
                  <span className="text-ink-3">decision only</span>
                )}
              </Td>
              <Td>
                <span className="num text-ink-3">{row.bar_timestamp ?? "—"}</span>
              </Td>
              <Td>{row.participate === null ? "—" : row.participate ? "PARTICIPATE" : "DEFENSIVE"}</Td>
              <Td>{row.eda1_signal ?? "—"}</Td>
              <Td>
                <span className="num">{row.eda1_stance ?? "—"}</span>
              </Td>
              <Td>
                <span className="text-ink-3">{row.v3_signal ?? "—"}</span>
              </Td>
              <Td>
                <span className="num">{row.actual_quantity}</span>
              </Td>
              <Td>
                <span
                  className={cn(
                    "num",
                    row.last_risk_reason && row.last_risk_reason !== "APPROVED" && "text-warn",
                  )}
                >
                  {row.last_risk_reason ?? "—"}
                </span>
              </Td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

export function PaperOrders({ orders }: { orders: PaperOrderRow[] }) {
  return (
    <Card
      title="Paper orders"
      meta={<Tag title="Accepted is not filled. A broker status other than filled means the order exists and has not settled.">Broker truth</Tag>}
      bodyClassName="overflow-x-auto"
    >
      {orders.length === 0 ? (
        <p className="p-4 text-[12.5px] text-ink-3">No order intent has been created yet.</p>
      ) : (
        <table className="w-full min-w-[900px] border-collapse text-[12.5px]">
          <thead>
            <tr>
              <Th>Created</Th>
              <Th>Symbol</Th>
              <Th>Side</Th>
              <Th>Requested</Th>
              <Th>Approved</Th>
              <Th>Risk</Th>
              <Th>Intent</Th>
              <Th>Broker</Th>
              <Th>Filled</Th>
              <Th>Avg price</Th>
            </tr>
          </thead>
          <tbody>
            {orders.map((row) => (
              <tr key={row.client_order_id} className="border-t border-line">
                <Td>
                  <span className="num text-ink-3">{row.created_at ?? "—"}</span>
                </Td>
                <Td>
                  <span className="font-medium text-ink">{row.symbol}</span>
                </Td>
                <Td>{row.side}</Td>
                <Td>
                  <span className="num">{row.requested_quantity}</span>
                </Td>
                <Td>
                  <span className="num">{row.approved_quantity}</span>
                </Td>
                <Td>
                  <span className={cn("num", row.risk_reason_code !== "APPROVED" && "text-warn")}>
                    {row.risk_reason_code}
                  </span>
                </Td>
                <Td>{row.status}</Td>
                <Td>
                  <span className={cn(row.broker_status !== "filled" && "text-warn")}>
                    {row.broker_status ?? "—"}
                  </span>
                </Td>
                <Td>
                  <span className="num">{row.filled_quantity ?? "—"}</span>
                </Td>
                <Td>
                  <span className="num">{row.filled_average_price?.toFixed(2) ?? "—"}</span>
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

export function PaperSafety({ safety }: { safety: PaperSafetyPanel | null }) {
  const safe = safety?.account_safety === "SAFE";
  const clean = safety?.reconciliation_status === "CLEAN";
  return (
    <Card
      title="Safety"
      meta={<Dot tone={safe && clean ? "POSITIVE" : safe ? "ATTENTION" : "NEGATIVE"} />}
    >
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Field
          label="Account safety"
          title="Account-wide. An ambiguous order raised by either book halts this one."
        >
          <span className={cn("font-medium", safe ? "text-pos" : "text-neg")}>
            {safety?.account_safety ?? "—"}
          </span>
        </Field>
        <Field label="Reconciliation">
          <span className={cn(clean ? "text-pos" : "text-warn")}>
            {safety?.reconciliation_status ?? "—"}
          </span>
        </Field>
        <Field label="Reconciled at">
          <span className="num">{safety?.reconciliation_at ?? "—"}</span>
        </Field>
        <Field label="Unresolved">
          <span
            className={cn("num", (safety?.reconciliation_unresolved ?? 0) > 0 && "text-neg")}
          >
            {safety?.reconciliation_unresolved ?? "—"}
          </span>
        </Field>
        <Field
          label="Shadow/Paper mismatches"
          className="sm:col-span-2"
          title="A symbol whose two independently computed EDA-1 answers disagree is excluded from mutation for that bar."
        >
          <span className={cn("num", (safety?.parity_mismatches ?? 0) > 0 && "text-warn")}>
            {safety?.parity_mismatches ?? 0}
          </span>
        </Field>
        <Field label="Risk-blocked targets" className="sm:col-span-2">
          <span className="num">{safety?.risk_blocked_recent?.join("  ") || "none"}</span>
        </Field>
      </div>
      {safety?.account_safety_reason ? (
        <p className="mt-3 max-w-[92ch] text-[11px] leading-relaxed text-ink-3">
          {safety.account_safety_reason}
        </p>
      ) : null}
    </Card>
  );
}
