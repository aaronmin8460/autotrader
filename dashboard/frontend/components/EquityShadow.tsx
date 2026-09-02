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

import { percent, signedPercent, stampUtc } from "@/lib/format";
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
  return (
    <section
      aria-label="Equity Shadow scope"
      className="card tint-observe px-5 py-4"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="text-[13px] leading-none font-semibold tracking-tight text-ink">
          Equity — Shadow observation only
        </span>
        <Tag tone="SHADOW" title="No order can be submitted, cancelled, or replaced by this process.">
          Zero order mutation
        </Tag>
        <Tag tone="SHADOW" title="V3 is the production engine. EDA-1 is a research champion under observation.">
          V3 + EDA-1 side by side
        </Tag>
      </div>
      <p className="mt-2 max-w-[92ch] text-[12px] leading-snug text-ink-2">
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
  if (!service) {
    return (
      <Card title="Shadow service">
        <Empty headline="The shadow API is not answering." />
      </Card>
    );
  }

  const tone = shadowTone(service.status);
  const invariantOk = service.zero_order_invariant_holds;

  return (
    <Card
      tone="SHADOW"
      title="Shadow service"
      meta={
        <>
          <Pill tone={tone} emphasis>{shadowStatusLabel(service.status)}</Pill>
          <Tag title={service.mode}>{service.mode}</Tag>
        </>
      }
    >
      <p className="mb-4 text-[12px] leading-snug text-ink-2">
        {STATUS_REASON_LABELS[service.status_reason] ?? service.status_reason}
      </p>

      <div className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3 lg:grid-cols-4">
        <Field label="Last successful cycle">
          {service.last_cycle_at ? stampUtc(service.last_cycle_at, generatedAt) : "—"}
        </Field>
        <Field
          label="Next expected cycle"
          title="15-minute bar boundaries during US regular sessions only."
        >
          {service.next_expected_cycle_at
            ? stampUtc(service.next_expected_cycle_at, generatedAt)
            : service.within_regular_session
              ? "—"
              : "Next regular session"}
        </Field>
        <Field label="Cycles recorded">
          <span className="num">{service.cycles_recorded}</span>
        </Field>
        <Field label="Symbols last cycle">
          <span className="num">
            {service.symbols_recorded_last_cycle}/{service.universe.length}
          </span>
        </Field>
        <Field label="Universe">
          <span className="num">{service.universe.length} symbols</span>
        </Field>
        <Field label="Code SHA" title={service.code_sha ?? undefined}>
          <span className="num">{service.code_sha ? service.code_sha.slice(0, 12) : "—"}</span>
        </Field>
        <Field label="Observer started">
          {service.started_at ? stampUtc(service.started_at, generatedAt) : "—"}
        </Field>
        <Field label="Session (broker calendar)">
          {service.session_confirmed_open ? "Open — confirmed" : "No session confirmed today"}
        </Field>
      </div>

      {/* The invariant, measured rather than asserted. */}
      <div
        className={cn(
          "mt-5 rounded-[6px] border px-4 py-3",
          invariantOk ? "border-observe/30 bg-sunken" : "border-neg/45 tint-neg",
        )}
      >
        <div className="flex items-center gap-2">
          <Dot tone={invariantOk ? "SHADOW" : "NEGATIVE"} />
          <span
            className={cn(
              "text-[11px] leading-none font-medium tracking-[0.06em] uppercase",
              toneText(invariantOk ? "SHADOW" : "NEGATIVE"),
            )}
          >
            {invariantOk ? "Zero-order invariant holds" : "Zero-order invariant violated"}
          </span>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-x-5 gap-y-3 sm:grid-cols-4">
          <Field label="Broker mutation">{service.broker_mutation}</Field>
          <Field label="Orders submitted">
            <span className="num">{service.orders_submitted}</span>
          </Field>
          <Field label="Order intents in DB">
            <span className="num">{service.order_intents_in_database}</span>
          </Field>
          <Field label="Orders linked to a decision">
            <span className="num">{service.linked_orders_in_database}</span>
          </Field>
        </div>
        <p className="mt-3 max-w-[92ch] text-[11.5px] leading-snug text-ink-3">
          <span className="text-ink-2">
            Actionable candidates released: {service.released_candidates}.
          </span>{" "}
          {service.released_candidates_meaning}
        </p>
        <p className="mt-2 max-w-[92ch] text-[11.5px] leading-snug text-ink-3">
          <span className="text-ink-2">Startup safety / reconciliation:</span>{" "}
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
  if (!regime || regime.participate === null) {
    return (
      <Card title="EDA-1 regime">
        <Empty
          headline="No regime state recorded yet."
          detail="The observer resolves one state per session, before the session's first decision."
        />
      </Card>
    );
  }

  const on = regime.participate;
  return (
    <Card
      tone="SHADOW"
      title="EDA-1 regime"
      meta={<Pill tone={on ? "POSITIVE" : "MUTED"}>{on ? "Participate" : "Defensive / V3"}</Pill>}
    >
      <p className="mb-4 max-w-[86ch] text-[12px] leading-snug text-ink-2">
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
        <Field label="Session" title="One state per session, resolved before any decision in it.">
          {regime.session_date ?? "—"}
        </Field>
        <Field label="Reference">{regime.reference_symbol ?? "—"}</Field>
        <Field label="Sessions observed">
          <span className="num">{regime.sessions_observed ?? "—"}</span>
        </Field>
        <Field
          label={`${regime.reference_symbol ?? "SPY"} completed-session close`}
          title="The last completed session's close. The state governing a session never reads that session's own close."
        >
          <span className="num">{regime.info_close?.toFixed(2) ?? "—"}</span>
        </Field>
        <Field label={`SMA ${regime.sma_sessions ?? 200}`}>
          <span className="num">{regime.info_sma?.toFixed(2) ?? "—"}</span>
        </Field>
        <Field label="Drawdown from trailing peak">
          <span className={cn("num", toneText(signTone(regime.info_drawdown)))}>
            {signedPercent(regime.info_drawdown)}
          </span>
        </Field>
      </div>

      <dl className="mt-5 space-y-1.5 border-t border-line pt-4 text-[11.5px] leading-snug text-ink-3">
        <div className="flex gap-2">
          <dt className="shrink-0 text-ink-2">Rule</dt>
          <dd>
            Participate if and only if close &gt; SMA{regime.sma_sessions ?? 200} and drawdown &gt;{" "}
            {percent(regime.calm_threshold, 0)}, both strict.
          </dd>
        </div>
        <div className="flex gap-2">
          <dt className="shrink-0 text-ink-2">Causal lag</dt>
          <dd>
            {regime.lag_sessions ?? 1} session. The information set ends at the previous completed
            session, so no state can read a close from the session it governs.
          </dd>
        </div>
        <div className="flex gap-2">
          <dt className="shrink-0 text-ink-2">Computed</dt>
          <dd>{stampUtc(regime.computed_at, generatedAt)}</dd>
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
  const recorded = symbols.filter((row) => row.bar_timestamp !== null);
  if (recorded.length === 0) {
    return (
      <Card title="V3 vs EDA-1 — latest bar">
        <Empty
          headline="No decisions recorded yet."
          detail="The observer records on completed 15-minute bars during US regular sessions."
        />
      </Card>
    );
  }

  return (
    <Card
      tone="SHADOW"
      title="V3 vs EDA-1 — latest bar"
      meta={<Tag title="Both engines evaluated the same bar.">{recorded.length} symbols</Tag>}
      bodyClassName="p-0"
    >
      <div className="overflow-x-auto">
        <table className="w-full border-collapse">
          <thead className="border-b border-line">
            <tr>
              <Th>Symbol</Th>
              <Th>Bar (UTC)</Th>
              <Th align="right">Reference</Th>
              <Th>V3</Th>
              <Th align="right">V3 score</Th>
              <Th align="right">V3 conf.</Th>
              <Th>V3 regime</Th>
              <Th>EDA-1</Th>
              <Th>EDA-1 regime</Th>
              <Th>Agree</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {symbols.map((row) => (
              <tr key={row.symbol}>
                <Td className="font-medium text-ink">{row.symbol}</Td>
                <Td className="text-ink-3">
                  {row.bar_timestamp ? stampUtc(row.bar_timestamp, generatedAt) : "—"}
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
      <p className="border-t border-line px-4 py-3 text-[11.5px] leading-snug text-ink-3">
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
  if (!engine) {
    return (
      <div className="rounded-[6px] border border-line bg-sunken px-4 py-3">
        <div className="eyebrow text-ink-3">{label}</div>
        <p className="mt-2 text-[12px] text-ink-3">Not enough recorded bars.</p>
      </div>
    );
  }
  return (
    <div className="rounded-[6px] border border-line bg-sunken px-4 py-3">
      <div className="flex items-baseline justify-between gap-3">
        <span className="eyebrow text-ink-3">{label}</span>
        <span className="text-[11px] text-ink-3">{engine.current_stance_summary}</span>
      </div>
      <div className="mt-2 flex items-baseline gap-3">
        <span className="num text-[22px] leading-none font-semibold tracking-tight text-ink">
          {engine.portfolio_value?.toFixed(2) ?? "—"}
        </span>
        <span className={cn("num text-[13px]", toneText(signTone(engine.cumulative_return)))}>
          {signedPercent(engine.cumulative_return)}
        </span>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-x-4 gap-y-2">
        <Field label="Max drawdown">
          <span className="num">{signedPercent(engine.max_drawdown)}</span>
        </Field>
        <Field label="Long exposure">
          <span className="num">{percent(engine.long_exposure_fraction, 0)}</span>
        </Field>
        <Field label="Stance changes">
          <span className="num">{engine.stance_changes}</span>
        </Field>
      </div>
    </div>
  );
}

export function ShadowHypothetical({ panel }: { panel: HypotheticalPanel | null }) {
  if (!panel || panel.unavailable_reason) {
    return (
      <Card title="Hypothetical portfolio">
        <Empty
          headline="Not enough recorded bars to compound a return."
          detail="At least two observed bars are needed before a step return exists."
        />
      </Card>
    );
  }

  return (
    <Card
      tone="SHADOW"
      title="Hypothetical portfolio"
      meta={<Pill tone="SHADOW" emphasis>{panel.label}</Pill>}
    >
      <p className="mb-4 max-w-[92ch] text-[12px] leading-snug text-ink-2">
        An equal-weight book that followed each engine&apos;s recorded stance, compounded from a
        normalized <span className="num">{panel.normalized_start.toFixed(0)}</span> over{" "}
        <span className="num">{panel.steps}</span> observed{" "}
        {panel.steps === 1 ? "step" : "steps"}. Index points, not currency, and{" "}
        <strong className="font-semibold text-ink">no commission, spread or slippage is charged</strong>{" "}
        — so both curves are an upper bound, not a forecast. This is not, and must never be shown
        beside, broker account equity.
      </p>

      <div className="grid gap-3 sm:grid-cols-2">
        <EngineCard engine={panel.v3} label="V3 — production engine (shadow)" />
        <EngineCard engine={panel.eda1} label="EDA-1 — research champion (shadow)" />
      </div>

      <div className="mt-4 grid grid-cols-2 gap-x-5 gap-y-3 border-t border-line pt-4 sm:grid-cols-4">
        <Field label="Equal-weight benchmark" title="Every universe symbol held throughout.">
          <span className={cn("num", toneText(signTone(panel.benchmark_return)))}>
            {signedPercent(panel.benchmark_return)}
          </span>
        </Field>
        <Field label="Costs applied">{panel.costs_applied ? "Yes" : "None"}</Field>
        <Field label="First bar">{panel.first_bar ? stampUtc(panel.first_bar) : "—"}</Field>
        <Field label="Last bar">{panel.last_bar ? stampUtc(panel.last_bar) : "—"}</Field>
      </div>
    </Card>
  );
}

// --------------------------------------------------------------------------
// Comparison metrics
// --------------------------------------------------------------------------

export function ShadowComparison({ panel }: { panel: ComparisonPanel | null }) {
  if (!panel || panel.unavailable_reason) {
    return (
      <Card title="V3 vs EDA-1 — comparison">
        <Empty headline="No comparisons recorded yet." />
      </Card>
    );
  }

  return (
    <Card tone="SHADOW" title="V3 vs EDA-1 — comparison">
      {/* The sample warning is unconditional and first. A caveat below the
          numbers is a caveat read after the conclusion has been formed. */}
      <div className="mb-4 rounded-[6px] border border-warn/35 tint-warn px-4 py-3">
        <div className="flex items-center gap-2">
          <Dot tone="ATTENTION" />
          <span className="text-[11px] leading-none font-medium tracking-[0.06em] text-warn uppercase">
            Sample size warning
          </span>
        </div>
        <p className="mt-2 max-w-[92ch] text-[12px] leading-snug text-ink-2">{panel.sample_warning}</p>
      </div>

      <div className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3 lg:grid-cols-4">
        <Field label="Bars compared">
          <span className="num">{panel.bars_compared}</span>
        </Field>
        <Field label="Decision agreement">
          <span className="num">{percent(panel.agreement_fraction, 1)}</span>
        </Field>
        <Field label="Disagreements">
          <span className="num">{panel.disagreement_count}</span>
        </Field>
        <Field label="Stance disagreements" title="Bars where the two engines' target positions differ.">
          <span className="num">{panel.stance_disagreement_count}</span>
        </Field>
        <Field label="Risk-on bars">
          <span className="num">{panel.participate_bars}</span>
        </Field>
        <Field label="Risk-off bars">
          <span className="num">{panel.defensive_bars}</span>
        </Field>
        <Field label="Risk-on sessions">
          <span className="num">{panel.participate_sessions}</span>
        </Field>
        <Field label="Regime transitions">
          <span className="num">{panel.regime_transitions}</span>
        </Field>
      </div>

      <div className="mt-5 border-t border-line pt-4">
        <div className="eyebrow mb-2 text-ink-3">Capture ratios</div>
        {panel.capture_unavailable_reason ? (
          <p className="max-w-[92ch] text-[12px] leading-snug text-ink-3">
            Withheld. {panel.steps} observed {panel.steps === 1 ? "step" : "steps"} is far below the
            threshold at which an up- or down-capture ratio means anything, and a ratio computed
            from a handful of points is noise wearing a statistic&apos;s name. No annualized figure —
            Sharpe included — is computed on this page at any sample size.
          </p>
        ) : (
          <div className="grid grid-cols-2 gap-x-5 gap-y-3 sm:grid-cols-4">
            <Field label="EDA-1 up-capture">
              <span className="num">{panel.up_capture?.toFixed(3) ?? "—"}</span>
            </Field>
            <Field label="EDA-1 down-capture">
              <span className="num">{panel.down_capture?.toFixed(3) ?? "—"}</span>
            </Field>
          </div>
        )}
      </div>

      <p className="mt-4 border-t border-line pt-4 text-[11.5px] leading-snug text-ink-3">
        No winner is declared here and none can be. The pre-registered evaluation judges EDA-1 on
        whether it reproduces its historical thesis across multiple regime states over months — not
        on a return column. V3 remains the production engine regardless of anything on this page,
        and any change to that requires a separate, explicit authorization.
      </p>
    </Card>
  );
}
