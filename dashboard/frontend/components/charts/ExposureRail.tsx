/**
 * The exposure rail: 0 ─────── target ── cap ── 100%, with the current
 * position marked on it.
 *
 * The band between target and cap is amber and the band past the cap is red,
 * so the rail says where the number sits relative to both lines without a
 * legend. The marker's colour is the row's tone - green on target, amber near
 * the cap, red past it - and the percentages are printed, not implied.
 */

import { percent } from "@/lib/format";
import type { Tone } from "@/lib/types";

import { cn } from "../ui";

const MARKER: Record<Tone, string> = {
  POSITIVE: "bg-pos",
  NEGATIVE: "bg-neg",
  ATTENTION: "bg-warn",
  NEUTRAL: "bg-ink",
  MUTED: "bg-ink-3",
  SHADOW: "bg-observe",
};

export function ExposureRail({
  current,
  target,
  cap,
  tone,
  compact = false,
}: {
  current: number | null;
  target: number | null;
  cap: number | null;
  tone: Tone;
  compact?: boolean;
}) {
  const clamp = (value: number) => Math.max(0, Math.min(1, value));
  const scale = (value: number) => `${clamp(value) * 100}%`;
  return (
    <div className={compact ? "" : "mt-2"}>
      <div
        className="relative h-2 w-full rounded-[3px] bg-line-strong/50"
        role="img"
        aria-label={`Current ${percent(current, 2)}${target !== null ? `, target ${percent(target, 0)}` : ""}${cap !== null ? `, hard cap ${percent(cap, 0)}` : ""}`}
      >
        {target !== null && cap !== null ? (
          <div
            className="absolute top-0 h-full bg-warn/25"
            style={{ left: scale(target), width: `${(clamp(cap) - clamp(target)) * 100}%` }}
          />
        ) : null}
        {cap !== null ? (
          <div
            className="absolute top-0 h-full rounded-r-[3px] bg-neg/25"
            style={{ left: scale(cap), width: `${(1 - clamp(cap)) * 100}%` }}
          />
        ) : null}
        {current !== null ? (
          <div
            className={cn("absolute top-0 h-full rounded-l-[3px]", tone === "NEGATIVE" ? "bg-neg/60" : "bg-accent/45")}
            style={{ width: scale(current) }}
          />
        ) : null}
        {target !== null ? (
          <div className="absolute top-[-2px] h-[calc(100%+4px)] w-px bg-ink" style={{ left: scale(target) }} />
        ) : null}
        {cap !== null ? (
          <div className="absolute top-[-2px] h-[calc(100%+4px)] w-px bg-neg" style={{ left: scale(cap) }} />
        ) : null}
        {current !== null ? (
          <div
            className={cn("absolute top-[-3px] size-[14px] -translate-x-1/2 rounded-full ring-2 ring-surface", MARKER[tone])}
            style={{ left: scale(current) }}
          />
        ) : null}
      </div>
      {compact ? null : (
        <div className="num mt-1 flex justify-between text-[10px] text-ink-3">
          <span>0%</span>
          {target !== null ? <span>target {percent(target, 0)}</span> : <span />}
          {cap !== null ? <span className="text-neg/80">cap {percent(cap, 0)}</span> : <span />}
          <span>100%</span>
        </div>
      )}
    </div>
  );
}
