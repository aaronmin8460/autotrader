"use client";

/**
 * The market / strategy state panel — the answer to "what is the system doing
 * right now, and why".
 *
 * This is new at V3 and it is the point of the redesign. At V2 the EDA-1
 * session regime existed only as a small pill on the Equity Paper page, so the
 * Overview — the page an operator actually opens — could not answer the
 * question at all. It is now a first-class panel with the state word set large
 * and its real drivers beside it.
 *
 * EVERY FIGURE HERE IS THE RUNTIME'S OWN. `participate`, the reference symbol,
 * its completed-session close, that symbol's own long moving average, the
 * trailing-peak drawdown, the sessions observed and the router spec all come
 * from `/api/equity-paper/overview`'s regime panel. Nothing is recomputed in
 * the browser and nothing is inferred: when the panel cannot be read it says
 * so and prints no state.
 *
 * `PARTICIPATE` and `DEFENSIVE` are authoritative runtime words and are never
 * translated. In Korean a gloss is printed beneath them; it explains the word
 * and does not replace it.
 *
 * THE OVERLAY LINE IS LOAD-BEARING. Research programmes in this repository have
 * evaluated volatility, breadth and regime overlays. None is deployed. The
 * panel states that as a fact so that no reader can take a research result for
 * production state.
 */

import { useI18n } from "@/lib/i18n";
import { useFormat } from "@/lib/i18n/useFormat";
import type { PaperRegimePanel, PolicyPanel } from "@/lib/paper";

import { Card, EmptyState, Field, Status, Tag, cn } from "./ui";

export function MarketState({
  regime,
  policy,
  compact = false,
}: {
  regime: PaperRegimePanel | null;
  policy: PolicyPanel | null | undefined;
  /** The Overview form: state, target and drivers, no router row. */
  compact?: boolean;
}) {
  const { t, gloss } = useI18n();
  const format = useFormat();

  if (!regime || regime.participate === null || regime.participate === undefined) {
    return (
      <Card title={t("market.title")}>
        <EmptyState headline={t("market.unavailable")} />
      </Card>
    );
  }

  const on = regime.participate;
  // The word is the runtime's. The gloss is empty in English by construction.
  const word = on ? "PARTICIPATE" : "DEFENSIVE";
  const explanation = t(on ? "market.participateGloss" : "market.defensiveGloss");

  return (
    <Card
      title={t("market.title")}
      meta={
        <>
          <Tag title={t("market.routerHint")}>EDA-1</Tag>
          {regime.session_date ? (
            <span className="num text-meta text-ink-3">{format.dateLong(regime.session_date)}</span>
          ) : null}
        </>
      }
    >
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          <div className={cn("text-value font-semibold tracking-[-0.01em]", on ? "text-pos" : "text-ink-2")}>
            {word}
          </div>
          {gloss(word) ? (
            <div className="mt-1 text-meta text-ink-2">{gloss(word)}</div>
          ) : null}
          <div className="mt-1.5 max-w-[46ch] text-meta leading-snug text-ink-3">{explanation}</div>
        </div>

        {policy ? (
          <div className="text-end">
            <div className="eyebrow text-ink-3">{t("market.targetGross")}</div>
            <div className="num mt-1.5 text-value-sm font-semibold text-ink">
              {format.percent(policy.target_gross, 0)}
            </div>
          </div>
        ) : null}
      </div>

      <div
        className={cn(
          "mt-5 grid gap-x-5 gap-y-3.5",
          compact ? "grid-cols-2 sm:grid-cols-4" : "grid-cols-2 sm:grid-cols-3",
        )}
      >
        <Field label={t("market.reference")}>{regime.reference_symbol ?? "—"}</Field>
        <Field label={t("market.closeVsSma")} title={t("market.closeVsSmaHint")}>
          <span className="num">
            {regime.info_close?.toFixed(2) ?? "—"}
            <span className="text-ink-3"> / </span>
            {regime.info_sma?.toFixed(2) ?? "—"}
          </span>
        </Field>
        <Field label={t("market.trailingDrawdown")} title={t("market.trailingDrawdownHint")}>
          <span className="num">
            {regime.info_drawdown === null || regime.info_drawdown === undefined
              ? "—"
              : format.signedPercent(regime.info_drawdown)}
          </span>
        </Field>
        <Field label={t("market.sessionsObserved")}>
          <span className="num">{regime.sessions_observed ?? "—"}</span>
        </Field>
        <Field
          label={t("market.router")}
          title={t("market.routerHint")}
          className={compact ? "sm:col-span-2" : "sm:col-span-2"}
        >
          <span className="num">
            sma {regime.spec?.sma_sessions ?? "—"} · calm {regime.spec?.calm_threshold ?? "—"} · lag{" "}
            {regime.spec?.lag_sessions ?? "—"}
          </span>
        </Field>
      </div>

      {/* Not a placeholder for a future feature: a statement about this build. */}
      <div className="mt-5 flex flex-wrap items-center gap-x-3 gap-y-1.5 border-t border-subtle pt-3">
        <span className="eyebrow text-ink-3">{t("market.overlays")}</span>
        <Status tone="MUTED" title={t("market.overlaysNote")}>
          {t("market.overlaysNotDeployed")}
        </Status>
        <span className="max-w-[76ch] text-meta leading-snug text-ink-3">{t("market.overlaysNote")}</span>
      </div>
    </Card>
  );
}
