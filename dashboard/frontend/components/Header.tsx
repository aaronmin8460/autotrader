/**
 * The header. An operations product's title bar, not a landing page's.
 *
 * Product name, the environment it is pointed at, the one-word system verdict,
 * and when the data on screen is from. Nothing else earns a place: there is no
 * navigation because there is one page, and no logo because a screenshot of an
 * internal tool does not need branding.
 */

import { clockUtc } from "@/lib/format";
import type { Overview } from "@/lib/types";

import { Dot, Tag, cn, toneText } from "./ui";

export function Header({
  overview,
  connected,
  lastSuccessAt,
}: {
  overview: Overview | null;
  connected: boolean;
  lastSuccessAt: string | null;
}) {
  const state = overview?.system_state ?? "—";
  const tone = overview?.system_state_tone ?? "MUTED";

  return (
    <header className="sticky top-0 z-10 border-b border-line bg-canvas/92 backdrop-blur-sm">
      <div className="mx-auto flex h-14 max-w-[1520px] items-center gap-3 px-5 sm:px-6">
        <h1 className="text-[14px] leading-none font-semibold tracking-tight text-ink">
          AutoTrader
        </h1>
        <Tag title="Alpaca paper trading. This repository has no live mode.">Paper</Tag>

        <div className="ml-auto flex items-center gap-4">
          <span
            className={cn("inline-flex items-center gap-2", toneText(tone))}
            title={overview?.attention.join(" ") || undefined}
          >
            <Dot tone={tone} />
            <span className="text-[12px] leading-none font-medium tracking-[0.06em] uppercase">
              {state}
            </span>
          </span>

          <span className="hidden h-4 w-px bg-line sm:block" />

          <span className="hidden items-center gap-2 sm:flex">
            {connected ? null : (
              <span className="text-[10px] leading-none font-medium tracking-[0.06em] text-warn uppercase">
                Reconnecting
              </span>
            )}
            <span
              className={cn("num text-[12px] leading-none", connected ? "text-ink-2" : "text-ink-3")}
            >
              {lastSuccessAt ? `Last sync ${clockUtc(lastSuccessAt)} UTC` : "Awaiting first sync"}
            </span>
          </span>
        </div>
      </div>
    </header>
  );
}
