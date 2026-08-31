/**
 * The header. An operations product's title bar, not a landing page's.
 *
 * Product name, the environment it is pointed at, the one-word system verdict,
 * and when the data on screen is from. No logo: a screenshot of an internal
 * tool does not need branding.
 *
 * There is navigation, because there are three pages whose records mean
 * categorically different things: an account that trades crypto and equities,
 * an equity book that trades on its own, and two engines that cannot trade at
 * all. Each tab states its own nature so that a reader who lands on a
 * screenshot can tell which of the three they are looking at without reading
 * the body. All three tabs appear on every page - a page missing from the nav
 * is a page an operator concludes does not exist.
 *
 * This page's tab says "Broker account", not "Crypto". The positions table
 * below is the whole paper account and holds equity rows alongside crypto
 * ones, so calling the page crypto-only would misdescribe what is on it.
 */

import Link from "next/link";

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
        <span className="hidden text-[12.5px] leading-none text-ink-3 sm:inline">
          Operations · Broker account
        </span>
        <Tag title="Alpaca paper trading. This repository has no live mode.">Paper</Tag>

        <nav aria-label="Sections" className="ml-2 flex items-center gap-1">
          <Link
            href="/"
            aria-current="page"
            className="flex items-baseline gap-2 rounded-[4px] bg-sunken px-2 py-1.5 text-[12.5px] leading-none whitespace-nowrap text-ink"
          >
            <span className="font-medium">Operations</span>
            <span className="hidden text-[10px] tracking-[0.06em] text-ink-3 uppercase sm:inline">
              Broker account
            </span>
          </Link>
          <Link
            href="/equity-shadow"
            className="flex items-baseline gap-2 rounded-[4px] px-2 py-1.5 text-[12.5px] leading-none whitespace-nowrap text-ink-3 transition-colors hover:text-ink-2"
          >
            <span className="font-medium">Equity Shadow</span>
            <span className="hidden text-[10px] tracking-[0.06em] uppercase sm:inline">
              Observation · zero orders
            </span>
          </Link>
          <Link
            href="/equity-paper"
            className="flex items-baseline gap-2 rounded-[4px] px-2 py-1.5 text-[12.5px] leading-none whitespace-nowrap text-ink-3 transition-colors hover:text-ink-2"
          >
            <span className="font-medium">Equity Paper</span>
            <span className="hidden text-[10px] tracking-[0.06em] uppercase sm:inline">
              EDA-1 · paper orders
            </span>
          </Link>
        </nav>

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
