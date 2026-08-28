/**
 * The primitives every panel is built from.
 *
 * Small on purpose. A card, a pill, a status dot, a utilization bar, a label,
 * an empty state, and the one component that renders "this could not be read".
 * Everything else is composition. Nothing here holds state, fetches, or knows
 * what a position is.
 */

import type { ReactNode } from "react";

import { unavailableLabel } from "@/lib/format";
import type { Amount, Tone, UnavailableReason } from "@/lib/types";

export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

const TONE_TEXT: Record<Tone, string> = {
  POSITIVE: "text-pos",
  NEGATIVE: "text-neg",
  ATTENTION: "text-warn",
  NEUTRAL: "text-ink",
  MUTED: "text-ink-3",
};

const TONE_DOT: Record<Tone, string> = {
  POSITIVE: "bg-pos",
  NEGATIVE: "bg-neg",
  ATTENTION: "bg-warn",
  NEUTRAL: "bg-ink-2",
  MUTED: "bg-ink-3",
};

export function toneText(tone: Tone): string {
  return TONE_TEXT[tone] ?? TONE_TEXT.NEUTRAL;
}

/** A 5px dot. The whole status vocabulary at a glance, in one column. */
export function Dot({ tone, className }: { tone: Tone; className?: string }) {
  return (
    <span
      aria-hidden
      className={cn(
        "inline-block size-[5px] shrink-0 rounded-full",
        TONE_DOT[tone] ?? TONE_DOT.NEUTRAL,
        className,
      )}
    />
  );
}

/**
 * A status pill: dot, then the status word.
 *
 * `emphasis` draws a hairline ring in the tone colour. It exists for exactly
 * one thing - an `UNKNOWN` order, the status that means nobody knows what the
 * broker did - and is not used decoratively anywhere else.
 */
export function Pill({
  tone = "NEUTRAL",
  children,
  emphasis = false,
  title,
}: {
  tone?: Tone;
  children: ReactNode;
  emphasis?: boolean;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-[4px] px-1.5 py-[3px]",
        "text-[10px] leading-none font-medium tracking-[0.06em] uppercase whitespace-nowrap",
        toneText(tone),
        emphasis ? "ring-1 ring-warn/45" : "ring-1 ring-line",
      )}
    >
      <Dot tone={tone} />
      {children}
    </span>
  );
}

/** A bordered outline pill with no status meaning. `PAPER`, `CRYPTO`, `BUY`. */
export function Tag({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center rounded-[4px] px-1.5 py-[3px] ring-1 ring-line",
        "text-[10px] leading-none font-medium tracking-[0.06em] text-ink-3 uppercase whitespace-nowrap",
      )}
    >
      {children}
    </span>
  );
}

export function Card({
  title,
  meta,
  children,
  className,
  bodyClassName,
}: {
  title?: string;
  meta?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section
      className={cn("rounded-card border border-line bg-surface", className)}
      aria-label={title}
    >
      {title ? (
        <header className="flex h-11 items-center justify-between gap-3 border-b border-line px-4">
          <h2 className="text-[13px] leading-none font-semibold tracking-tight text-ink">{title}</h2>
          {meta ? <div className="flex items-center gap-2">{meta}</div> : null}
        </header>
      ) : null}
      <div className={bodyClassName ?? "p-4"}>{children}</div>
    </section>
  );
}

/** A small label/value pair. The unit of every definition grid on the page. */
export function Field({
  label,
  children,
  title,
  className,
}: {
  label: string;
  children: ReactNode;
  title?: string;
  className?: string;
}) {
  return (
    <div className={cn("min-w-0", className)} title={title}>
      <div className="eyebrow text-ink-3">{label}</div>
      <div className="mt-1 truncate text-[13px] leading-tight text-ink">{children}</div>
    </div>
  );
}

/**
 * The rendering of a figure that could not be read.
 *
 * A dash and the reason, together. A bare dash would be indistinguishable from
 * a value of zero that happened to be formatted oddly, and the difference
 * between "flat" and "we cannot see the account" is the whole point.
 */
export function Unavailable({
  reason,
  className,
}: {
  reason: UnavailableReason | null | undefined;
  className?: string;
}) {
  return (
    <span className={cn("inline-flex items-baseline gap-1.5 text-ink-3", className)}>
      <span aria-hidden>—</span>
      <span className="text-[11px] leading-none">{unavailableLabel(reason)}</span>
    </span>
  );
}

/** An `Amount`, or the reason it is missing. */
export function Figure({
  value,
  render,
  className,
}: {
  value: Amount | null | undefined;
  render: (raw: number) => string;
  className?: string;
}) {
  if (!value || !value.available || value.value === null) {
    return <Unavailable reason={value?.unavailable_reason} className={className} />;
  }
  return <span className={cn("num", className)}>{render(value.value)}</span>;
}

/**
 * A horizontal utilization bar.
 *
 * Not a gauge and not an animation: a 3px track, a fill, and a colour that
 * changes only when the number means something - amber approaching the limit,
 * red past it. `value` is utilization, where 1 is the limit itself.
 */
export function Bar({ value, breached }: { value: number | null; breached: boolean }) {
  const known = value !== null && Number.isFinite(value);
  const filled = known ? Math.max(0, Math.min(1, value)) : 0;
  const fill = breached ? "bg-neg" : known && filled >= 0.8 ? "bg-warn" : "bg-accent";
  return (
    <div
      className="h-1 w-full overflow-hidden rounded-full bg-line-strong"
      role="img"
      aria-label={known ? `${Math.round(filled * 100)}% of limit used` : "Utilization unavailable"}
    >
      {known ? (
        <div className={cn("h-full rounded-full", fill)} style={{ width: `${filled * 100}%` }} />
      ) : null}
    </div>
  );
}

/** What a panel shows when there is genuinely nothing to show. */
export function Empty({ headline, detail }: { headline: string; detail?: ReactNode }) {
  return (
    <div className="flex min-h-[148px] flex-col items-center justify-center gap-1.5 px-4 py-10 text-center">
      <p className="text-[13px] text-ink-2">{headline}</p>
      {detail ? <p className="max-w-[42ch] text-[12px] text-ink-3">{detail}</p> : null}
    </div>
  );
}

/** A table header cell. */
export function Th({
  children,
  align = "left",
  className,
}: {
  children: ReactNode;
  align?: "left" | "right";
  className?: string;
}) {
  return (
    <th
      scope="col"
      className={cn(
        "eyebrow px-3 py-2 font-medium text-ink-3 whitespace-nowrap",
        align === "right" ? "text-right" : "text-left",
        className,
      )}
    >
      {children}
    </th>
  );
}

/** A table body cell. Numeric cells are right aligned and tabular. */
export function Td({
  children,
  align = "left",
  numeric = false,
  className,
}: {
  children: ReactNode;
  align?: "left" | "right";
  numeric?: boolean;
  className?: string;
}) {
  return (
    <td
      className={cn(
        "px-3 py-2.5 text-[12.5px] whitespace-nowrap",
        align === "right" || numeric ? "text-right" : "text-left",
        numeric && "num tracking-tight",
        className,
      )}
    >
      {children}
    </td>
  );
}
