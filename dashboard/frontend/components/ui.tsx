"use client";

/**
 * The primitives every panel is built from — AUTOTRADER INSTITUTIONAL GLASS.
 *
 * Three surface levels and nothing between them:
 *
 *   LEVEL 0  the page. `bg-bg`, no border, no card.
 *   LEVEL 1  `Surface` / `Card`. Opaque, one hairline, `--radius-md`. Every
 *            number on this application lives here. Grouping *inside* a
 *            Level 1 surface is done with space and type, never with another
 *            box - there are no bordered boxes inside bordered boxes.
 *   LEVEL 2  `FloatingSurface`, `Drawer`, the sidebar and the top bar. The
 *            only translucency in the system. Chrome, never content.
 *
 * Colour carries five meanings and status always also carries a word: every
 * `Pill` and `Status` prints its text and the dot beside it is a redundancy,
 * not the message. Nothing here fetches, and nothing here knows what a
 * position is.
 */

import { useEffect, useId, useRef, type ReactNode } from "react";

import { useI18n, useT } from "@/lib/i18n";
import type { MessageKey } from "@/lib/i18n";
import type { Freshness } from "@/lib/freshness";
import type { Amount, Tone, UnavailableReason } from "@/lib/types";

export { freshnessOf } from "@/lib/freshness";
export type { Freshness } from "@/lib/freshness";

export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

/* ------------------------------------------------------------------ tone -- */

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
  NEGATIVE: "ring-neg/50",
  ATTENTION: "ring-warn/50",
  NEUTRAL: "ring-subtle",
  MUTED: "ring-subtle",
  SHADOW: "ring-observe/50",
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
 * "look here" — an `UNKNOWN` order, a SHADOW label — and is not decorative.
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
        "inline-flex items-center gap-1.5 rounded-xs px-1.5 py-[3px]",
        "text-eyebrow font-medium tracking-[0.06em] whitespace-nowrap uppercase",
        toneText(tone),
        "ring-1",
        emphasis ? TONE_RING[tone] : "ring-subtle",
        className,
      )}
    >
      <Dot tone={tone} />
      {children}
    </span>
  );
}

/** Alias with the name the design system uses. Same component. */
export const StatusBadge = Pill;

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
        "inline-flex items-center rounded-xs px-1.5 py-[3px] ring-1 ring-subtle",
        "text-eyebrow font-medium tracking-[0.06em] whitespace-nowrap uppercase",
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
        "inline-flex items-center gap-1.5 font-medium tracking-[0.06em] whitespace-nowrap uppercase",
        size === "md" ? "text-body" : "text-meta",
        toneText(tone),
      )}
    >
      <Dot tone={tone} />
      {children}
    </span>
  );
}

/**
 * The observation-only marker. Violet, always paired with its word, and never
 * applied to anything that can place an order.
 */
export function StrategyBadge({ kind, className }: { kind: "PAPER" | "SHADOW"; className?: string }) {
  const t = useT();
  return kind === "SHADOW" ? (
    <Tag tone="SHADOW" title={t("strategies.observationOnlyHint")} className={className}>
      {t("strategies.observationOnly")}
    </Tag>
  ) : (
    <Tag title={t("strategies.noRealMoneyHint")} className={className}>
      {t("strategies.noRealMoney")}
    </Tag>
  );
}

/* -------------------------------------------------------------- surfaces -- */

/** LEVEL 1. The opaque surface every figure sits on. */
export function Surface({
  children,
  className,
  tone,
  id,
  label,
  as: Element = "section",
}: {
  children: ReactNode;
  className?: string;
  /** `SHADOW` tints the surface so an observation panel never looks like an account panel. */
  tone?: "SHADOW";
  id?: string;
  label?: string;
  as?: "section" | "div" | "article";
}) {
  return (
    <Element
      id={id}
      aria-label={label}
      className={cn("panel", tone === "SHADOW" && "tint-observe", className)}
    >
      {children}
    </Element>
  );
}

