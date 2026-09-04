"use client";

/**
 * Strategies — the lifecycle view.
 *
 * One card per strategy, ordered by what it is allowed to do: the paper
 * strategy that trades, then the two observers that structurally cannot, then
 * the legacy runtime that is masked on purpose. The market state sits at the
 * top because it is the condition every one of them is reacting to.
 *
 * No research candidate appears on this page. Several have been evaluated in
 * this repository and none is deployed; showing one here would state the
 * opposite.
 */

import { MarketState } from "@/components/MarketState";
import { StrategyCard, strategyModels } from "@/components/Strategies";
import { PageHeader } from "@/components/shell/PageHeader";
import { SectionHeader } from "@/components/ui";
import { useDashboard } from "@/lib/dashboard";
import { useI18n } from "@/lib/i18n";

export default function StrategiesPage() {
  const { t } = useI18n();
  const { account, paper, services } = useDashboard();
  const models = strategyModels(paper.data);
  const generatedAt = paper.data?.generated_at ?? account.data?.generated_at ?? null;

  return (
    <div className="space-y-5">
      <PageHeader title={t("strategies.title")} context={t("strategies.subtitle")} />

      <MarketState regime={paper.data?.regime ?? null} policy={paper.data?.policy ?? null} />

      <div className="space-y-2">
        <SectionHeader title={t("nav.strategies")} />
        <div className="space-y-4">
          {models.map((model) => (
            <StrategyCard
              key={model.key}
              model={model}
              services={services}
              generatedAt={generatedAt}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
