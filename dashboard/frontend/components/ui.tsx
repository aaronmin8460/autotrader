"use client";

/**
 * The primitives every panel is built from.
 *
 * Small on purpose. A card, a pill, a status dot, a utilization bar, a label,
 * a metric tile, a range selector, a drawer, an empty state, and the one
 * component that renders "this could not be read". Everything else is
 * composition. Nothing here fetches or knows what a position is.
 *
 * Colour carries five meanings and status always also carries a word: every
 * `Pill` and `Status` prints its text, and the dot beside it is a redundancy,
 * not the message.
 */

import { useEffect, useRef, type ReactNode } from "react";

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
  SHADOW: "text-observe",
};

const TONE_DOT: Record<Tone, string> = {
  POSITIVE: "bg-pos",
  NEGATIVE: "bg-neg",
  ATTENTION: "bg-warn",
  NEUTRAL: "bg-ink-2",
  MUTED: "bg-ink-3",
  SHADOW: "bg-observe",
};

const TONE_RING: Record<Tone, string> = {
  POSITIVE: "ring-pos/40",
  NEGATIVE: "ring-neg/45",
  ATTENTION: "ring-warn/45",
  NEUTRAL: "ring-line",
  MUTED: "ring-line",
  SHADOW: "ring-observe/45",
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
 * `emphasis` draws a ring in the tone colour. It exists for things that mean
 * "look here" - an `UNKNOWN` order, a SHADOW label - and is not decorative.
 */
export function Pill({
  tone = "NEUTRAL",
  children,
  emphasis = false,
  title,
  className,
}: {
  tone?: Tone;
  children: ReactNode;
  emphasis?: boolean;
  title?: string;
  className?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-[4px] px-1.5 py-[3px]",
        "text-[10px] leading-none font-medium tracking-[0.06em] uppercase whitespace-nowrap",
        toneText(tone),
        "ring-1",
        emphasis ? TONE_RING[tone] : "ring-line",
        className,
      )}
    >
      <Dot tone={tone} />
      {children}
    </span>
  );
}

/** A bordered outline pill with no status meaning. `PAPER`, `CRYPTO`, `BUY`. */
export function Tag({
  children,
  title,
  tone,
  className,
}: {
  children: ReactNode;
  title?: string;
  tone?: Tone;
  className?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center rounded-[4px] px-1.5 py-[3px] ring-1 ring-line",
        "text-[10px] leading-none font-medium tracking-[0.06em] uppercase whitespace-nowrap",
        tone ? toneText(tone) : "text-ink-3",
        className,
      )}
    >
      {children}
    </span>
  );
}

/** A status word with its dot, for headers and table cells. */
export function Status({
  tone,
  children,
  title,
  size = "sm",
}: {
  tone: Tone;
  children: ReactNode;
  title?: string;
  size?: "sm" | "md";
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1.5 leading-none font-medium tracking-[0.06em] uppercase whitespace-nowrap",
        size === "md" ? "text-[12px]" : "text-[11px]",
        toneText(tone),
      )}
    >
      <Dot tone={tone} />
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
  tone,
  id,
}: {
  title?: ReactNode;
  meta?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  /** `SHADOW` tints the card so an observation panel never looks like an account panel. */
  tone?: "SHADOW";
  id?: string;
}) {
  return (
    <section
      id={id}
      className={cn("card", tone === "SHADOW" && "tint-observe", className)}
      aria-label={typeof title === "string" ? title : undefined}
    >
      {title ? (
        <header className="flex min-h-11 items-center justify-between gap-3 px-4 pt-3 pb-2">
          <h2 className="text-[13px] leading-none font-semibold tracking-tight text-ink">{title}</h2>
          {meta ? <div className="flex flex-wrap items-center justify-end gap-2">{meta}</div> : null}
        </header>
      ) : null}
      <div className={bodyClassName ?? "px-4 pb-4"}>{children}</div>
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
    <div className={cn("min-w-0", className)}>
      <div className={cn("eyebrow text-ink-3", title && "hint inline-block")} title={title}>
        {label}
      </div>
      <div className="mt-1 truncate text-[13px] leading-tight text-ink">{children}</div>
    </div>
  );
}

