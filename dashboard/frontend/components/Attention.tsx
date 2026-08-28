/**
 * The one banner on the page, and it only appears when something is wrong.
 *
 * A rail in the state's colour and a list of reasons - not a filled block.
 * `PAUSED` gets a tinted background because trading being blocked is the one
 * condition on this screen that genuinely warrants being loud; `ATTENTION`
 * does not, because a warning that shouts every time is a warning nobody
 * reads twice.
 */

import type { Overview } from "@/lib/types";

import { cn, toneText } from "./ui";

export function Attention({ overview }: { overview: Overview }) {
  const reasons = [...overview.attention, ...overview.notices];
  if (overview.system_state === "HEALTHY" && reasons.length === 0) return null;

  const paused = overview.system_state === "PAUSED";
  return (
    <div
      role="status"
      className={cn(
        "rounded-card border border-line bg-surface",
        "border-l-2 pr-4 pl-3.5",
        paused ? "tint-neg border-l-neg" : "border-l-warn",
      )}
    >
      <div className="py-3">
        <div
          className={cn(
            "text-[11px] leading-none font-medium tracking-[0.06em] uppercase",
            toneText(overview.system_state_tone),
          )}
        >
          {overview.system_state}
        </div>
        <ul className="mt-2 space-y-1">
          {reasons.map((reason) => (
            <li key={reason} className="text-[12.5px] leading-snug text-ink-2">
              {reason}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
