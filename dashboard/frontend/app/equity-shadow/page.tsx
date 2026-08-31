"use client";

/**
 * The Equity Shadow page.
 *
 * A second page, and the first time this dashboard has had one - because the
 * thing it shows is categorically different from what the operational page
 * shows, and putting an observation record in a card on the account screen
 * would eventually get one read as the other.
 *
 * The composition is ordered by what could go wrong. The banner says what
 * this is before anything is read. The service card says whether the observer
 * is observing and, measured rather than asserted, whether it is still
 * incapable of trading. Only then come the decisions, and only after those
 * the hypothetical figures - last, behind their own warning, because they are
 * the only thing on the page a reader could mistake for money.
 *
 * There is no control here. No promote, no activate, no start, no stop - and
 * no endpoint behind any of them.
 */

import {
  ShadowBanner,
  ShadowComparison,
  ShadowHypothetical,
  ShadowRegime,
  ShadowService,
  ShadowSymbols,
} from "@/components/EquityShadow";
import { ShadowNav } from "@/components/ShadowNav";
import { useShadowOverview } from "@/lib/shadow";

export default function EquityShadowPage() {
  const { data, loading, connected, lastSuccessAt } = useShadowOverview();

  return (
    <div className="min-h-full">
      <ShadowNav current="shadow" connected={connected} lastSuccessAt={lastSuccessAt} />

      <main className="mx-auto max-w-[1520px] space-y-4 px-5 py-5 sm:px-6">
        <ShadowBanner />

        {loading && !data ? (
          <div className="flex min-h-[30vh] items-center justify-center">
            <p className="text-[13px] text-ink-3">Reading the shadow record…</p>
          </div>
        ) : (
          <>
            <ShadowService service={data?.service ?? null} generatedAt={data?.generated_at ?? null} />

            <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,380px)]">
              <ShadowSymbols
                symbols={data?.symbols ?? []}
                generatedAt={data?.generated_at ?? null}
              />
              <ShadowRegime regime={data?.regime ?? null} generatedAt={data?.generated_at ?? null} />
            </div>

            <ShadowHypothetical panel={data?.hypothetical ?? null} />
            <ShadowComparison panel={data?.comparison ?? null} />
          </>
        )}
      </main>
    </div>
  );
}
