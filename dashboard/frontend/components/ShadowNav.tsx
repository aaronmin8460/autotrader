/**
 * The three-page navigation, and the distinction it has to carry.
 *
 * This dashboard shows three records that must never be confused: a crypto
 * book that trades, an equity book that trades, and two engines that cannot.
 * Two of the three are real and the middle one is hypothetical. The nav is the
 * first place a reader learns which one they are on, so each tab states its
 * own nature in its detail line rather than leaving the difference to the page
 * body.
 *
 * `next/link` rather than a bare anchor, which is what the framework's own
 * lint rule requires for an in-app route - and it keeps the two pages from
 * re-downloading the bundle on every switch.
 */

import Link from "next/link";

import { clockUtc } from "@/lib/format";

import { Tag, cn } from "./ui";

function Tab({
  href,
  label,
  detail,
  active,
}: {
  href: string;
  label: string;
  detail: string;
  active: boolean;
}) {
  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      className={cn(
        "group flex items-baseline gap-2 rounded-[4px] px-2 py-1.5 whitespace-nowrap",
        "text-[12.5px] leading-none transition-colors",
        active ? "bg-sunken text-ink" : "text-ink-3 hover:text-ink-2",
      )}
    >
      <span className={cn("font-medium", active && "text-ink")}>{label}</span>
      <span className="hidden text-[10px] tracking-[0.06em] uppercase sm:inline">{detail}</span>
    </Link>
  );
}

export function ShadowNav({
  current,
  connected,
  lastSuccessAt,
}: {
  current: "operations" | "shadow" | "paper";
  connected: boolean;
  lastSuccessAt: string | null;
}) {
  return (
    <header className="sticky top-0 z-10 border-b border-line bg-canvas/92 backdrop-blur-sm">
      <div className="mx-auto flex h-14 max-w-[1520px] items-center gap-3 px-5 sm:px-6">
        <h1 className="text-[14px] leading-none font-semibold tracking-tight text-ink">
          AutoTrader
        </h1>

        <nav aria-label="Sections" className="ml-2 flex items-center gap-1">
          <Tab
            href="/"
            label="Operations"
            detail="Crypto · paper"
            active={current === "operations"}
          />
          <Tab
            href="/equity-shadow"
            label="Equity Shadow"
            detail="Observation · zero orders"
            active={current === "shadow"}
          />
          <Tab
            href="/equity-paper"
            label="Equity Paper"
            detail="EDA-1 · paper orders"
            active={current === "paper"}
          />
        </nav>

        <div className="ml-auto flex items-center gap-3">
          {current === "shadow" ? (
            <Tag title="This page shows recorded decisions. No order can be submitted, cancelled, or replaced by the process behind it.">
              Zero order mutation
            </Tag>
          ) : null}

          {current === "paper" ? (
            <Tag title="Orders on this page were really submitted, to a paper brokerage account. No real money is involved and this system has no live path.">
              Alpaca paper · no real money
            </Tag>
          ) : null}

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
