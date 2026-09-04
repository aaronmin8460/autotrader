"use client";

/**
 * The exposure rail: 0 ─── target ── cap ── end, with the current position
 * marked on it.
 *
 * **The rail is scaled to the limit, not to 100%.** A per-symbol cap of 11%
 * drawn on a 0–100% axis puts 89% of the track in the past-the-cap colour and
 * squeezes every number that matters into the first tenth of it — a healthy 9%
 * position rendered as a mostly-red bar. The domain is therefore derived from
 * the policy's own figures, so the distance between the current value, the
 * target and the hard cap is the thing the eye actually measures. The axis
 * labels print the domain, so the scale is stated rather than assumed.
 *
 * The band between target and cap is amber and the band past the cap is red,
 * so the rail says where the number sits relative to both lines without a
 * legend. The marker's colour is the row's tone — green on target, amber near
 * the cap, red past it — and every percentage is printed, not implied.
 */

import { percent } from "@/lib/format";
import { railDomain } from "@/lib/rail";
import { useI18n } from "@/lib/i18n";
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
  const { t } = useI18n();
  const max = railDomain(current, target, cap);
  const clamp = (value: number) => Math.max(0, Math.min(1, value / max));
  const scale = (value: number) => `${clamp(value) * 100}%`;

  return (
    <div className={compact ? "" : "mt-2.5"}>
      <div
        className="relative h-2 w-full rounded-xs bg-active/50"
        role="img"
        aria-label={`${percent(current, 2)}${target !== null ? `, ${t("account.target")} ${percent(target, 0)}` : ""}${
          cap !== null ? `, ${t("account.hardCap")} ${percent(cap, 0)}` : ""
        }`}
      >
        {target !== null && cap !== null ? (
          <div
            className="absolute top-0 h-full bg-warn/25"
            style={{ left: scale(target), width: `${(clamp(cap) - clamp(target)) * 100}%` }}
          />
        ) : null}
        {cap !== null ? (
          <div
            className="absolute top-0 h-full rounded-e-xs bg-neg/25"
            style={{ left: scale(cap), width: `${(1 - clamp(cap)) * 100}%` }}
          />
        ) : null}
        {current !== null ? (
          <div
            className={cn(
              "absolute top-0 h-full rounded-s-xs",
              tone === "NEGATIVE" ? "bg-neg/60" : "bg-accent/45",
            )}
            style={{ width: scale(current) }}
          />
        ) : null}
        {target !== null ? (
          <div
            className="absolute top-[-2px] h-[calc(100%+4px)] w-px bg-ink"
            style={{ left: scale(target) }}
          />
        ) : null}
        {cap !== null ? (
          <div
            className="absolute top-[-2px] h-[calc(100%+4px)] w-px bg-neg"
            style={{ left: scale(cap) }}
          />
        ) : null}
        {current !== null ? (
          <div
            className={cn(
              "absolute top-[-3px] size-[14px] -translate-x-1/2 rounded-full ring-2 ring-surface-1",
              MARKER[tone],
            )}
            style={{ left: scale(current) }}
          />
        ) : null}
      </div>
      {compact ? null : (
        <div className="num mt-1 flex justify-between text-eyebrow text-ink-3">
          <span>0%</span>
          {target !== null ? (
            <span>
              {t("account.target")} {percent(target, 0)}
            </span>
          ) : (
            <span />
          )}
          {cap !== null ? (
            <span className="text-neg/80">
              {t("account.hardCap")} {percent(cap, 0)}
            </span>
          ) : (
            <span />
          )}
          <span>{percent(max, 0)}</span>
        </div>
      )}
    </div>
  );
}
