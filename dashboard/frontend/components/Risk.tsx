/**
 * Risk: the deployed policy's target and hard caps, against the broker's now.
 *
 * Every limit on this card comes from the equity paper API's policy panel -
 * the running paper process's own policy, resolved in the allocation registry
 * - and every observation from the operational API's broker read. The join
 * is `buildRiskView`, tested on its own. Nothing here is a constant.
 *
 * Three words carry the verdict: ON TARGET is green, NEAR CAP is amber, OVER
 * CAP is red. A book sitting where its policy aims is healthy and looks it;
 * only an actual hard-cap breach is painted as a fault.
 *
 * The bars are read-only, like everything else here. There is no control that
 * edits a limit, and no endpoint that would accept one.
 */

import { money, percent, unavailableLabel } from "@/lib/format";
import type { RiskRow, RiskView } from "@/lib/risk";
import type { RiskLimit } from "@/lib/types";

import { ExposureRail } from "./charts/ExposureRail";
import { Bar, Card, Status, Tag, cn } from "./ui";

function Row({ row }: { row: RiskRow }) {
  const known = row.current !== null;
  return (
    <div className="py-3 first:pt-0 last:pb-0">
      <div className="flex items-baseline justify-between gap-3">
        <div className="flex min-w-0 items-baseline gap-2">
          <span className="hint truncate text-[12.5px] text-ink-2" title={row.detail}>
            {row.label}
          </span>
          {row.subject ? <Tag>{row.subject}</Tag> : null}
        </div>
        <Status tone={row.tone}>{row.status}</Status>
      </div>

      <dl className="num mt-2 grid grid-cols-3 gap-x-3 text-[12px]">
        <div>
          <dt className="eyebrow text-ink-3">Current</dt>
          <dd className={cn("mt-0.5 text-[14px] font-semibold", known ? "text-ink" : "text-ink-3")}>
            {known ? percent(row.current, 2) : "—"}
          </dd>
          <dd className="text-[10.5px] text-ink-3">{known ? money(row.currentValue) : ""}</dd>
        </div>
        <div>
          <dt className="eyebrow text-ink-3">Target</dt>
          <dd className="mt-0.5 text-[14px] text-ink-2">
            {row.target === null ? "—" : `${row.key === "symbol" ? "~" : ""}${percent(row.target, 2)}`}
          </dd>
        </div>
        <div>
          <dt className="eyebrow text-ink-3">Hard cap</dt>
          <dd className="mt-0.5 text-[14px] text-ink-2">{row.cap === null ? "none" : percent(row.cap, 2)}</dd>
          <dd className="text-[10.5px] text-ink-3">{row.capValue !== null ? money(row.capValue) : ""}</dd>
        </div>
      </dl>

      {row.rail ? <ExposureRail current={row.rail.current} target={row.rail.target} cap={row.rail.cap} tone={row.tone} /> : null}
    </div>
  );
}

function DailyLoss({ limit }: { limit: RiskLimit }) {
  const known = limit.used_value.available && limit.used_fraction !== null;
  return (
    <div className="pt-3">
      <div className="flex items-baseline justify-between gap-3">
        <span className="hint truncate text-[12.5px] text-ink-2" title="Loss against the stored UTC-day baseline equity. At the halt, entries pause and exits stay free.">
          Daily loss halt
        </span>
        <span className="num shrink-0 text-[12.5px] whitespace-nowrap">
          <span className={cn(known ? (limit.breached ? "text-neg" : "text-ink") : "text-ink-3")}>
            {known ? percent(limit.used_fraction) : "—"}
          </span>
          <span className="text-ink-3"> / {percent(limit.limit_fraction, 0)}</span>
        </span>
      </div>
      <div className="mt-2">
        <Bar value={limit.utilization} breached={limit.breached} />
      </div>
      <div className="mt-1.5 text-[11px] whitespace-nowrap text-ink-3">
        {known ? (
          <span className="num">
            {money(limit.used_value.value)} of {money(limit.limit_value.value)}
          </span>
        ) : (
          unavailableLabel(limit.used_value.unavailable_reason)
        )}
      </div>
    </div>
  );
}

export function Risk({ view }: { view: RiskView }) {
  const meta = view.policyId ? (
    <Tag
      title={`Sizing policy ${view.policyId}${view.policyHash ? ` (${view.policyHash})` : ""}, read from the running paper process.${view.authoritative ? "" : " Not authoritative: fallback figures."}`}
      tone={view.authoritative ? undefined : "ATTENTION"}
    >
      {view.policyId}
    </Tag>
  ) : (
    <Tag tone="ATTENTION">Policy unavailable</Tag>
  );

  return (
    <Card title="Risk limits" meta={meta} bodyClassName="px-4 pb-3.5">
      {view.rows.length ? (
        <div className="divide-y divide-line">
          {view.rows.map((row) => (
            <Row key={row.key} row={row} />
          ))}
        </div>
      ) : (
        <p className="text-[12px] leading-snug text-ink-3">{view.note}</p>
      )}
      {view.dailyLoss ? (
        <div className="mt-3 border-t border-line">
          <DailyLoss limit={view.dailyLoss} />
        </div>
      ) : null}
      {view.rows.length ? (
        <p className="mt-3 border-t border-line pt-3 text-[11px] leading-snug text-ink-3">
          {view.note} Crypto entries are additionally gated by the crypto runtime&apos;s own, lower
          account-gross ceiling: while the equity book sits at target the crypto book can only shrink.
        </p>
      ) : null}
    </Card>
  );
}
