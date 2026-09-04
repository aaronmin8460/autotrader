"use client";

/**
 * Shadows: forward comparison of observation-only strategies.
 *
 * One workspace, one card per observer, one comparison table. Two records feed
 * it — the V3 + EDA-1 shadow and the A1-B U30 shadow — each from its own
 * process reading its own database, neither of which can act. Unit state comes
 * from the service manager through the paper API, and the parity figure for the
 * EDA-1 shadow is the paper runtime's own cumulative count.
 *
 * Everything on this page is violet and says OBSERVING, because nothing on it
 * can trade. No figure here is broker account equity, and the comparison table
 * refuses to print a performance number as a conclusion until each observer's
 * own sample threshold is met.
 *
 * There is no control here. No promote, no activate, no start, no stop — and no
 * endpoint behind any of them.
 */

import { useMemo } from "react";

import {
  ShadowComparison,
  ShadowHypothetical,
  ShadowRegime,
  ShadowService,
  ShadowSymbols,
} from "@/components/EquityShadow";
import {
  A1BDetail,
  A1BUniverse,
  ShadowCard,
  ShadowComparisonTable,
  ShadowsBanner,
  shadowCards,
} from "@/components/Shadows";
import { PageHeader } from "@/components/shell/PageHeader";
import { SectionHeader, StrategyBadge } from "@/components/ui";
import { useA1BOverview } from "@/lib/a1b";
import { useDashboard } from "@/lib/dashboard";
import { useI18n } from "@/lib/i18n";
import { A1B_SHADOW_KEY, serviceUnit } from "@/lib/services";
import { useShadowOverview } from "@/lib/shadow";
import { compareShadows } from "@/lib/shadows";

export default function ShadowsPage() {
  const { t } = useI18n();
  const eda1 = useShadowOverview();
  const a1b = useA1BOverview();
  const { services, paper } = useDashboard();

  const cards = useMemo(
    () =>
      shadowCards(
        eda1.data,
        a1b.data,
        { eda1: serviceUnit(services, "equity_shadow"), a1b: serviceUnit(services, A1B_SHADOW_KEY) },
        paper.data?.safety.parity_mismatches ?? null,
      ),
    [eda1.data, a1b.data, services, paper.data],
  );
  const comparison = useMemo(() => compareShadows(eda1.data, a1b.data), [eda1.data, a1b.data]);
  const generatedAt = eda1.data?.generated_at ?? a1b.data?.generated_at ?? null;

  return (
    <div className="space-y-5">
      <PageHeader
        title={t("shadows.title")}
        context={t("nav.detail.shadows")}
        actions={<StrategyBadge kind="SHADOW" />}
      />

      <ShadowsBanner />

      <div className="grid items-start gap-4 xl:grid-cols-2">
        {cards.map((card) => (
          <ShadowCard key={card.key} model={card} generatedAt={generatedAt} />
        ))}
      </div>

      <ShadowComparisonTable comparison={comparison} />

      <A1BUniverse overview={a1b.data} generatedAt={a1b.data?.generated_at ?? null} />
      <A1BDetail overview={a1b.data} generatedAt={a1b.data?.generated_at ?? null} />

      <div className="space-y-2">
        <SectionHeader title="Equity Shadow · V3 + EDA-1" />
        <ShadowService service={eda1.data?.service ?? null} generatedAt={eda1.data?.generated_at ?? null} />
        <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,400px)]">
          <ShadowSymbols symbols={eda1.data?.symbols ?? []} generatedAt={eda1.data?.generated_at ?? null} />
          <ShadowRegime regime={eda1.data?.regime ?? null} generatedAt={eda1.data?.generated_at ?? null} />
        </div>
        <ShadowHypothetical panel={eda1.data?.hypothetical ?? null} />
        <ShadowComparison panel={eda1.data?.comparison ?? null} />
      </div>
    </div>
  );
}
