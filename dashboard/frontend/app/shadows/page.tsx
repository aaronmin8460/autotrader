"use client";

/**
 * Shadows: forward comparison of observation-only strategies.
 *
 * One workspace, one card per observer, one comparison table. Two records
 * feed it - the V3 + EDA-1 shadow and the A1-B U30 shadow - each from its
 * own process reading its own database, neither of which can act. Unit state
 * comes from the service manager through the paper API, and the parity
 * figure for the EDA-1 shadow is the paper runtime's own cumulative count.
 *
 * There is no control here. No promote, no activate, no start, no stop - and
 * no endpoint behind any of them.
 */

import { useMemo } from "react";

import {
  ShadowComparison,
  ShadowHypothetical,
  ShadowRegime,
  ShadowService,
  ShadowSymbols,
} from "@/components/EquityShadow";
import { Footer, Nav } from "@/components/Nav";
import { A1BDetail, A1BUniverse, ShadowCard, ShadowComparisonTable, ShadowsBanner, shadowCards } from "@/components/Shadows";
import { useA1BOverview } from "@/lib/a1b";
import { useOverview, useServiceUnits } from "@/lib/api";
import { usePaperOverview } from "@/lib/paper";
import { A1B_SHADOW_KEY, serviceUnit } from "@/lib/services";
import { SHADOW_POLL_INTERVAL_MS, useShadowOverview } from "@/lib/shadow";
import { compareShadows } from "@/lib/shadows";

export default function ShadowsPage() {
  const eda1 = useShadowOverview();
  const a1b = useA1BOverview();
  const { services } = useServiceUnits();
  const { data: account } = useOverview();
  const { data: paper } = usePaperOverview();

  const cards = useMemo(
    () =>
      shadowCards(
        eda1.data,
        a1b.data,
        { eda1: serviceUnit(services, "equity_shadow"), a1b: serviceUnit(services, A1B_SHADOW_KEY) },
        paper?.safety.parity_mismatches ?? null,
      ),
    [eda1.data, a1b.data, services, paper],
  );
  const comparison = useMemo(() => compareShadows(eda1.data, a1b.data), [eda1.data, a1b.data]);
  const connected = eda1.connected && a1b.connected;
  const lastSuccessAt = eda1.lastSuccessAt ?? a1b.lastSuccessAt;
  const generatedAt = eda1.data?.generated_at ?? a1b.data?.generated_at ?? null;

  return (
    <div className="min-h-full">
      <Nav
        section="shadows"
        verdict={account?.system_state ?? null}
        verdictTone={account?.system_state_tone ?? "MUTED"}
        verdictTitle={account?.attention.join(" ") || "Broker account"}
        badge={{ text: "Observation only", title: "Every process on this page records decisions and cannot submit, cancel or replace an order.", tone: "SHADOW" }}
        connected={connected}
        lastSuccessAt={lastSuccessAt}
      />

      <main className="mx-auto max-w-[1720px] space-y-4 px-5 py-5 sm:px-6">
        <ShadowsBanner />

        <div className="grid gap-4 xl:grid-cols-2">
          {cards.map((card) => (
            <ShadowCard key={card.key} model={card} generatedAt={generatedAt} />
          ))}
        </div>

        <ShadowComparisonTable comparison={comparison} />

        <A1BUniverse overview={a1b.data} generatedAt={a1b.data?.generated_at ?? null} />
        <A1BDetail overview={a1b.data} generatedAt={a1b.data?.generated_at ?? null} />

        <h2 className="pt-2 text-[13px] font-semibold tracking-tight text-ink-2">Equity Shadow · V3 + EDA-1 detail</h2>
        {eda1.loading && !eda1.data ? (
          <div className="flex min-h-[20vh] items-center justify-center">
            <p className="text-[13px] text-ink-3">Reading the shadow record…</p>
          </div>
        ) : (
          <>
            <ShadowService service={eda1.data?.service ?? null} generatedAt={eda1.data?.generated_at ?? null} />
            <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,380px)]">
              <ShadowSymbols symbols={eda1.data?.symbols ?? []} generatedAt={eda1.data?.generated_at ?? null} />
              <ShadowRegime regime={eda1.data?.regime ?? null} generatedAt={eda1.data?.generated_at ?? null} />
            </div>
            <ShadowHypothetical panel={eda1.data?.hypothetical ?? null} />
            <ShadowComparison panel={eda1.data?.comparison ?? null} />
          </>
        )}
        <Footer intervalSeconds={SHADOW_POLL_INTERVAL_MS / 1000} />
      </main>
    </div>
  );
}