/**
 * A summary tile: one eyebrow, one prominent value, one or two lines of context.
 *
 * The value is the point; the context says what it is measured against, so a
 * percentage with no denominator on screen never has to be trusted.
 */
export function Metric({
  label,
  value,
  context,
  tone,
  title,
  children,
}: {
  label: string;
  value: ReactNode;
  context?: ReactNode;
  tone?: Tone;
  title?: string;
  children?: ReactNode;
}) {
  return (
    <div className="card px-4 py-3.5">
      <div className={cn("eyebrow text-ink-3", title && "hint inline-block")} title={title}>
        {label}
      </div>
      <div
        className={cn(
          "num mt-2 text-[26px] leading-none font-semibold tracking-[-0.02em]",
          tone ? toneText(tone) : "text-ink",
        )}
      >
        {value}
      </div>
      {context ? <div className="mt-2 text-[11.5px] leading-snug text-ink-3">{context}</div> : null}
      {children}
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
    <div className="flex min-h-[120px] flex-col items-center justify-center gap-1.5 px-4 py-8 text-center">
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
  title,
}: {
  children: ReactNode;
  align?: "left" | "right";
  className?: string;
  title?: string;
}) {
  return (
    <th
      scope="col"
      title={title}
      className={cn(
        "eyebrow px-3 py-2 font-medium text-ink-3 whitespace-nowrap",
        align === "right" ? "text-right" : "text-left",
        title && "hint",
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
  title,
}: {
  children: ReactNode;
  align?: "left" | "right";
  numeric?: boolean;
  className?: string;
  title?: string;
}) {
  return (
    <td
      title={title}
      className={cn(
        "px-3 py-2 text-[12.5px] whitespace-nowrap",
        align === "right" || numeric ? "text-right" : "text-left",
        numeric && "num tracking-tight",
        className,
      )}
    >
      {children}
    </td>
  );
}

/**
 * A segmented range selector. Real buttons, so the keyboard works; the
 * pressed one is announced as such.
 */
export function RangeSelector<T extends string>({
  options,
  value,
  onChange,
  label = "Range",
}: {
  options: ReadonlyArray<T>;
  value: T;
  onChange: (next: T) => void;
  label?: string;
}) {
  return (
    <div role="group" aria-label={label} className="inline-flex rounded-[6px] ring-1 ring-line">
      {options.map((option) => (
        <button
          key={option}
          type="button"
          aria-pressed={option === value}
          onClick={() => onChange(option)}
          className={cn(
            "px-2 py-1 text-[11px] leading-none font-medium tracking-[0.04em]",
            "first:rounded-l-[6px] last:rounded-r-[6px] focus-visible:outline-2 focus-visible:outline-accent",
            option === value ? "bg-surface-2 text-ink" : "text-ink-3 hover:text-ink-2",
          )}
        >
          {option}
        </button>
      ))}
    </div>
  );
}

/**
 * A right-hand drawer. `Escape` closes it, focus lands inside on open, and
 * the page behind it is inert to pointer events. Reduced-motion users get
 * no slide.
 */
export function Drawer({
  open,
  title,
  onClose,
  children,
  meta,
}: {
  open: boolean;
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  meta?: ReactNode;
}) {
  const closeRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-30" role="presentation">
      <div className="absolute inset-0 bg-canvas/70" onClick={onClose} aria-hidden />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : "Detail"}
        className="drawer-panel absolute top-0 right-0 flex h-full w-full max-w-[760px] flex-col border-l border-line bg-surface shadow-2xl"
      >
        <header className="flex items-center justify-between gap-3 border-b border-line px-5 py-3">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <h2 className="text-[15px] leading-none font-semibold tracking-tight text-ink">{title}</h2>
            {meta}
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            className="rounded-[4px] px-2 py-1 text-[11px] font-medium tracking-[0.06em] text-ink-3 uppercase ring-1 ring-line hover:text-ink focus-visible:outline-2 focus-visible:outline-accent"
          >
            Close · Esc
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
      </aside>
    </div>
  );
}
