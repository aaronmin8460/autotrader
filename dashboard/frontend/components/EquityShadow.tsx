"use client";

/**
 * The Equity Shadow panels.
 *
 * Every one of them exists to answer a different question than the
 * operational dashboard answers, and the visual language says so before a
 * word is read: this page is banded, its headline figures are indexes rather
 * than currency, and the word SHADOW is never more than one card away.
 *
 * **The distinction this file is responsible for.** The operational page
 * shows an account that can trade. This page shows two engines that cannot.
 * If a reader ever has to work out which one they are looking at, this file
 * has failed - so the banner is unconditional, the hypothetical figures never
 * carry a currency symbol, and no panel here renders broker equity at all.
 *
 * There is no control on this page, and no endpoint behind one.
 */

import { percent, signedPercent } from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { useFormat } from "@/lib/i18n/useFormat";
import {
  type ComparisonPanel,
  type EngineHypothetical,
  type HypotheticalPanel,
  type RegimePanel,
  type ServicePanel,
  STATUS_REASON_LABELS,
  type SymbolRow,
  shadowStatusLabel,
  shadowTone,
} from "@/lib/shadow";

import { Card, Dot, Empty, Field, Pill, Tag, Td, Th, cn, toneText } from "./ui";

// --------------------------------------------------------------------------
// The banner
// --------------------------------------------------------------------------

/**
 * The unconditional statement of what this page is.
 *
 * Not a tooltip, not a footnote, and not conditional on any payload: it
 * renders before the first poll lands and stays rendered if every poll fails.
 * A safety label that can be absent when the backend is down is a safety
 * label that is missing exactly when someone is worried.
 */
export function ShadowBanner() {
  const { t } = useI18n();
  return (
    <section
      aria-label={t("sd.scope")}
      className="panel tint-observe px-5 py-4"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="text-body leading-none font-semibold tracking-tight text-ink">
          Equity — Shadow observation only
        </span>
        <Tag tone="SHADOW" title={t("sd.noMutation")}>
          Zero order mutation
        </Tag>
        <Tag tone="SHADOW" title={t("sd.enginesHint")}>
          V3 + EDA-1 side by side
        </Tag>
      </div>
      <p className="mt-2 max-w-[92ch] text-table leading-snug text-ink-2">
        These are decisions <strong className="font-semibold text-ink">recorded, not taken</strong>.
        The process behind this page has no execution path: no order has been placed, no position
        exists, and no figure here is broker account equity. Equity production remains disabled and
        masked; nothing on this page can change that.
      </p>
    </section>
  );
}

// --------------------------------------------------------------------------
// Service status
// --------------------------------------------------------------------------

