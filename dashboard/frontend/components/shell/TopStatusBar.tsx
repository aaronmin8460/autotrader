"use client";

/**
 * The operational status strip, on every page.
 *
 * Four runtimes and one data-freshness reading, in one compact row. The whole
 * point is that a healthy system is **quiet**: every normal state renders in
 * muted ink with a small dot, and only a warning or a failure gains colour and
 * weight. A bar that shouts when everything is fine is a bar nobody reads on
 * the day something is not.
 *
 * The runtime states come from the service manager through the paper API — the
 * same source the System page uses — and never from a trail written by a
 * different service. A running observer says OBSERVING in violet; green is
 * reserved for a process that can place an order. The masked legacy unit is
 * deliberately absent from this row: it is a decision, not a status, and it is
 * reported in full on System.
 *
 * When the service manager cannot be asked, every unit reads UNKNOWN. It never
 * falls back to a store-derived guess.
 */

import { useI18n } from "@/lib/i18n";
import { displayStatus, SERVICE_UNITS, serviceUnit } from "@/lib/services";
import { useDashboard } from "@/lib/dashboard";
import type { Tone } from "@/lib/types";

import { Dot, FreshnessIndicator, cn, toneText } from "../ui";
import { LanguageSwitcher, ThemeSwitcher } from "./GlobalControls";

/** The four units worth a permanent line. The legacy unit is not one of them. */
const BAR_UNITS = ["equity_paper", "crypto", "equity_shadow", "equity_a1b_shadow"] as const;

function UnitState({ unitKey }: { unitKey: (typeof BAR_UNITS)[number] }) {
  const { t } = useI18n();
  const { services, servicesConnected } = useDashboard();
  const spec = SERVICE_UNITS.find((entry) => entry.key === unitKey);
  const unit = serviceUnit(services, unitKey);

  const shown = unit
    ? displayStatus(unit)
    : { status: "UNKNOWN", tone: (servicesConnected ? "MUTED" : "ATTENTION") as Tone };
  // Quiet when normal: a healthy state is ink, not colour. Only ATTENTION and
  // NEGATIVE keep their semantic colour, and OBSERVING keeps violet because
  // "this one cannot trade" is information, not an alarm.
  const loud = shown.tone === "ATTENTION" || shown.tone === "NEGATIVE" || shown.tone === "SHADOW";

  return (
    <span
      className="inline-flex items-baseline gap-1.5 whitespace-nowrap"
      title={unit?.detail ?? t("status.unknownHint")}
    >
      <span className="text-meta text-ink-3">{spec?.label ?? unitKey}</span>
      <span className={cn("inline-flex items-center gap-1", loud ? toneText(shown.tone) : "text-ink-2")}>
        <Dot tone={shown.tone} />
        <span className="text-meta font-medium tracking-[0.05em] uppercase">{shown.status}</span>
      </span>
    </span>
  );
}

export function TopStatusBar() {
  const { t } = useI18n();
  const { account, freshness } = useDashboard();

  return (
    <div className="floating sticky top-0 z-(--z-topbar) border-b">
      <div className="flex min-h-12 flex-wrap items-center gap-x-5 gap-y-1.5 px-5 py-2 sm:px-6">
        <div
          className="flex flex-wrap items-center gap-x-4 gap-y-1.5"
          role="status"
          aria-label={t("status.title")}
        >
          {BAR_UNITS.map((key) => (
            <UnitState key={key} unitKey={key} />
          ))}
          <FreshnessIndicator
            state={freshness.state}
            ageSeconds={freshness.ageSeconds}
            label={t("status.data")}
          />
        </div>

        <div className="ms-auto flex items-center gap-2.5">
          {account.connected ? null : (
            <span className="text-eyebrow font-medium tracking-[0.06em] text-warn uppercase">
              {t("status.reconnecting")}
            </span>
          )}
          <LanguageSwitcher />
          <ThemeSwitcher />
        </div>
      </div>
    </div>
  );
}
