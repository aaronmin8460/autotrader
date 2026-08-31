"use client";

/**
 * The dashboard. One page, and deliberately only one.
 *
 * The composition answers the operator's questions in the order they are
 * asked: the header says whether anything is wrong, the metric row says what
 * the account is worth, the left column says what is held and what was sent,
 * and the right column says whether the machinery underneath is healthy and
 * how much risk is in use.
 *
 * Several runtimes share one account, so the shared account-safety strip sits
 * directly above them: it answers "may anything trade?", which outranks any
 * service's answer to "am I running?". The runtime cards sit side by side on a
 * wide screen and stack below it.
 *
 * **Two sources, one page.** The operational API describes the account and the
 * trails written into its store; the service endpoint describes which units are
 * actually up. The second exists because the first cannot see the equity paper
 * runtime at all, and a health panel built only from the first reported the
 * masked legacy service as though it were the current equity book.
 *
 * There is no control on this page. No buy, no sell, no close, no start, no
 * stop, no editable limit - and no endpoint behind any of them, which is the
 * part that actually makes it safe.
 */

import { AccountSafety } from "@/components/AccountSafety";
import { Attention } from "@/components/Attention";
import { Header } from "@/components/Header";
import { Metrics } from "@/components/Metrics";
import { Orders } from "@/components/Orders";
import { Positions } from "@/components/Positions";
import { Risk } from "@/components/Risk";
import { Runtimes } from "@/components/Runtime";
import { SystemHealth } from "@/components/SystemHealth";
import { POLL_INTERVAL_MS, useOverview, useServiceUnits } from "@/lib/api";

function Loading() {
  return (
    <div className="flex min-h-[40vh] items-center justify-center">
      <p className="text-[13px] text-ink-3">Reading operational state…</p>
    </div>
  );
}

function Unreachable() {
  return (
    <div className="rounded-card border border-line bg-surface px-5 py-8 text-center">
      <p className="text-[13px] text-ink-2">The dashboard API is not answering.</p>
      <p className="mx-auto mt-2 max-w-[52ch] text-[12px] leading-snug text-ink-3">
        Start it with{" "}
        <code className="rounded-[3px] bg-sunken px-1 py-0.5 font-mono text-[11.5px] text-ink-2">
          python -m autotrader.dashboard
        </code>
        . It binds 127.0.0.1:8000 and serves GET routes only.
      </p>
    </div>
  );
}

export default function Page() {
  const { data, loading, connected, lastSuccessAt } = useOverview();
  const { services } = useServiceUnits();

  return (
    <div className="min-h-full">
      <Header overview={data} connected={connected} lastSuccessAt={lastSuccessAt} />

      <main className="mx-auto max-w-[1520px] px-5 py-5 sm:px-6">
        {loading && !data ? (
          <Loading />
        ) : !data ? (
          <Unreachable />
        ) : (
          <div className="space-y-4">
            <Attention overview={data} />

            <Metrics metrics={data.metrics} />

            <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-12">
              <div className="space-y-4 lg:col-span-8">
                <Positions panel={data.positions} generatedAt={data.generated_at} />
                <Orders panel={data.orders} generatedAt={data.generated_at} />
              </div>

              <div className="lg:col-span-4">
                <SystemHealth
                  components={data.health}
                  services={services}
                  reconciliation={data.reconciliation}
                  generatedAt={data.generated_at}
                />
              </div>
            </div>

            <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-12">
              <div className="lg:col-span-4">
                <Risk panel={data.risk} />
              </div>
              <div className="space-y-4 lg:col-span-8">
                <AccountSafety
                  panel={data.account_safety}
                  budget={data.api_budget}
                  lastFailure={data.last_failure}
                  lastFailureAt={data.last_failure_at}
                  generatedAt={data.generated_at}
                />
                <Runtimes
                  panels={data.runtimes}
                  services={services}
                  generatedAt={data.generated_at}
                />
              </div>
            </div>
          </div>
        )}

        <footer className="mt-6 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line pt-4 text-[11px] text-ink-3">
          <span>Read-only view. The dashboard API exposes GET routes only.</span>
          <span>All times UTC.</span>
          <span className="num">Refreshes every {POLL_INTERVAL_MS / 1000}s.</span>
        </footer>
      </main>
    </div>
  );
}
