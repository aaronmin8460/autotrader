"use client";

/**
 * The Live page's display atoms: four ways a figure can be missing, kept apart.
 *
 * The frozen contract is explicit that `null` means unknown and never zero, and
 * this page has four genuinely different reasons a number is not on screen.
 * Collapsing them into one blank would destroy the distinction an operator
 * needs most:
 *
 *   `$0.00`         the figure is known and is zero — a real, trustworthy fact
 *   UNKNOWN         the backend sent `null`: it does not know
 *   NOT AVAILABLE   the backend cannot know — a declared, permanent limitation
 *   STALE           the figure is real but was last true some time ago
 *
 * `Cash $0.00` and `Broker withdrawable cash NOT VERIFIED` are the worked
 * example: one is an empty account, the other is a number this broker's API
 * does not expose. They must never render the same way.
 *
 * Nothing here computes. Every component takes a decimal string the backend
 * produced and formats it through `lib/live-decimal`, which never converts to a
 * JavaScript number.
 */

import type { ReactNode } from "react";

import { useT } from "@/lib/i18n";
import {
  contractMoney,
  contractPercent,
  contractSignedMoney,
  contractSignedPercent,
  contractTone,
} from "@/lib/live-decimal";
import { cn, Tag, toneText } from "../ui";
import type { Tone } from "@/lib/types";

/**
 * What a figure the backend does not know looks like.
 *
 * The dash and the word together. A bare dash is indistinguishable from an
 * oddly formatted zero, and the difference between "flat" and "we cannot see
 * it" is the whole point of the distinction.
 */
export function Unknown({ label }: { label?: string }) {
  const t = useT();
  return (
    <span className="inline-flex items-baseline gap-1.5 text-ink-3">
      <span aria-hidden>—</span>
      <span className="text-meta leading-none">{label ?? t("live.status.unknown")}</span>
    </span>
  );
}

/**
 * A figure the backend cannot ever supply, as opposed to one it does not have.
 *
 * Used for `broker_withdrawable_cash`, which the contract declares always
 * `null` because this broker's Trading API exposes no such field. Rendering it
 * as `UNKNOWN` would imply it might resolve later; it will not.
 */
export function NotAvailable({ label }: { label?: string }) {
  const t = useT();
  return (
    <span className="inline-flex items-baseline gap-1.5 text-ink-3">
      <span aria-hidden>—</span>
      <span className="text-meta leading-none">{label ?? t("live.status.notAvailable")}</span>
    </span>
  );
}

/** A contract money string, or the honest reason it is absent. */
export function Money({
  value,
  className,
  tone,
  signed = false,
}: {
  value: string | null | undefined;
  className?: string;
  tone?: Tone;
  /** Show the sign always. For figures whose direction is the message. */
  signed?: boolean;
}) {
  if (value === null || value === undefined) return <Unknown />;
  const text = signed ? contractSignedMoney(value) : contractMoney(value);
  return <span className={cn("num", tone && toneText(tone), className)}>{text}</span>;
}

/**
 * A signed contract money string, coloured by its own sign.
 *
 * The tone is derived from the value rather than passed in, so a P&L figure
 * cannot be rendered green while reading negative.
 */
export function SignedMoney({ value, className }: { value: string | null | undefined; className?: string }) {
  if (value === null || value === undefined) return <Unknown />;
  return (
    <span className={cn("num", toneText(contractTone(value)), className)}>
      {contractSignedMoney(value)}
    </span>
  );
}

/** A contract ratio as a percentage, or the reason it is absent. */
export function Percent({
  value,
  signed = false,
  digits = 2,
  className,
}: {
  value: string | null | undefined;
  signed?: boolean;
  digits?: number;
  className?: string;
}) {
  if (value === null || value === undefined) return <Unknown />;
  const text = signed ? contractSignedPercent(value, digits) : contractPercent(value, digits);
  return (
    <span className={cn("num", signed && toneText(contractTone(value)), className)}>{text}</span>
  );
}

