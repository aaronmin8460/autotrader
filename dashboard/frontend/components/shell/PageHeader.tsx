"use client";

/**
 * A page's own heading: title, one line of context, and optional controls.
 *
 * The title is the largest text on a page apart from the account's own figure,
 * and it is the only `h1`. Everything else on the page is `h2` or lower, so the
 * heading outline a screen reader announces is the information architecture.
 */

import type { ReactNode } from "react";

export function PageHeader({
  title,
  context,
  actions,
}: {
  title: string;
  context?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-2">
      <div className="min-w-0">
        <h1 className="text-title font-semibold tracking-[-0.02em] text-ink">{title}</h1>
        {context ? <p className="mt-1 text-meta leading-snug text-ink-3">{context}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}
