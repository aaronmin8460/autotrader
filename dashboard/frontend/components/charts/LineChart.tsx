"use client";

/**
 * The symbol detail chart: a close line with a few time labels, a price
 * scale, an optional average-entry line, and optional fill markers.
 *
 * Deliberately plain. There are no indicators, no volume pane, no candles -
 * the chart answers "what has the price done over this range, and where did
 * this book buy and sell". Markers are drawn only for real broker fills the
 * caller passes in, and each carries its own label so the marker can never be
 * mistaken for a signal.
 *
 * Hover (or the arrow keys, once the chart is focused) moves a cursor and
 * prints the bar under it. Reduced-motion users see no transition anywhere.
 */

import { useMemo, useState, type KeyboardEvent, type MouseEvent } from "react";

import { chartUnavailableLabel, type ChartSeries } from "@/lib/charts";
import { money } from "@/lib/format";
import { useI18n } from "@/lib/i18n";

import { cn } from "../ui";

export interface ChartMarker {
  at: string;
  side: "BUY" | "SELL";
  price: number | null;
  label: string;
}

const W = 720;
const H = 260;
const PAD = { top: 14, right: 64, bottom: 26, left: 8 };

function nearestIndex(stamps: number[], target: number): number {
  let best = 0;
  let distance = Number.POSITIVE_INFINITY;
  stamps.forEach((stamp, index) => {
    const delta = Math.abs(stamp - target);
    if (delta < distance) {
      distance = delta;
      best = index;
    }
  });
  return best;
}

function timeLabel(iso: string, range: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const hh = at.getUTCHours().toString().padStart(2, "0");
  const mm = at.getUTCMinutes().toString().padStart(2, "0");
  const dd = at.getUTCDate().toString().padStart(2, "0");
  const month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][
    at.getUTCMonth()
  ];
  if (range === "1D") return `${hh}:${mm}`;
  if (range === "5D") return `${dd} ${month} ${hh}:${mm}`;
  return `${dd} ${month}`;
}

