/**
 * The allocation: one horizontal bar per slice, largest first, cash last.
 *
 * Plus a structure strip above it that makes the deployed-versus-reserve
 * shape obvious at a glance: equity, crypto if any, and cash, as one 100%
 * bar with the policy's target-gross mark on it. Every bar prints its
 * percentage; the length is a redundancy.
 */

import type { AllocationSlice } from "@/lib/portfolio";
import { money, percent } from "@/lib/format";

import { cn } from "../ui";

const KIND_FILL: Record<AllocationSlice["kind"], string> = {
  EQUITY: "bg-accent",
  CRYPTO: "bg-warn",
  CASH: "bg-ink-3",
  OTHER: "bg-line-strong",
};

export function StructureStrip({
  slices,
  targetGross,
  hardCap,
}: {
  slices: AllocationSlice[];
  targetGross: number | null;
  hardCap: number | null;
}) {
  const equity = slices.filter((slice) => slice.kind === "EQUITY" || slice.kind === "OTHER");
  const crypto = slices.filter((slice) => slice.kind === "CRYPTO");
  const cash = slices.filter((slice) => slice.kind === "CASH");
  const sum = (items: AllocationSlice[]) => items.reduce((total, slice) => total + (slice.fraction ?? 0), 0);
  const segments = [
    { key: "equity", label: "Equity", fraction: sum(equity), fill: "bg-accent" },
    { key: "crypto", label: "Crypto", fraction: sum(crypto), fill: "bg-warn" },
    { key: "cash", label: "Cash", fraction: sum(cash), fill: "bg-ink-3" },
  ].filter((segment) => segment.fraction > 0);

  return (
    <div>
      <div className="relative h-3 w-full overflow-hidden rounded-[3px] bg-line-strong/60" role="img" aria-label="Account structure">
        <div className="flex h-full w-full">
          {segments.map((segment) => (
            <div
              key={segment.key}
              className={cn("h-full", segment.fill)}
              style={{ width: `${Math.max(0, Math.min(1, segment.fraction)) * 100}%` }}
              title={`${segment.label} ${percent(segment.fraction, 1)}`}
            />
          ))}
        </div>
        {targetGross !== null ? (
          <div
            className="absolute top-0 h-full w-px bg-ink"
            style={{ left: `${targetGross * 100}%` }}
            title={`Target gross ${percent(targetGross, 0)}`}
          />
        ) : null}
        {hardCap !== null ? (
          <div
            className="absolute top-0 h-full w-px bg-neg"
            style={{ left: `${hardCap * 100}%` }}
            title={`Hard cap ${percent(hardCap, 0)}`}
          />
        ) : null}
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-ink-3">
        {segments.map((segment) => (
          <span key={segment.key} className="inline-flex items-center gap-1.5">
            <span className={cn("inline-block size-2 rounded-[2px]", segment.fill)} aria-hidden />
            {segment.label} <span className="num text-ink-2">{percent(segment.fraction, 1)}</span>
          </span>
        ))}
        {targetGross !== null ? (
          <span className="num">target {percent(targetGross, 0)}</span>
        ) : null}
        {hardCap !== null ? <span className="num text-neg/80">cap {percent(hardCap, 0)}</span> : null}
      </div>
    </div>
  );
}

export function AllocationBars({ slices }: { slices: AllocationSlice[] }) {
  const max = Math.max(...slices.map((slice) => slice.fraction ?? 0), 0.0001);
  return (
    <ul className="space-y-1.5" aria-label="Allocation by position">
      {slices.map((slice) => (
        <li key={slice.label} className="grid grid-cols-[64px_minmax(0,1fr)_56px_92px] items-center gap-2">
          <span
            className={cn(
              "truncate text-[12px] font-medium",
              slice.kind === "CASH" ? "text-ink-2" : "text-ink",
            )}
          >
            {slice.label}
          </span>
          <span className="h-2 w-full overflow-hidden rounded-[2px] bg-line-strong/40" aria-hidden>
            <span
              className={cn("block h-full rounded-[2px]", KIND_FILL[slice.kind])}
              style={{ width: `${((slice.fraction ?? 0) / max) * 100}%` }}
            />
          </span>
          <span className="num text-right text-[12px] text-ink">{percent(slice.fraction, 1)}</span>
          <span className="num text-right text-[11.5px] text-ink-3">{money(slice.value)}</span>
        </li>
      ))}
    </ul>
  );
}