export function ShadowService({
  service,
  generatedAt,
}: {
  service: ServicePanel | null;
  generatedAt: string | null;
}) {
  const { t } = useI18n();
  const format = useFormat();
  if (!service) {
    return (
      <Card title={t("sd.service")}>
        <Empty headline={t("sd.apiDown")} />
      </Card>
    );
  }

  const tone = shadowTone(service.status);
  const invariantOk = service.zero_order_invariant_holds;

  return (
    <Card
      tone="SHADOW"
      title={t("sd.service")}
      meta={
        <>
          <Pill tone={tone} emphasis>{shadowStatusLabel(service.status)}</Pill>
          <Tag title={service.mode}>{service.mode}</Tag>
        </>
      }
    >
      <p className="mb-4 text-table leading-snug text-ink-2">
        {STATUS_REASON_LABELS[service.status_reason] ?? service.status_reason}
      </p>

      <div className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3 lg:grid-cols-4">
        <Field label={t("sd.lastCycle")}>
          {service.last_cycle_at ? format.stamp(service.last_cycle_at, generatedAt) : "—"}
        </Field>
        <Field
          label={t("sd.nextCycle")}
          title={t("sd.nextCycleHint")}
        >
          {service.next_expected_cycle_at
            ? format.stamp(service.next_expected_cycle_at, generatedAt)
            : service.within_regular_session
              ? "—"
              : "Next regular session"}
        </Field>
        <Field label={t("sd.cyclesRecorded")}>
          <span className="num">{service.cycles_recorded}</span>
        </Field>
        <Field label={t("sd.symbolsLastCycle")}>
          <span className="num">
            {service.symbols_recorded_last_cycle}/{service.universe.length}
          </span>
        </Field>
        <Field label={t("strategies.universe")}>
          <span className="num">{service.universe.length} symbols</span>
        </Field>
        <Field label={t("sd.codeSha")} title={service.code_sha ?? undefined}>
          <span className="num">{service.code_sha ? service.code_sha.slice(0, 12) : "—"}</span>
        </Field>
        <Field label={t("sd.observerStarted")}>
          {service.started_at ? format.stamp(service.started_at, generatedAt) : "—"}
        </Field>
        <Field label={t("sd.session")}>
          {service.session_confirmed_open ? "Open — confirmed" : "No session confirmed today"}
        </Field>
      </div>

      {/* The invariant, measured rather than asserted. */}
      <div
        className={cn(
          "mt-5 rounded-sm border px-4 py-3",
          invariantOk ? "border-observe/30 bg-surface-0" : "border-neg/45 tint-neg",
        )}
      >
        <div className="flex items-center gap-2">
          <Dot tone={invariantOk ? "SHADOW" : "NEGATIVE"} />
          <span
            className={cn(
              "text-meta leading-none font-medium tracking-[0.06em] uppercase",
              toneText(invariantOk ? "SHADOW" : "NEGATIVE"),
            )}
          >
            {invariantOk ? "Zero-order invariant holds" : "Zero-order invariant violated"}
          </span>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-x-5 gap-y-3 sm:grid-cols-4">
          <Field label={t("sd.brokerMutation")}>{service.broker_mutation}</Field>
          <Field label={t("sd.ordersSubmitted")}>
            <span className="num">{service.orders_submitted}</span>
          </Field>
          <Field label={t("sd.intentsInDb")}>
            <span className="num">{service.order_intents_in_database}</span>
          </Field>
          <Field label={t("sd.ordersLinked")}>
            <span className="num">{service.linked_orders_in_database}</span>
          </Field>
        </div>
        <p className="mt-3 max-w-[92ch] text-meta leading-snug text-ink-3">
          <span className="text-ink-2">
            Actionable candidates released: {service.released_candidates}.
          </span>{" "}
          {service.released_candidates_meaning}
        </p>
        <p className="mt-2 max-w-[92ch] text-meta leading-snug text-ink-3">
          <span className="text-ink-2">{t("sd.startupSafety")}</span>{" "}
          {service.startup_safety_note}
        </p>
      </div>
    </Card>
  );
}

// --------------------------------------------------------------------------
// Regime
// --------------------------------------------------------------------------

export function ShadowRegime({
  regime,
  generatedAt,
}: {
  regime: RegimePanel | null;
  generatedAt: string | null;
}) {
  const { t } = useI18n();
  const format = useFormat();
  if (!regime || regime.participate === null) {
    return (
      <Card title={t("sd.regime")}>
        <Empty
          headline={t("sd.noRegime")}
          detail="The observer resolves one state per session, before the session's first decision."
        />
      </Card>
    );
  }

  const on = regime.participate;
  return (
    <Card
      tone="SHADOW"
      title={t("sd.regime")}
      meta={<Pill tone={on ? "POSITIVE" : "MUTED"}>{on ? "Participate" : "Defensive / V3"}</Pill>}
    >
      <p className="mb-4 max-w-[86ch] text-table leading-snug text-ink-2">
        {on ? (
          <>
            Risk-on. EDA-1 targets a long position in every universe symbol while the router is in
            this state.
          </>
        ) : (
          <>
            Risk-off. EDA-1 hands control back to V3 verbatim and holds no independent opinion while
            the router is in this state.
          </>
        )}
      </p>

      <div className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3">
        <Field label={t("market.session")} title={t("sd.sessionHint")}>
          {regime.session_date ?? "—"}
        </Field>
        <Field label={t("market.reference")}>{regime.reference_symbol ?? "—"}</Field>
        <Field label={t("market.sessionsObserved")}>
          <span className="num">{regime.sessions_observed ?? "—"}</span>
        </Field>
        <Field
          label={`${regime.reference_symbol ?? "SPY"} completed-session close`}
          title={t("sd.closeHint")}
        >
          <span className="num">{regime.info_close?.toFixed(2) ?? "—"}</span>
        </Field>
        <Field label={`SMA ${regime.sma_sessions ?? 200}`}>
          <span className="num">{regime.info_sma?.toFixed(2) ?? "—"}</span>
        </Field>
        <Field label={t("sd.drawdownPeak")}>
          <span className={cn("num", toneText(signTone(regime.info_drawdown)))}>
            {signedPercent(regime.info_drawdown)}
          </span>
        </Field>
      </div>

      <dl className="mt-5 space-y-1.5 border-t border-subtle pt-4 text-meta leading-snug text-ink-3">
        <div className="flex gap-2">
          <dt className="shrink-0 text-ink-2">{t("sd.rule")}</dt>
          <dd>
            Participate if and only if close &gt; SMA{regime.sma_sessions ?? 200} and drawdown &gt;{" "}
            {percent(regime.calm_threshold, 0)}, both strict.
          </dd>
        </div>
        <div className="flex gap-2">
          <dt className="shrink-0 text-ink-2">{t("sd.causalLag")}</dt>
          <dd>
            {regime.lag_sessions ?? 1} session. The information set ends at the previous completed
            session, so no state can read a close from the session it governs.
          </dd>
        </div>
        <div className="flex gap-2">
          <dt className="shrink-0 text-ink-2">{t("sd.computed")}</dt>
          <dd>{format.stamp(regime.computed_at, generatedAt)}</dd>
        </div>
      </dl>
    </Card>
  );
}