export function LineChart({
  series,
  entryPrice,
  markers = [],
  className,
}: {
  series: ChartSeries | undefined;
  entryPrice?: number | null;
  markers?: ChartMarker[];
  className?: string;
}) {
  const { t } = useI18n();
  const [cursor, setCursor] = useState<number | null>(null);

  const model = useMemo(() => {
    if (!series || !series.available || series.points.length < 2) return null;
    const stamps = series.points.map((point) => new Date(point[0]).getTime());
    const closes = series.points.map((point) => point[4]);
    const lows = series.points.map((point) => point[3]);
    const highs = series.points.map((point) => point[2]);
    const candidates = [...lows, ...highs];
    if (entryPrice !== null && entryPrice !== undefined) candidates.push(entryPrice);
    for (const marker of markers) if (marker.price !== null) candidates.push(marker.price);
    const min = Math.min(...candidates);
    const max = Math.max(...candidates);
    const span = max - min || 1;
    const innerW = W - PAD.left - PAD.right;
    const innerH = H - PAD.top - PAD.bottom;
    const x = (index: number) => PAD.left + (innerW * index) / (closes.length - 1);
    const y = (price: number) => PAD.top + innerH * (1 - (price - min) / span);
    const path = closes
      .map((close, index) => `${index === 0 ? "M" : "L"}${x(index).toFixed(1)} ${y(close).toFixed(1)}`)
      .join(" ");
    const area = `${path} L${x(closes.length - 1).toFixed(1)} ${(PAD.top + innerH).toFixed(1)} L${PAD.left} ${(PAD.top + innerH).toFixed(1)} Z`;
    const tickCount = 5;
    const ticks = Array.from({ length: tickCount }, (_, tick) =>
      Math.round(((closes.length - 1) * tick) / (tickCount - 1)),
    );
    const priceTicks = [max, min + span * 0.5, min];
    return { stamps, closes, min, max, span, x, y, path, area, ticks, priceTicks, innerH };
  }, [series, entryPrice, markers]);

  if (!series) {
    return (
      <div className={cn("flex h-[220px] items-center justify-center text-table text-ink-3", className)}>
        {t("chart.loading")}
      </div>
    );
  }
  if (!model) {
    return (
      <div className={cn("flex h-[220px] items-center justify-center text-table text-ink-3", className)}>
        {t("chart.unavailable")} · {chartUnavailableLabel(series.unavailable_reason)}
      </div>
    );
  }

  const up = (series.last_close ?? 0) >= (series.first_close ?? 0);
  const stroke = up ? "text-pos" : "text-neg";
  const last = model.closes.length - 1;
  const active = cursor ?? last;
  const activePoint = series.points[active];

  const onMove = (event: MouseEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const fraction = (event.clientX - rect.left) / rect.width;
    const chartX = fraction * W;
    const innerW = W - PAD.left - PAD.right;
    const index = Math.round(((chartX - PAD.left) / innerW) * last);
    setCursor(Math.max(0, Math.min(last, index)));
  };

  const onKey = (event: KeyboardEvent<SVGSVGElement>) => {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      setCursor(Math.max(0, active - 1));
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      setCursor(Math.min(last, active + 1));
    } else if (event.key === "Home") {
      setCursor(0);
    } else if (event.key === "End") {
      setCursor(last);
    }
  };

  return (
    <div className={className}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="block h-auto w-full select-none focus-visible:outline-2 focus-visible:outline-accent"
        role="img"
        tabIndex={0}
        aria-label={t("chart.keyboardHint", { symbol: series.symbol, range: series.range })}
        onMouseMove={onMove}
        onMouseLeave={() => setCursor(null)}
        onKeyDown={onKey}
      >
        <title>{`${series.symbol} · ${series.range} · ${series.timeframe} bars`}</title>
        {/* price scale */}
        {model.priceTicks.map((price, index) => (
          <g key={index}>
            <line
              x1={PAD.left}
              x2={W - PAD.right}
              y1={model.y(price)}
              y2={model.y(price)}
              stroke="currentColor"
              strokeOpacity={0.12}
              className="text-ink-3"
            />
            <text
              x={W - PAD.right + 6}
              y={model.y(price) + 3.5}
              fontSize={10}
              fill="currentColor"
              className="num text-ink-3"
            >
              {money(price)}
            </text>
          </g>
        ))}
        {/* time scale */}
        {model.ticks.map((index) => (
          <text
            key={index}
            x={model.x(index)}
            y={H - 8}
            fontSize={10}
            textAnchor={index === 0 ? "start" : index === last ? "end" : "middle"}
            fill="currentColor"
            className="num text-ink-3"
          >
            {timeLabel(series.points[index]?.[0] ?? "", series.range)}
          </text>
        ))}
        {/* area + line */}
        <path d={model.area} fill="currentColor" fillOpacity={0.06} className={stroke} />
        <path
          d={model.path}
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          strokeLinejoin="round"
          className={stroke}
        />
        {/* average entry */}
        {entryPrice !== null && entryPrice !== undefined ? (
          <g>
            <line
              x1={PAD.left}
              x2={W - PAD.right}
              y1={model.y(entryPrice)}
              y2={model.y(entryPrice)}
              stroke="currentColor"
              strokeDasharray="4 4"
              strokeWidth={1}
              className="text-accent"
            />
            <text
              x={PAD.left + 4}
              y={model.y(entryPrice) - 4}
              fontSize={10}
              fill="currentColor"
              className="num text-accent"
            >
              {t("chart.avgEntry")} {money(entryPrice)}
            </text>
          </g>
        ) : null}
        {/* real fills */}
        {markers.map((marker, index) => {
          const at = new Date(marker.at).getTime();
          if (Number.isNaN(at)) return null;
          const pointIndex = nearestIndex(model.stamps, at);
          const price = marker.price ?? model.closes[pointIndex] ?? model.min;
          const cx = model.x(pointIndex);
          const cy = model.y(price);
          const buy = marker.side === "BUY";
          const points = buy
            ? `${cx},${cy - 7} ${cx - 5},${cy + 2} ${cx + 5},${cy + 2}`
            : `${cx},${cy + 7} ${cx - 5},${cy - 2} ${cx + 5},${cy - 2}`;
          return (
            <g key={`${marker.at}-${index}`} className={buy ? "text-pos" : "text-neg"}>
              <title>{marker.label}</title>
              <polygon points={points} fill="currentColor" stroke="var(--color-surface-1)" strokeWidth={1} />
            </g>
          );
        })}
        {/* cursor */}
        {activePoint ? (
          <g>
            <line
              x1={model.x(active)}
              x2={model.x(active)}
              y1={PAD.top}
              y2={PAD.top + model.innerH}
              stroke="currentColor"
              strokeOpacity={cursor === null ? 0 : 0.35}
              className="text-ink-2"
            />
            <circle
              cx={model.x(active)}
              cy={model.y(activePoint[4])}
              r={3}
              fill="currentColor"
              className={stroke}
            />
          </g>
        ) : null}
      </svg>
      <div className="mt-1 flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-meta text-ink-3">
        <span className="num">
          {activePoint ? (
            <>
              <span className="text-ink-2">{timeLabel(activePoint[0], series.range === "1D" ? "5D" : series.range)}</span>
              {" · "}O {money(activePoint[1])} H {money(activePoint[2])} L {money(activePoint[3])} C{" "}
              <span className="text-ink">{money(activePoint[4])}</span>
            </>
          ) : null}
        </span>
        <span className="num">
          {t("chart.bars", { timeframe: series.timeframe, count: series.points.length })}
          {series.from_cache ? ` · ${t("chart.cached")}` : ""}
        </span>
      </div>
      {markers.length ? (
        <p className="mt-1 text-meta leading-snug text-ink-3">{t("drawer.fillsLegend")}</p>
      ) : null}
    </div>
  );
}
