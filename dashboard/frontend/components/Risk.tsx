"use client";

/**
 * Risk: the deployed policy's target and hard caps, against the broker's now.
 *
 * Every limit on this panel comes from the equity paper API's policy panel —
 * the running paper process's own policy, resolved in the allocation registry
 * — and every observation from the operational API's broker read. The join is
 * `buildRiskView`, tested on its own. **Nothing here is a constant**, which is
 * the whole reason this module exists: the operational API's own risk panel
 * still reports the crypto engine's own much lower per-symbol and account
 * ceilings and currently flags both as breached; rendering those would paint
 * a book sitting exactly where its policy aims as a failure, against lines
 * belonging to a different runtime.
 *
 * NO GAUGES. A number, its target, its hard cap, and one thin rail. Three
 * words carry the verdict: ON TARGET is green, NEAR CAP is amber, OVER CAP is
 * red. A book sitting where its policy aims is healthy and looks it; only an
 * actual hard-cap breach is painted as a fault.
 *
 * The verdict words are the risk engine's own vocabulary and are identical in
 * both locales; the row labels and explanations are translated.
 */

import { RISK_STATUS_KEY } from "./AccountSummary";
import { useI18n } from "@/lib/i18n";
import type { MessageKey } from "@/lib/i18n";
import { useFormat } from "@/lib/i18n/useFormat";
import { money, percent } from "@/lib/format";
import type { RiskRow, RiskView } from "@/lib/risk";
import type { RiskLimit } from "@/lib/types";

import { ExposureRail } from "./charts/ExposureRail";
import { Bar, Card, SectionHeader, Status, Tag, cn, useUnavailableLabel } from "./ui";

const ROW_LABEL: Record<RiskRow["key"], MessageKey> = {
  symbol: "risk.perSymbol",
  total: "risk.totalExposure",
  cash: "risk.cashReserve",
};

const ROW_DETAIL: Record<RiskRow["key"], MessageKey> = {
  symbol: "risk.perSymbolDetail",
  total: "risk.totalExposureDetail",
  cash: "risk.cashReserveDetail",
};

function Row({ row, dense }: { row: RiskRow; dense: boolean }) {
  const { t } = useI18n();
  const known = row.current !== null;
  return (
    <div className={cn(dense ? "py-2.5" : "py-3.5", "first:pt-0 last:pb-0")}>
      <div className="flex items-baseline justify-between gap-3">
        <div className="flex min-w-0 items-baseline gap-2">
          <span className="hint truncate text-table text-ink-2" title={t(ROW_DETAIL[row.key])}>
            {t(ROW_LABEL[row.key])}
          </span>
          {row.subject ? <Tag>{row.subject}</Tag> : null}
        </div>
        <Status tone={row.tone}>{t(RISK_STATUS_KEY[row.status])}</Status>
      </div>

      <dl className="num mt-2 grid grid-cols-3 gap-x-3 text-table">
        <div>
          <dt className="eyebrow text-ink-3">{t("risk.current")}</dt>
          <dd className={cn("mt-1 text-value-sm font-semibold", known ? "text-ink" : "text-ink-3")}>
            {known ? percent(row.current, 2) : "—"}
          </dd>
          {dense ? null : (
            <dd className="text-eyebrow text-ink-3">{known ? money(row.currentValue) : ""}</dd>
          )}
        </div>
        <div>
          <dt className="eyebrow text-ink-3">{t("risk.target")}</dt>
          <dd className="mt-1 text-value-sm text-ink-2">
            {row.target === null
              ? "—"
              : `${row.key === "symbol" ? "~" : ""}${percent(row.target, 2)}`}
          </dd>
        </div>
        <div>
          <dt className="eyebrow text-ink-3">{t("risk.hardCap")}</dt>
          <dd className="mt-1 text-value-sm text-ink-2">
            {row.cap === null ? t("risk.none") : percent(row.cap, 2)}
          </dd>
          {dense ? null : (
            <dd className="text-eyebrow text-ink-3">
              {row.capValue !== null ? money(row.capValue) : ""}
            </dd>
          )}
        </div>
      </dl>

      {row.rail ? (
        <ExposureRail
          current={row.rail.current}
          target={row.rail.target}
          cap={row.rail.cap}
          tone={row.tone}
          compact={dense}
        />
      ) : null}
    </div>
  );
}