/** LEVEL 2. Chrome only — never a number that has to be read precisely. */
export function FloatingSurface({
  children,
  className,
  role,
  label,
}: {
  children: ReactNode;
  className?: string;
  role?: string;
  label?: string;
}) {
  return (
    <div role={role} aria-label={label} className={cn("floating rounded-lg border shadow-float", className)}>
      {children}
    </div>
  );
}

/**
 * A section heading: an eyebrow-weight title, optional meta, optional link.
 *
 * Used both as a panel's own header and as a standalone heading above a group
 * of panels, so a page can group by typography instead of by nesting boxes.
 */
export function SectionHeader({
  title,
  meta,
  action,
  className,
  level = 2,
}: {
  title: ReactNode;
  meta?: ReactNode;
  action?: ReactNode;
  className?: string;
  level?: 1 | 2 | 3;
}) {
  const Heading = (`h${level}` as const) satisfies keyof React.JSX.IntrinsicElements;
  return (
    <div className={cn("flex min-h-8 flex-wrap items-center justify-between gap-x-3 gap-y-2", className)}>
      <Heading className="text-heading font-semibold tracking-tight text-ink">{title}</Heading>
      {meta || action ? (
        <div className="flex flex-wrap items-center justify-end gap-2">
          {meta}
          {action}
        </div>
      ) : null}
    </div>
  );
}

/** A LEVEL 1 surface with a header. The workhorse of every page. */
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
  tone?: "SHADOW";
  id?: string;
}) {
  return (
    <Surface
      id={id}
      tone={tone}
      className={className}
      label={typeof title === "string" ? title : undefined}
    >
      {title ? <SectionHeader title={title} meta={meta} className="px-4 pt-3 pb-2" /> : null}
      <div className={bodyClassName ?? "px-4 pb-4"}>{children}</div>
    </Surface>
  );
}

/* ------------------------------------------------------------ data atoms -- */

/** A small label/value pair. The unit of every definition grid. */
export function Field({
  label,
  children,
  title,
  className,
  wrap = false,
}: {
  label: string;
  children: ReactNode;
  title?: string;
  className?: string;
  /** Let a long authoritative identifier wrap rather than be cut in half. */
  wrap?: boolean;
}) {
  return (
    <div className={cn("min-w-0", className)}>
      <div className={cn("eyebrow text-ink-3", title && "hint inline-block")} title={title}>
        {label}
      </div>
      <div
        className={cn(
          "mt-1 text-body leading-tight text-ink",
          wrap ? "break-words" : "truncate",
        )}
      >
        {children}
      </div>
    </div>
  );
}

/**
 * A summary tile: eyebrow, one prominent value, one or two lines of context.
 *
 * `size` is the numeric hierarchy of the whole application: `display` is the
 * account's own figure and appears once per page; `value` is a section's
 * headline; `sm` is a supporting figure. Nothing else may be large.
 */
export function MetricBlock({
  label,
  value,
  context,
  tone,
  title,
  size = "value",
  children,
  className,
}: {
  label: string;
  value: ReactNode;
  context?: ReactNode;
  tone?: Tone;
  title?: string;
  size?: "display" | "value" | "sm";
  children?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("min-w-0", className)}>
      <div className={cn("eyebrow text-ink-3", title && "hint inline-block")} title={title}>
        {label}
      </div>
      <div
        className={cn(
          "num mt-2 font-semibold tracking-[-0.02em]",
          size === "display" ? "text-display" : size === "value" ? "text-value" : "text-value-sm",
          tone ? toneText(tone) : "text-ink",
        )}
      >
        {value}
      </div>
      {context ? <div className="mt-2 text-meta leading-snug text-ink-3">{context}</div> : null}
      {children}
    </div>
  );
}

