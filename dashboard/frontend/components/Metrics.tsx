/**
 * The four headline numbers.
 *
 * Four, and no more: equity, cash, the day's P&L, and how much of the account
 * is deployed. Each is one figure with one line of context under it - a caption
 * that says what the number is measured against, because a percentage with no
 * denominator on screen is a number an operator has to trust rather than read.
 *
 * The tiles are deliberately plain. Only the P&L carries colour, because only
 * the P&L has a direction that means something.
 */

import { amount, money, percent, signTone, signedMoney, signedPercent } from "@/lib/format";
import type { Amount, PrimaryMetrics } from "@/lib/types";

import { Figure, Unavailable, cn, toneText } from "./ui";

function Tile({
  label,
  value,
  caption,
  captionTitle,
  tone,
}: {
  label: string;
  value: React.ReactNode;
  caption: React.ReactNode;
  captionTitle?: string;
  tone?: "POSITIVE" | "NEGATIVE" | "NEUTRAL";
}) {
  return (
    <div className="rounded-card border border-line bg-surface px-4 py-3.5">
      <div className="eyebrow text-ink-3">{label}</div>
      <div
        className={cn(
          "num mt-2 text-[26px] leading-none font-semibold tracking-[-0.02em]",
          tone ? toneText(tone) : "text-ink",
        )}
      >
        {value}
      </div>
      <div className="mt-2 truncate text-[11.5px] leading-none text-ink-3" title={captionTitle}>
        {caption}
      </div>
    </div>
  );
}

function cashCaption(cash: Amount, equity: Amount): string {
  if (!cash.available || !equity.available || !equity.value) return "Settled paper cash";
  return `${percent(cash.value! / equity.value, 1)} of equity`;
}

export function Metrics({ metrics }: { metrics: PrimaryMetrics | null }) {
  if (!metrics) {
    return (
      <div className="rounded-card border border-line bg-surface px-4 py-6">
        <Unavailable reason="DATABASE_UNREADABLE" />
      </div>
    );
  }

  const pnlTone = metrics.daily_pnl.available ? signTone(metrics.daily_pnl.value) : undefined;
  const baselineDate = metrics.daily_pnl_baseline_date;

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Tile
        label="Portfolio equity"
        value={<Figure value={metrics.equity} render={money} />}
        caption="Alpaca paper account"
      />
      <Tile
        label="Cash"
        value={<Figure value={metrics.cash} render={money} />}
        caption={cashCaption(metrics.cash, metrics.equity)}
      />
      <Tile
        label="Daily P&L"
        tone={pnlTone}
        value={<Figure value={metrics.daily_pnl} render={signedMoney} />}
        caption={
          metrics.daily_pnl.available
            ? `${signedPercent(metrics.daily_pnl_fraction)} vs ${amount(
                metrics.daily_pnl_baseline,
              )} baseline`
            : "Needs live equity and a stored UTC baseline"
        }
        captionTitle={
          baselineDate
            ? `Measured against the equity first observed on ${baselineDate} UTC.`
            : undefined
        }
      />
      <Tile
        label="Total exposure"
        value={<Figure value={metrics.exposure} render={money} />}
        caption={
          metrics.exposure.available
            ? `${percent(metrics.exposure_fraction)} of equity · 30% limit`
            : "Needs a broker position read"
        }
      />
    </div>
  );
}