function DailyLoss({ limit }: { limit: RiskLimit }) {
  const { t } = useI18n();
  const unavailableLabel = useUnavailableLabel();
  const known = limit.used_value.available && limit.used_fraction !== null;
  return (
    <div className="pt-3.5">
      <div className="flex items-baseline justify-between gap-3">
        <span className="hint truncate text-table text-ink-2" title={t("risk.dailyLossHint")}>
          {t("risk.dailyLoss")}
        </span>
        <span className="num shrink-0 text-table whitespace-nowrap">
          <span className={cn(known ? (limit.breached ? "text-neg" : "text-ink") : "text-ink-3")}>
            {known ? percent(limit.used_fraction) : "—"}
          </span>
          <span className="text-ink-3"> / {percent(limit.limit_fraction, 0)}</span>
        </span>
      </div>
      <div className="mt-2">
        <Bar value={limit.utilization} breached={limit.breached} />
      </div>
      <div className="mt-1.5 text-meta whitespace-nowrap text-ink-3">
        {known ? (
          <span className="num">
            {money(limit.used_value.value)} {t("common.of")} {money(limit.limit_value.value)}
          </span>
        ) : (
          unavailableLabel(limit.used_value.unavailable_reason)
        )}
      </div>
    </div>
  );
}

/** The panel's policy tag: the runtime's own policy id, never a constant. */
export function PolicyTag({ view }: { view: RiskView }) {
  const { t } = useI18n();
  if (!view.policyId) return <Tag tone="ATTENTION">{t("risk.policyUnavailable")}</Tag>;
  return (
    <Tag
      title={
        t("risk.policyHint", {
          policy: view.policyId,
          hash: view.policyHash ? ` (${view.policyHash})` : "",
        }) + (view.authoritative ? "" : t("risk.policyNotAuthoritative"))
      }
      tone={view.authoritative ? undefined : "ATTENTION"}
    >
      {view.policyId}
    </Tag>
  );
}

export function Risk({
  view,
  dense = false,
  title,
  showNote = true,
}: {
  view: RiskView;
  /** The Overview's condensed form: no dollar sub-lines, tighter rows. */
  dense?: boolean;
  title?: string;
  showNote?: boolean;
}) {
  const { t } = useI18n();
  const note = !view.policyId
    ? t("risk.noPolicyNote")
    : view.authoritative
      ? t("risk.authoritativeNote", { policy: view.policyId })
      : t("risk.fallbackNote");

  return (
    <Card
      title={title ?? t("risk.limits")}
      meta={<PolicyTag view={view} />}
      bodyClassName="px-4 pb-3.5"
    >
      {view.rows.length ? (
        <div className="divide-y divide-subtle">
          {view.rows.map((row) => (
            <Row key={row.key} row={row} dense={dense} />
          ))}
        </div>
      ) : (
        <p className="text-table leading-snug text-ink-3">{note}</p>
      )}
      {view.dailyLoss ? (
        <div className="mt-3 border-t border-subtle">
          <DailyLoss limit={view.dailyLoss} />
        </div>
      ) : null}
      {view.rows.length && showNote ? (
        <p className="mt-3.5 border-t border-subtle pt-3 text-meta leading-relaxed text-ink-3">
          {note} {t("risk.cryptoNote")}
        </p>
      ) : null}
    </Card>
  );
}

/** The heading used above the risk group on a page that has other groups. */
export function RiskHeader({ view }: { view: RiskView }) {
  const { t } = useI18n();
  const format = useFormat();
  void format;
  return <SectionHeader title={t("risk.limits")} meta={<PolicyTag view={view} />} />;
}
