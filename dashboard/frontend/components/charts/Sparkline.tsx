"use client";

/**
 * A price sparkline: one line, one baseline, one colour for direction.
 *
 * No axes, no gridlines, no markers, no signals - it is a price visualization
 * and nothing more. Direction is carried by colour *and* by the change figure
 * the table prints beside it, so a reader who cannot see green from red still
 * reads it. Unavailable series render a small labelled dash rather than a
 * flat line that would look like a price.
 */

import type { ChartSeries } from "@/lib/charts";
import { chartUnavailableLabel } from "@/lib/charts";
import { useI18n } from "@/lib/i18n";
import { sparkPath } from "@/lib/spark";

import { cn } from "../ui";

export const SPARK_WIDTH = 96;
export const SPARK_HEIGHT = 26;

export function Sparkline({
  series,
  width = SPARK_WIDTH,
  height = SPARK_HEIGHT,
  className,
}: {
  series: ChartSeries | undefined;
  width?: number;
  height?: number;
  className?: string;
}) {
  const { t } = useI18n();
  if (!series) {
    return (
      <span className={cn("inline-block text-eyebrow text-ink-3", className)} aria-label={t("chart.loading")}>
        …
      </span>
    );
  }
  if (!series.available || series.points.length < 2) {
    return (
      <span
        className={cn("inline-block text-eyebrow text-ink-3", className)}
        title={chartUnavailableLabel(series.unavailable_reason)}
      >
        N/A
      </span>
    );
  }
  const closes = series.points.map((point) => point[4]);
  const first = series.first_close ?? closes[0] ?? 0;
  const last = series.last_close ?? closes[closes.length - 1] ?? 0;
  const up = last >= first;
  const min = Math.min(...closes, first);
  const max = Math.max(...closes, first);
  const span = max - min || 1;
  const baselineY = 2 + (height - 4) * (1 - (first - min) / span);
  const label = `${series.symbol} ${series.range}: ${up ? "up" : "down"} ${(Math.abs(series.change_fraction ?? 0) * 100).toFixed(2)}%`;

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      role="img"
      aria-label={label}
      className={cn("shrink-0 overflow-visible", className)}
    >
      <title>{label}</title>
      <line
        x1={0}
        x2={width}
        y1={baselineY}
        y2={baselineY}
        stroke="currentColor"
        strokeOpacity={0.18}
        strokeDasharray="2 3"
        className="text-ink-3"
      />
      <path
        d={sparkPath(closes, width, height)}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.4}
        strokeLinejoin="round"
        strokeLinecap="round"
        className={up ? "text-pos" : "text-neg"}
      />
    </svg>
  );
}