/** Backwards-compatible alias: a `MetricBlock` on its own Level 1 surface. */
export function Metric(props: Parameters<typeof MetricBlock>[0]) {
  return (
    <div className="panel px-4 py-3.5">
      <MetricBlock {...props} />
    </div>
  );
}

/** Why a figure is missing, in the reader's language. */
export function useUnavailableLabel(): (reason: UnavailableReason | null | undefined) => string {
  const t = useT();
  return (reason) => {
    const key: MessageKey =
      reason === "BROKER_NOT_CONFIGURED"
        ? "reason.brokerNotConfigured"
        : reason === "BROKER_UNREADABLE"
          ? "reason.brokerUnreadable"
          : reason === "DATABASE_UNREADABLE"
            ? "reason.databaseUnreadable"
            : reason === "NOT_RECORDED"
              ? "reason.notRecorded"
              : "reason.unavailable";
    return t(key);
  };
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
  const label = useUnavailableLabel();
  return (
    <span className={cn("inline-flex items-baseline gap-1.5 text-ink-3", className)}>
      <span aria-hidden>—</span>
      <span className="text-meta leading-none">{label(reason)}</span>
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
 * Not a gauge and not an animation: a thin track, a fill, and a colour that
 * changes only when the number means something — amber approaching the limit,
 * red past it. `value` is utilization, where 1 is the limit itself.
 */
export function Bar({ value, breached }: { value: number | null; breached: boolean }) {
  const known = value !== null && Number.isFinite(value);
  const filled = known ? Math.max(0, Math.min(1, value)) : 0;
  const fill = breached ? "bg-neg" : known && filled >= 0.8 ? "bg-warn" : "bg-accent";
  return (
    <div
      className="h-1 w-full overflow-hidden rounded-full bg-active"
      role="img"
      aria-label={known ? `${Math.round(filled * 100)}% of limit used` : "Utilization unavailable"}
    >
      {known ? (
        <div className={cn("h-full rounded-full", fill)} style={{ width: `${filled * 100}%` }} />
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------- states of a panel -- */

/** Calm loading. A block of the right shape, never a spinner over real data. */
export function Skeleton({ className }: { className?: string }) {
  return <span aria-hidden className={cn("skeleton block", className)} />;
}

/** The loading state of a metric row: right shape, right count, no zeroes. */
export function MetricSkeleton({ count = 4 }: { count?: number }) {
  const t = useT();
  return (
    <div
      className="grid grid-cols-2 gap-3 xl:grid-cols-4"
      role="status"
      aria-busy="true"
      aria-label={t("common.loading")}
    >
      {Array.from({ length: count }, (_, index) => (
        <div key={index} className="panel px-4 py-3.5">
          <Skeleton className="h-2.5 w-24" />
          <Skeleton className="mt-3 h-7 w-36" />
          <Skeleton className="mt-3 h-2.5 w-full" />
        </div>
      ))}
    </div>
  );
}

/** The loading state of a table: the real header, skeleton rows beneath it. */
export function TableSkeleton({ rows = 6, columns = 6 }: { rows?: number; columns?: number }) {
  const t = useT();
  return (
    <div className="px-4 pb-4" role="status" aria-busy="true" aria-label={t("common.loading")}>
      {Array.from({ length: rows }, (_, row) => (
        <div key={row} className="flex items-center gap-3 border-b border-subtle/60 py-2.5 last:border-0">
          {Array.from({ length: columns }, (_, column) => (
            <Skeleton
              key={column}
              className={cn("h-2.5", column === 0 ? "w-16" : "flex-1")}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

/**
 * What a panel shows when there is genuinely nothing to show.
 *
 * Distinct from `ErrorState` on purpose: "the account holds nothing" and "we
 * could not read the account" must never look the same.
 */
export function EmptyState({ headline, detail }: { headline: string; detail?: ReactNode }) {
  return (
    <div className="flex min-h-[120px] flex-col items-center justify-center gap-1.5 px-4 py-8 text-center">
      <p className="text-body text-ink-2">{headline}</p>
      {detail ? <p className="max-w-[46ch] text-meta leading-snug text-ink-3">{detail}</p> : null}
    </div>
  );
}

export const Empty = EmptyState;

/** A backend that did not answer, or a record that cannot be read. */
export function ErrorState({
  headline,
  detail,
  tone = "ATTENTION",
}: {
  headline: string;
  detail?: ReactNode;
  tone?: Tone;
}) {
  return (
    <div className="flex min-h-[120px] flex-col items-center justify-center gap-2 px-4 py-8 text-center">
      <Status tone={tone}>{headline}</Status>
      {detail ? <p className="max-w-[52ch] text-meta leading-snug text-ink-3">{detail}</p> : null}
    </div>
  );
}

/**
 * A metric this build genuinely does not compute.
 *
 * Visually distinct from both "empty" and "error" because it is neither: the
 * number is not missing, it does not exist. Used for realized P&L and for the
 * account-equity curve, and it always states why.
 */
export function NotTracked({ headline, detail }: { headline: string; detail: ReactNode }) {
  const t = useT();
  return (
    <div className="flex min-h-[104px] flex-col justify-center gap-2 px-4 py-6">
      <div className="flex flex-wrap items-center gap-2">
        <Tag>{t("common.notTracked")}</Tag>
        <span className="text-body text-ink-2">{headline}</span>
      </div>
      <p className="max-w-[78ch] text-meta leading-relaxed text-ink-3">{detail}</p>
    </div>
  );
}

/* ------------------------------------------------------------ table atoms -- */

export function Th({
  children,
  align = "left",
  className,
  title,
  scope = "col",
}: {
  children: ReactNode;
  align?: "left" | "right";
  className?: string;
  title?: string;
  scope?: "col" | "row";
}) {
  return (
    <th
      scope={scope}
      title={title}
      className={cn(
        "eyebrow px-3 py-2 font-medium whitespace-nowrap text-ink-3",
        align === "right" ? "text-right" : "text-left",
        title && "hint",
        className,
      )}
    >
      {children}
    </th>
  );
}

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
        "px-3 py-2 text-table whitespace-nowrap",
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
 * The shell every dense table shares: horizontal scroll inside the panel,
 * a sticky header, and a caption for screen readers.
 */
export function DataTable({
  caption,
  minWidth,
  head,
  children,
  sticky = true,
}: {
  caption: string;
  /**
   * A literal Tailwind min-width class, e.g. `"min-w-[880px]"`.
   *
   * Deliberately a class and not a number. The deployed CSP is `style-src
   * 'self'` with no `style-src-attr`, so an inline `style` attribute present in
   * SERVER-RENDERED markup is blocked by the browser — and a static table (the
   * System page's record list) is exactly that. Dynamic geometry elsewhere is
   * written after hydration through the CSSOM, which the policy permits; a
   * literal class needs neither exception.
   */
  minWidth: string;
  head: ReactNode;
  children: ReactNode;
  sticky?: boolean;
}) {
  return (
    <div className="scroll-x">
      <table className={cn("w-full border-collapse", minWidth, sticky && "sticky-head")}>
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b border-subtle">{head}</tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

/** A body row. `onOpen` makes it keyboard-operable without leaving `<tr>`. */
export function Tr({
  children,
  onOpen,
  label,
  selected = false,
  tint,
  className,
}: {
  children: ReactNode;
  onOpen?: () => void;
  label?: string;
  selected?: boolean;
  tint?: "warn" | "neg";
  className?: string;
}) {
  return (
    <tr
      role={onOpen ? "button" : undefined}
      tabIndex={onOpen ? 0 : undefined}
      aria-label={onOpen ? label : undefined}
      aria-pressed={onOpen && selected ? true : undefined}
      onClick={onOpen}
      onKeyDown={
        onOpen
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onOpen();
              }
            }
          : undefined
      }
      className={cn(
        "border-b border-subtle/60 last:border-0",
        onOpen && "row-link hover:bg-surface-2",
        selected && "bg-surface-3",
        tint === "warn" && "tint-warn",
        tint === "neg" && "tint-neg",
        className,
      )}
    >
      {children}
    </tr>
  );
}

/* ---------------------------------------------------------------- controls -- */

/**
 * A segmented range selector. Real buttons, so the keyboard works; the
 * pressed one is announced as such.
 */
export function SegmentedTimeRange<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: ReadonlyArray<T>;
  value: T;
  onChange: (next: T) => void;
  label: string;
}) {
  return (
    <div role="group" aria-label={label} className="inline-flex rounded-sm ring-1 ring-subtle">
      {options.map((option) => (
        <button
          key={option}
          type="button"
          aria-pressed={option === value}
          onClick={() => onChange(option)}
          className={cn(
            "num px-2 py-1 text-meta leading-none font-medium tracking-[0.04em]",
            "first:rounded-l-sm last:rounded-r-sm transition-colors duration-(--duration-instant)",
            "focus-visible:outline-2 focus-visible:outline-accent",
            option === value ? "bg-surface-3 text-ink" : "text-ink-3 hover:text-ink-2",
          )}
        >
          {option}
        </button>
      ))}
    </div>
  );
}

export const RangeSelector = SegmentedTimeRange;

/**
 * A right-hand drawer. `Escape` closes it, focus lands inside on open, focus
 * is trapped while it is open, and the scrim is inert to the page behind.
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
  const t = useT();
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLElement | null>(null);
  const headingId = useId();

  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab" || !panelRef.current) return;
      const focusable = panelRef.current.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"]), input, select, textarea',
      );
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-(--z-overlay)" role="presentation">
      <div className="motion-fade absolute inset-0 bg-bg/75" onClick={onClose} aria-hidden />
      <aside
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        className={cn(
          "motion-drawer overlay-surface absolute top-0 right-0 flex h-full w-full max-w-[780px]",
          "flex-col border-l border-subtle",
        )}
      >
        <header className="flex items-center justify-between gap-3 border-b border-subtle px-5 py-3">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <h2 id={headingId} className="text-value-sm font-semibold tracking-tight text-ink">
              {title}
            </h2>
            {meta}
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            className={cn(
              "rounded-xs px-2 py-1 text-meta font-medium tracking-[0.06em] uppercase",
              "text-ink-3 ring-1 ring-subtle hover:text-ink focus-visible:outline-2 focus-visible:outline-accent",
            )}
          >
            {t("common.closeEsc")}
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
      </aside>
    </div>
  );
}

/* --------------------------------------------------------------- freshness -- */

const FRESHNESS_TONE: Record<Freshness, Tone> = {
  FRESH: "POSITIVE",
  STALE: "ATTENTION",
  WAITING: "MUTED",
  OFFLINE: "NEGATIVE",
};

const FRESHNESS_KEY: Record<Freshness, MessageKey> = {
  FRESH: "status.data.fresh",
  STALE: "status.data.stale",
  WAITING: "status.data.waiting",
  OFFLINE: "status.data.offline",
};

/** The word, the dot, and — only when it is not fresh — how old it is. */
export function FreshnessIndicator({
  state,
  ageSeconds,
  label,
}: {
  state: Freshness;
  ageSeconds: number | null;
  label?: string;
}) {
  const { t } = useI18n();
  return (
    <span className="inline-flex items-center gap-2">
      {label ? <span className="eyebrow text-ink-3">{label}</span> : null}
      <Status
        tone={FRESHNESS_TONE[state]}
        title={state === "STALE" ? t("status.staleHint") : undefined}
      >
        {t(FRESHNESS_KEY[state])}
      </Status>
      {state !== "FRESH" && ageSeconds !== null ? (
        <span className="num text-meta text-ink-3">{ageSeconds}s</span>
      ) : null}
    </span>
  );
}
