"use client";

/**
 * The Equity Paper page.
 *
 * The dashboard's third record, and the one that most needs to be told apart
 * from the other two. The crypto page shows a paper book that trades. The
 * shadow page shows an observation record whose figures are hypothetical and
 * whose order count is structurally zero. This page shows a paper book that
 * trades *equities*, against the same account as the crypto book: its
 * positions are real broker positions and its fills are real fills.
 *
 * Ordered by what could go wrong. The banner says which record this is. The
 * service card says which stage is live and which sizing policy is frozen,
 * because "EDA-1 is running" means something different on one symbol than on
 * ten. Exposure is account-wide. Then the targets, then what was actually
 * sent, then safety.
 *
 * There is no control here. No start, no stop, no stage advance, no cancel -
 * and no endpoint behind any of them.
 */

import {
  PaperBanner,
  PaperExposure,
  PaperOrders,
  PaperRegime,
  PaperSafety,
  PaperService,
  PaperTargets,
} from "@/components/EquityPaper";
import { ShadowNav } from "@/components/ShadowNav";
import { usePaperOverview } from "@/lib/paper";

export default function EquityPaperPage() {
  const { data, loading, connected, lastSuccessAt } = usePaperOverview();

  return (
    <div className="min-h-full">
      <ShadowNav current="paper" connected={connected} lastSuccessAt={lastSuccessAt} />

      <main className="mx-auto max-w-[1520px] space-y-4 px-5 py-5 sm:px-6">
        <PaperBanner />

        {loading && !data ? (
          <div className="flex min-h-[30vh] items-center justify-center">
            <p className="text-[13px] text-ink-3">Reading the equity paper record…</p>
          </div>
        ) : (
          <>
            <PaperService
              service={data?.service ?? null}
              generatedAt={data?.generated_at ?? null}
            />

            <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,420px)]">
              <PaperExposure exposure={data?.exposure ?? null} />
              <PaperRegime regime={data?.regime ?? null} />
            </div>

            <PaperTargets targets={data?.targets ?? []} />
            <PaperOrders orders={data?.orders ?? []} />
            <PaperSafety safety={data?.safety ?? null} />
          </>
        )}
      </main>
    </div>
  );
}
