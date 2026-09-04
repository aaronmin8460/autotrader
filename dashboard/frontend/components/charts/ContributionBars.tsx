"use client";

/**
 * Unrealized P&L per position, as diverging horizontal bars.
 *
 * The figure is the broker's own: market value minus the broker's average
 * entry cost. It is not a history and not a realized figure - there is no
 * authoritative per-symbol realized P&L series in any store, so none is
 * drawn. Gains grow right in green, losses grow left in red, and every bar
 * prints its number.
 */

import type { Contribution } from "@/lib/portfolio";
import { signedMoney, signedPercent } from "@/lib/format";
import { useI18n } from "@/lib/i18n";

import { cn } from "../ui";

export function ContributionBars({ rows }: { rows: Contribution[] }) {
  const { t } = useI18n();
  const max = Math.max(...rows.map((row) => Math.abs(row.pnl)), 0.01);
  return (
    <ul className="space-y-1.5" aria-label={t("pnl.byPosition")}>
      {rows.map((row) => {
        const fraction = Math.abs(row.pnl) / max;
        const positive = row.pnl >= 0;
        return (
          <li key={row.symbol} className="grid grid-cols-[64px_minmax(0,1fr)_88px_64px] items-center gap-2">
            <span className="truncate text-table font-medium text-ink">{row.symbol}</span>
            <span className="relative h-2 w-full" aria-hidden>
              <span className="absolute top-0 bottom-0 left-1/2 w-px bg-active" />
              <span
                className={cn(
                  "absolute top-0 h-full rounded-xs",
                  positive ? "left-1/2 bg-pos" : "right-1/2 bg-neg",
                )}
                style={{ width: `${(fraction * 100) / 2}%` }}
              />
            </span>
            <span className={cn("num text-right text-table", positive ? "text-pos" : "text-neg")}>
              {signedMoney(row.pnl)}
            </span>
            <span className="num text-right text-meta text-ink-3">{signedPercent(row.fraction)}</span>
          </li>
        );
      })}
    </ul>
  );
}