function signTone(value: number | null | undefined): "POSITIVE" | "NEGATIVE" | "NEUTRAL" {
  if (value === null || value === undefined || !Number.isFinite(value)) return "NEUTRAL";
  if (value > 0) return "POSITIVE";
  if (value < 0) return "NEGATIVE";
  return "NEUTRAL";
}

// --------------------------------------------------------------------------
// The side-by-side table
// --------------------------------------------------------------------------

function SignalPill({ signal }: { signal: string | null }) {
  if (!signal) return <span className="text-ink-3">—</span>;
  const tone = signal === "BUY" ? "POSITIVE" : signal === "SELL" ? "NEGATIVE" : "MUTED";
  return <Pill tone={tone}>{signal}</Pill>;
}

export function ShadowSymbols({
  symbols,
  generatedAt,
}: {
  symbols: SymbolRow[];
  generatedAt: string | null;
}) {
  const { t } = useI18n();
  const format = useFormat();
  const recorded = symbols.filter((row) => row.bar_timestamp !== null);
  if (recorded.length === 0) {
    return (
      <Card title={t("sd.latestBar")}>
        <Empty
          headline={t("sd.noDecisions")}
          detail="The observer records on completed 15-minute bars during US regular sessions."
        />
      </Card>
    );
  }

  return (
    <Card
      tone="SHADOW"
      title={t("sd.latestBar")}
      meta={<Tag title={t("sd.sameBar")}>{t("sd.symbolsCount", { count: recorded.length })}</Tag>}
      bodyClassName="p-0"
    >
      <div className="overflow-x-auto">
        <table className="w-full border-collapse">
          <thead className="border-b border-subtle">
            <tr>
              <Th>{t("orders.col.symbol")}</Th>
              <Th>{t("sd.barUtc")}</Th>
              <Th align="right">{t("market.reference")}</Th>
              <Th>V3</Th>
              <Th align="right">{t("sd.v3Score")}</Th>
              <Th align="right">{t("sd.v3Conf")}</Th>
              <Th>{t("sd.v3Regime")}</Th>
              <Th>EDA-1</Th>
              <Th>{t("sd.eda1Regime")}</Th>
              <Th>{t("sd.agree")}</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-subtle">
            {symbols.map((row) => (
              <tr key={row.symbol}>
                <Td className="font-medium text-ink">{row.symbol}</Td>
                <Td className="text-ink-3">
                  {row.bar_timestamp ? format.stamp(row.bar_timestamp, generatedAt) : "—"}
                </Td>
                <Td numeric>{row.reference_close?.toFixed(2) ?? "—"}</Td>
                <Td>
                  <SignalPill signal={row.v3_signal} />
                </Td>
                <Td numeric>{row.v3_score?.toFixed(3) ?? "—"}</Td>
                <Td numeric>{row.v3_confidence?.toFixed(3) ?? "—"}</Td>
                <Td className="text-ink-2">{row.v3_regime ?? "—"}</Td>
                <Td>
                  <SignalPill signal={row.eda1_signal} />
                </Td>
                <Td className="text-ink-2">{row.eda1_regime ?? "—"}</Td>
                <Td>
                  {row.signals_agree === null ? (
                    <span className="text-ink-3">—</span>
                  ) : (
                    <Pill tone={row.signals_agree ? "MUTED" : "ATTENTION"}>
                      {row.signals_agree ? "Agree" : "Differ"}
                    </Pill>
                  )}
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="border-t border-subtle px-4 py-3 text-meta leading-snug text-ink-3">
        EDA-1 has no score or confidence of its own — it is a participation router, not a second
        probability model, and the values it carries are V3&apos;s, copied. They are deliberately not
        repeated in an EDA-1 column, because a number in that column would read as a second opinion
        that does not exist.
      </p>
    </Card>
  );
}

// --------------------------------------------------------------------------
// Hypothetical portfolio
// --------------------------------------------------------------------------

function EngineCard({ engine, label }: { engine: EngineHypothetical | null; label: string }) {
  const { t } = useI18n();
  if (!engine) {
    return (
      <div className="rounded-sm border border-subtle bg-surface-0 px-4 py-3">
        <div className="eyebrow text-ink-3">{label}</div>
        <p className="mt-2 text-table text-ink-3">{t("sd.notEnoughBars")}</p>
      </div>
    );
  }
  return (
    <div className="rounded-sm border border-subtle bg-surface-0 px-4 py-3">
      <div className="flex items-baseline justify-between gap-3">
        <span className="eyebrow text-ink-3">{label}</span>
        <span className="text-meta text-ink-3">{engine.current_stance_summary}</span>
      </div>
      <div className="mt-2 flex items-baseline gap-3">
        <span className="num text-[22px] leading-none font-semibold tracking-tight text-ink">
          {engine.portfolio_value?.toFixed(2) ?? "—"}
        </span>
        <span className={cn("num text-body", toneText(signTone(engine.cumulative_return)))}>
          {signedPercent(engine.cumulative_return)}
        </span>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-x-4 gap-y-2">
        <Field label={t("sd.maxDrawdown")}>
          <span className="num">{signedPercent(engine.max_drawdown)}</span>
        </Field>
        <Field label={t("sd.longExposure")}>
          <span className="num">{percent(engine.long_exposure_fraction, 0)}</span>
        </Field>
        <Field label={t("sd.stanceChanges")}>
          <span className="num">{engine.stance_changes}</span>
        </Field>
      </div>
    </div>
  );
}

export function ShadowHypothetical({ panel }: { panel: HypotheticalPanel | null }) {
  const { t } = useI18n();
  const format = useFormat();
  if (!panel || panel.unavailable_reason) {
    return (
      <Card title={t("sd.hypothetical")}>
        <Empty
          headline={t("sd.notEnoughCompound")}
          detail="At least two observed bars are needed before a step return exists."
        />
      </Card>
    );
  }

  return (
    <Card
      tone="SHADOW"
      title={t("sd.hypothetical")}
      meta={<Pill tone="SHADOW" emphasis>{panel.label}</Pill>}
    >
      <p className="mb-4 max-w-[92ch] text-table leading-snug text-ink-2">
        An equal-weight book that followed each engine&apos;s recorded stance, compounded from a
        normalized <span className="num">{panel.normalized_start.toFixed(0)}</span> over{" "}
        <span className="num">{panel.steps}</span> observed{" "}
        {panel.steps === 1 ? "step" : "steps"}. Index points, not currency, and{" "}
        <strong className="font-semibold text-ink">no commission, spread or slippage is charged</strong>{" "}
        — so both curves are an upper bound, not a forecast. This is not, and must never be shown
        beside, broker account equity.
      </p>

      <div className="grid gap-3 sm:grid-cols-2">
        <EngineCard engine={panel.v3} label={t("sd.v3Engine")} />
        <EngineCard engine={panel.eda1} label={t("sd.eda1Engine")} />
      </div>

      <div className="mt-4 grid grid-cols-2 gap-x-5 gap-y-3 border-t border-subtle pt-4 sm:grid-cols-4">
        <Field label={t("sd.benchmark")} title={t("sd.benchmarkHint")}>
          <span className={cn("num", toneText(signTone(panel.benchmark_return)))}>
            {signedPercent(panel.benchmark_return)}
          </span>
        </Field>
        <Field label={t("sd.costsApplied")}>{panel.costs_applied ? t("sd.yes") : t("sd.costsNone")}</Field>
        <Field label={t("sd.firstBar")}>{panel.first_bar ? format.stamp(panel.first_bar) : "—"}</Field>
        <Field label={t("sd.lastBar")}>{panel.last_bar ? format.stamp(panel.last_bar) : "—"}</Field>
      </div>
    </Card>
  );
}

// --------------------------------------------------------------------------
// Comparison metrics
// --------------------------------------------------------------------------

export function ShadowComparison({ panel }: { panel: ComparisonPanel | null }) {
  const { t } = useI18n();
  if (!panel || panel.unavailable_reason) {
    return (
      <Card title={t("sd.comparison")}>
        <Empty headline={t("sd.noComparisons")} />
      </Card>
    );
  }

  return (
    <Card tone="SHADOW" title={t("sd.comparison")}>
      {/* The sample warning is unconditional and first. A caveat below the
          numbers is a caveat read after the conclusion has been formed. */}
      <div className="mb-4 rounded-sm border border-warn/35 tint-warn px-4 py-3">
        <div className="flex items-center gap-2">
          <Dot tone="ATTENTION" />
          <span className="text-meta leading-none font-medium tracking-[0.06em] text-warn uppercase">
            Sample size warning
          </span>
        </div>
        <p className="mt-2 max-w-[92ch] text-table leading-snug text-ink-2">{panel.sample_warning}</p>
      </div>

      <div className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3 lg:grid-cols-4">
        <Field label={t("sd.barsCompared")}>
          <span className="num">{panel.bars_compared}</span>
        </Field>
        <Field label={t("sd.decisionAgreement")}>
          <span className="num">{percent(panel.agreement_fraction, 1)}</span>
        </Field>
        <Field label={t("sd.disagreements")}>
          <span className="num">{panel.disagreement_count}</span>
        </Field>
        <Field label={t("sd.stanceDisagreements")} title={t("sd.stanceDisagreementsHint")}>
          <span className="num">{panel.stance_disagreement_count}</span>
        </Field>
        <Field label={t("sd.riskOnBars")}>
          <span className="num">{panel.participate_bars}</span>
        </Field>
        <Field label={t("sd.riskOffBars")}>
          <span className="num">{panel.defensive_bars}</span>
        </Field>
        <Field label={t("sd.riskOnSessions")}>
          <span className="num">{panel.participate_sessions}</span>
        </Field>
        <Field label={t("sd.regimeTransitions")}>
          <span className="num">{panel.regime_transitions}</span>
        </Field>
      </div>

      <div className="mt-5 border-t border-subtle pt-4">
        <div className="eyebrow mb-2 text-ink-3">{t("sd.captureRatios")}</div>
        {panel.capture_unavailable_reason ? (
          <p className="max-w-[92ch] text-table leading-snug text-ink-3">
            Withheld. {panel.steps} observed {panel.steps === 1 ? "step" : "steps"} is far below the
            threshold at which an up- or down-capture ratio means anything, and a ratio computed
            from a handful of points is noise wearing a statistic&apos;s name. No annualized figure —
            Sharpe included — is computed on this page at any sample size.
          </p>
        ) : (
          <div className="grid grid-cols-2 gap-x-5 gap-y-3 sm:grid-cols-4">
            <Field label={t("sd.upCapture")}>
              <span className="num">{panel.up_capture?.toFixed(3) ?? "—"}</span>
            </Field>
            <Field label={t("sd.downCapture")}>
              <span className="num">{panel.down_capture?.toFixed(3) ?? "—"}</span>
            </Field>
          </div>
        )}
      </div>

      <p className="mt-4 border-t border-subtle pt-4 text-meta leading-snug text-ink-3">
        No winner is declared here and none can be. The pre-registered evaluation judges EDA-1 on
        whether it reproduces its historical thesis across multiple regime states over months — not
        on a return column. V3 remains the production engine regardless of anything on this page,
        and any change to that requires a separate, explicit authorization.
      </p>
    </Card>
  );
}