/**
 * A machine identifier: a policy id, a hash, a status code, a unit name.
 *
 * Rendered verbatim in both locales and never translated. `wrap` lets a long
 * hash break rather than be cut in half, because half a hash is worse than a
 * wrapped one.
 */
export function Identifier({
  value,
  wrap = false,
  className,
}: {
  value: string | null | undefined;
  wrap?: boolean;
  className?: string;
}) {
  if (!value) return <Unknown />;
  return (
    <span
      className={cn(
        "num text-table",
        // `max-w-full` with `inline-block` is what makes the clip actually
        // happen: `overflow-hidden` on a plain inline element does nothing, so
        // a 191px policy id was widening its own grid column and pushing the
        // page into horizontal scroll on a phone.
        wrap ? "break-all" : "inline-block max-w-full truncate align-bottom",
        className,
      )}
      title={value}
    >
      {value}
    </span>
  );
}

/** A boolean the backend answered, with `null` reading as unknown rather than false. */
export function YesNo({
  value,
  invertTone = false,
}: {
  value: boolean | null | undefined;
  /** When true, `true` is the state that deserves attention (e.g. "blocked"). */
  invertTone?: boolean;
}) {
  const t = useT();
  if (value === null || value === undefined) return <Unknown />;
  const attention = invertTone ? value : !value;
  return (
    <span className={cn("num", attention ? "text-warn" : "text-ink")}>
      {value ? t("live.withdrawal.on") : t("live.withdrawal.off")}
    </span>
  );
}

/**
 * The REAL MONEY marker.
 *
 * Text, not colour. It appears on every Live surface an operator could mistake
 * for a paper one, and the amber ring beside it is redundancy.
 */
export function LiveTag({ className }: { className?: string }) {
  const t = useT();
  return (
    <Tag tone="ATTENTION" title={t("live.paperContrast")} className={cn("ring-warn/40", className)}>
      {t("env.live.realMoney")}
    </Tag>
  );
}

/**
 * A toned sentence: the dot and colour of a `Status`, but it wraps.
 *
 * `Status` is `whitespace-nowrap`, which is correct for what it is for - a
 * status WORD like FILLED, CLEAN or DISARMED, which should never be broken
 * across lines. A sentence in one is a single unbreakable 600px line, and on
 * this page the sentences are the important part: "REAL-MONEY ORDER MUTATION
 * IS ENABLED" and "no withdrawal preview while below the adjusted high-water
 * mark" both have to survive a phone.
 *
 * So the tone vocabulary is reused and the nowrap is not. The dot remains
 * redundancy for the words, never the message.
 */
export function Statement({
  tone,
  children,
  emphasis = false,
  className,
}: {
  tone: Tone;
  children: ReactNode;
  /** Body size rather than meta. For the one sentence that must be read first. */
  emphasis?: boolean;
  className?: string;
}) {
  return (
    <p
      className={cn(
        "flex items-baseline gap-2 font-medium",
        emphasis ? "text-body" : "text-meta leading-snug",
        toneText(tone),
        className,
      )}
    >
      <span
        aria-hidden
        className={cn(
          "mt-[0.4em] inline-block size-[5px] shrink-0 rounded-full",
          tone === "POSITIVE"
            ? "bg-pos"
            : tone === "NEGATIVE"
              ? "bg-neg"
              : tone === "ATTENTION"
                ? "bg-warn"
                : tone === "SHADOW"
                  ? "bg-observe"
                  : tone === "MUTED"
                    ? "bg-ink-3"
                    : "bg-ink-2",
        )}
      />
      <span className="min-w-0">{children}</span>
    </p>
  );
}

/** A short explanatory line under a group of figures. */
export function Note({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <p className={cn("max-w-[92ch] text-meta leading-relaxed text-ink-3", className)}>{children}</p>
  );
}
