/**
 * The header and the three-section navigation, on every page.
 *
 * Three sections, fixed: the account (Operations), the equity book that
 * trades (Equity Paper), and the observers that cannot (Shadows). Shadow
 * strategies are cards inside the third, never tabs of their own - a
 * navigation that grew a tab per experiment would stop being a navigation.
 *
 * The header carries the one-word verdict for the account, the environment,
 * and when the data on screen is from. No logo: a screenshot of an internal
 * tool does not need branding. Each section states its nature in its detail
 * line so a reader landing on a screenshot knows which of the three records
 * they are looking at without reading the body.
 */

import Link from "next/link";

import { clockUtc } from "@/lib/format";
import type { Tone } from "@/lib/types";

import { Dot, Tag, cn, toneText } from "./ui";

export type Section = "operations" | "paper" | "shadows";

export const SECTIONS: ReadonlyArray<{ key: Section; href: string; label: string; detail: string }> = [
  { key: "operations", href: "/", label: "Operations", detail: "Broker account" },
  { key: "paper", href: "/equity-paper", label: "Equity Paper", detail: "EDA-1 · paper orders" },
  { key: "shadows", href: "/shadows", label: "Shadows", detail: "Observation · zero orders" },
];

function Tab({ href, label, detail, active }: { href: string; label: string; detail: string; active: boolean }) {
  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex items-baseline gap-2 rounded-[5px] px-2.5 py-1.5 whitespace-nowrap",
        "text-[12.5px] leading-none transition-colors focus-visible:outline-2 focus-visible:outline-accent",
        active ? "bg-surface-2 text-ink ring-1 ring-line" : "text-ink-3 hover:text-ink-2",
      )}
    >
      <span className="font-medium">{label}</span>
      <span className="hidden text-[10px] tracking-[0.06em] uppercase lg:inline">{detail}</span>
    </Link>
  );
}

export function Nav({
  section,
  verdict,
  verdictTone,
  verdictTitle,
  badge,
  connected,
  lastSuccessAt,
}: {
  section: Section;
  /** The account's one-word state, shown on every page. */
  verdict: string | null;
  verdictTone: Tone;
  verdictTitle?: string;
  /** A section-specific qualifier: paper, or observation only. */
  badge?: { text: string; title: string; tone?: Tone };
  connected: boolean;
  lastSuccessAt: string | null;
}) {
  return (
    <header className="sticky top-0 z-20 border-b border-line bg-canvas/92 backdrop-blur-sm">
      <div className="mx-auto flex h-14 max-w-[1720px] items-center gap-3 px-5 sm:px-6">
        <h1 className="text-[14px] leading-none font-semibold tracking-tight text-ink">AutoTrader</h1>
        <Tag title="Broker paper trading. This repository has no live mode.">Paper</Tag>

        <nav aria-label="Sections" className="ml-2 flex items-center gap-1">
          {SECTIONS.map((item) => (
            <Tab
              key={item.key}
              href={item.href}
              label={item.label}
              detail={item.detail}
              active={item.key === section}
            />
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-3">
          {badge ? (
            <Tag tone={badge.tone} title={badge.title}>
              {badge.text}
            </Tag>
          ) : null}

          <span
            className={cn("inline-flex items-center gap-2", toneText(verdictTone))}
            title={verdictTitle}
          >
            <Dot tone={verdictTone} />
            <span className="text-[12px] leading-none font-medium tracking-[0.06em] uppercase">
              {verdict ?? "—"}
            </span>
          </span>

          <span className="hidden h-4 w-px bg-line sm:block" />

          <span className="hidden items-center gap-2 sm:flex">
            {connected ? null : (
              <span className="text-[10px] leading-none font-medium tracking-[0.06em] text-warn uppercase">
                Reconnecting
              </span>
            )}
            <span className={cn("num text-[12px] leading-none", connected ? "text-ink-2" : "text-ink-3")}>
              {lastSuccessAt ? `Last sync ${clockUtc(lastSuccessAt)} UTC` : "Awaiting first sync"}
            </span>
          </span>
        </div>
      </div>
    </header>
  );
}

export function Footer({ intervalSeconds }: { intervalSeconds: number }) {
  return (
    <footer className="mt-6 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line pt-4 text-[11px] text-ink-3">
      <span>Read-only view. Every dashboard API exposes GET routes only; nothing here can act.</span>
      <span>All times UTC.</span>
      <span className="num">Refreshes every {intervalSeconds}s.</span>
    </footer>
  );
}
