"use client";

import {
  useEffect,
  useMemo,
  useState,
  type MouseEvent,
  type ReactNode,
} from "react";

import { clockUtc, stampUtc } from "@/lib/format";
import { contractMoney } from "@/lib/live-decimal";
import type { CapitalFlow, HistoryPoint, TerminalEvent } from "@/lib/terminal";

export type TerminalTone = "good" | "danger" | "warn" | "purple" | "unknown";

export function toneFor(value: string | null | undefined): TerminalTone {
  const state = (value ?? "UNKNOWN").toUpperCase();
  if (state.includes("PARTIAL")) return "warn";
  if (
    [
      "PASS",
      "CLEAN",
      "FRESH",
      "READY",
      "RUNNING",
      "PINNED",
      "RESOLVED",
      "OK",
    "FILLED",
    "SAFE",
    "ACTIVE",
      "PARTICIPATE",
    ].includes(state)
  )
    return "good";
  if (
    [
      "BLOCKED",
      "FAILED",
      "MISMATCH",
      "ACCOUNT_MISMATCH",
      "BROKER_UNAVAILABLE",
      "REJECTED",
      "ARMED",
    ].includes(state)
  )
    return "danger";
  if (
    [
      "WARN",
      "STALE",
      "DEGRADED",
      "UNKNOWN_EXTERNAL_FLOW",
      "PARTIAL",
      "DEFENSIVE",
    ].includes(state)
  )
    return "warn";
  if (["OBSERVING", "SHADOW", "RESEARCH"].includes(state)) return "purple";
  return "unknown";
}

export function Badge({
  children,
  tone,
  title,
}: {
  children: ReactNode;
  tone?: TerminalTone;
  title?: string;
}) {
  return (
    <span className={`tv4-badge ${tone ?? "unknown"}`} title={title}>
      <span className="tv4-dot" />
      {children}
    </span>
  );
}

export function PageHead({
  title,
  description,
  kicker = "AUTOTRADER TERMINAL V6",
  meta,
  scope = "live",
}: {
  title: string;
  description: string;
  kicker?: string;
  meta?: ReactNode;
  scope?: "live" | "paper" | "research" | "mixed";
}) {
  const scopeLabel =
    scope === "research"
      ? "RESEARCH · OBSERVATION"
      : scope === "paper"
        ? "PAPER · SIMULATED"
        : scope === "mixed"
          ? "PAPER + LIVE · SEPARATE"
          : "LIVE · REAL MONEY";
  return (
    <header className="tv4-page-head">
      <div>
        <p className="tv4-kicker">{kicker}</p>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      <div className="tv4-page-meta">
        {meta}
        <span className={`tv4-tag ${scope === "live" ? "live" : ""}`}>
          {scopeLabel}
        </span>
      </div>
    </header>
  );
}

export function Panel({
  title,
  meta,
  children,
  className = "",
  body = true,
}: {
  title: ReactNode;
  meta?: ReactNode;
  children: ReactNode;
  className?: string;
  body?: boolean;
}) {
  return (
    <section className={`tv4-panel ${className}`}>
      <header className="tv4-panel-head">
        <h2>{title}</h2>
        {meta ? <div>{meta}</div> : null}
      </header>
      {body ? <div className="tv4-panel-body">{children}</div> : children}
    </section>
  );
}

export interface MetricSpec {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  tone?: "positive" | "negative" | "warning";
}

export function MetricStrip({ metrics }: { metrics: MetricSpec[] }) {
  return (
    <section className="tv4-metric-strip" aria-label="Key metrics">
      {metrics.map((metric) => (
        <div className="tv4-metric" key={metric.label}>
          <span className="tv4-metric-label">{metric.label}</span>
          <strong className={`tv4-metric-value ${metric.tone ?? ""}`}>
            {metric.value}
          </strong>
          {metric.detail ? (
            <span className="tv4-metric-detail">{metric.detail}</span>
          ) : null}
        </div>
      ))}
    </section>
  );
}

export function Field({
  label,
  value,
  className = "",
  large = false,
}: {
  label: string;
  value: ReactNode;
  className?: string;
  large?: boolean;
}) {
  return (
    <dl className={`tv4-field ${className}`}>
      <dt>{label}</dt>
      <dd className={large ? "large" : ""}>{value}</dd>
    </dl>
  );
}

export function FieldGrid({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return <div className={`tv4-field-grid ${className}`}>{children}</div>;
}

export function Empty({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="tv4-empty">
      <div>
        <strong>{title}</strong>
        <p>{detail}</p>
      </div>
    </div>
  );
}

export function LiveHero({
  ready,
  armed,
  policy,
  detail,
  children,
}: {
  ready: boolean | null | undefined;
  armed: string | null | undefined;
  policy: string | null | undefined;
  detail?: string | null;
  children: ReactNode;
}) {
  const readyText =
    ready === true ? "READY" : ready === false ? "NOT READY" : "READY UNKNOWN";
  const auth = armed ?? "UNKNOWN";
  return (
    <section className="tv4-live-hero">
      <div className="tv4-live-hero-top">
        <Badge tone={auth === "ARMED" ? "danger" : toneFor(auth)}>
          AUTH {auth}
        </Badge>
        <h2 className={ready === true ? "ready" : "armed"}>{readyText}</h2>
        <span className="tv4-tag live">{policy ?? "POLICY UNKNOWN"}</span>
      </div>
      <p>
        {detail ||
          "Authoritative Live state. Read-only terminal; no broker controls are exposed."}
      </p>
      <div className="tv4-live-hero-grid">{children}</div>
    </section>
  );
}

export function EventTape({
  events,
  filterable = false,
}: {
  events: TerminalEvent[];
  filterable?: boolean;
}) {
  const categories = useMemo(
    () => [
      "ALL",
      ...Array.from(
        new Set(events.map((event) => event.category.toUpperCase())),
      ),
    ],
    [events],
  );
  const [filter, setFilter] = useState("ALL");
  const shown =
    filter === "ALL"
      ? events
      : events.filter((event) => event.category.toUpperCase() === filter);
  return (
    <div>
      {filterable && categories.length > 1 ? (
        <div className="tv4-filter-row tv4-panel-body">
          {categories.map((category) => (
            <button
              type="button"
              className={filter === category ? "active" : ""}
              key={category}
              onClick={() => setFilter(category)}
            >
              {category}
            </button>
          ))}
        </div>
      ) : null}
      {shown.length ? (
        <div className="tv4-tape">
          {shown.map((event, index) => (
            <div
              className={`tv4-tape-row ${event.category.toLowerCase()}`}
              key={`${event.timestamp}-${event.type}-${index}`}
            >
              <time dateTime={event.timestamp}>
                {clockUtc(event.timestamp)}
              </time>
              <span className="category">{event.category}</span>
              <span className="event">
                <strong>
                  {event.symbol ? `${event.symbol} · ` : ""}
                  {event.type}
                </strong>
                {event.detail ? ` · ${event.detail}` : ""}
              </span>
              <Badge tone={toneFor(event.status)}>
                {event.status ?? "EVENT"}
              </Badge>
            </div>
          ))}
        </div>
      ) : (
        <Empty
          title="NO RECORDED EVENTS"
          detail="The authoritative event store has no events for this view."
        />
      )}
    </div>
  );
}

type ChartRange = "1D" | "1W" | "1M" | "3M" | "1Y" | "ALL";

const RANGE_DAYS: Readonly<Record<Exclude<ChartRange, "ALL">, number>> = {
  "1D": 1,
  "1W": 7,
  "1M": 30,
  "3M": 90,
  "1Y": 365,
};

function dateCutoff(points: HistoryPoint[], range: ChartRange): number | null {
  if (range === "ALL" || points.length === 0) return null;
  const latest = Date.parse(points[points.length - 1]?.taken_at ?? "");
  if (!Number.isFinite(latest)) return null;
  const days = RANGE_DAYS[range];
  return latest - days * 86_400_000;
}

function toFinite(text: string | null): number | null {
  if (text === null || text.trim() === "") return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

function pathFor(
  values: Array<number | null>,
  min: number,
  span: number,
): string {
  const usable = Math.max(values.length - 1, 1);
  return values
    .map((value, index) =>
      value === null
        ? null
        : `${index === 0 ? "M" : "L"}${(index / usable) * 100},${92 - ((value - min) / span) * 80}`,
    )
    .filter(Boolean)
    .join(" ");
}

function chartDate(iso: string | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "UTC",
  }).format(date);
}

export function EquityChart({
  points,
  flows = [],
  mode = "equity",
}: {
  points: HistoryPoint[];
  flows?: CapitalFlow[];
  mode?: "equity" | "drawdown";
}) {
  const [range, setRange] = useState<ChartRange>("ALL");
  const [cursor, setCursor] = useState<number | null>(null);
  const historyDays =
    points.length > 1
      ? (Date.parse(points.at(-1)?.taken_at ?? "") -
          Date.parse(points[0]?.taken_at ?? "")) /
        86_400_000
      : 0;
  const shown = useMemo(() => {
    const cutoff = dateCutoff(points, range);
    return cutoff === null
      ? points
      : points.filter((point) => Date.parse(point.taken_at) >= cutoff);
  }, [points, range]);
  const broker = shown.map((point) => toFinite(point.broker_equity));
  const adjusted = shown.map((point) =>
    toFinite(mode === "drawdown" ? point.drawdown : point.flow_adjusted_equity),
  );
  const all = (
    mode === "drawdown" ? adjusted : [...broker, ...adjusted]
  ).filter((value): value is number => value !== null);
  if (shown.length === 0 || all.length === 0)
    return (
      <Empty
        title="HISTORY UNAVAILABLE"
        detail="No authoritative accounting checkpoints exist for this range. No series has been inferred."
      />
    );
  const low = Math.min(...all);
  const high = Math.max(...all);
  const span = high === low ? Math.max(Math.abs(high) * 0.02, 1) : high - low;
  const brokerPath = pathFor(broker, low, span);
  const adjustedPath = pathFor(adjusted, low, span);
  const first = shown[0];
  const last = shown[shown.length - 1];
  const lastIndex = shown.length - 1;
  const activeIndex = cursor ?? lastIndex;
  const activePoint = shown[activeIndex];
  const adjustedCurrent = adjusted.at(-1) ?? null;
  const ticks = Array.from({ length: 5 }, (_, index) => high - (span * index) / 4);
  const onMove = (event: MouseEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const fraction = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    setCursor(Math.round(fraction * lastIndex));
  };
  return (
    <div className="tv4-chart v6-equity-chart">
      <div className="v6-chart-toolbar">
        <div className="tv4-range" aria-label="Chart range">
          {(["1D", "1W", "1M", "3M", "1Y", "ALL"] as const).map((item) => {
            const disabled = item !== "ALL" && historyDays < RANGE_DAYS[item];
            return (
              <button
                key={item}
                type="button"
                onClick={() => setRange(item)}
                className={range === item ? "active" : ""}
                disabled={disabled}
                title={disabled ? `${item} authoritative history is unavailable` : `Show ${item} history`}
              >
                {item}
              </button>
            );
          })}
        </div>
        <span className="v6-range-context">
          RANGE {contractMoney(String(low))}–{contractMoney(String(high))} · SPAN {contractMoney(String(span))}
        </span>
      </div>
      {mode === "drawdown" ? (
        <div className="v6-chart-series-summary single" aria-label="Drawdown legend and values">
          <div className="drawdown"><span>Drawdown from Adjusted HWM</span><strong>{adjustedCurrent === null ? "—" : `${(adjustedCurrent * 100).toFixed(2)}%`}</strong><small>Authoritative accounting series</small></div>
        </div>
      ) : (
        <div className="v6-chart-series-summary" aria-label="Chart legend and values">
          <div className="broker"><span>Broker Equity</span><strong>{contractMoney(last?.broker_equity)}</strong><small>{range} start {contractMoney(first?.broker_equity)} → current</small></div>
          <div className="adjusted"><span>Flow-Adjusted Equity</span><strong>{contractMoney(last?.flow_adjusted_equity)}</strong><small>{range} start {contractMoney(first?.flow_adjusted_equity)} → current</small></div>
          <div className="flow"><span>External Cash Flow</span><strong>{flows.length ? `${flows.length} event${flows.length === 1 ? "" : "s"}` : "No events"}</strong><small>Authoritative event markers</small></div>
        </div>
      )}
      <div className="v6-chart-stage">
        <div className="tv4-chart-scale">
          {ticks.map((value) => <span key={value}>{mode === "drawdown" ? `${(value * 100).toFixed(2)}%` : contractMoney(String(value))}</span>)}
        </div>
        <svg
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
          role="img"
          aria-label={mode === "drawdown" ? "Authoritative drawdown history" : "Authoritative broker equity, flow-adjusted equity, and external cash-flow events"}
          onMouseMove={onMove}
          onMouseLeave={() => setCursor(null)}
        >
          {[12, 32, 52, 72, 92].map((y) => <line key={y} className="tv4-chart-grid" x1="0" y1={y} x2="100" y2={y} />)}
          {mode === "equity" && brokerPath ? <path className="tv4-series-broker" d={brokerPath} /> : null}
          {adjustedPath ? <path className={mode === "drawdown" ? "tv4-series-drawdown" : "tv4-series-adjusted"} d={adjustedPath} /> : null}
          {mode === "equity" ? flows.filter((flow) => shown.some((point) => point.taken_at >= flow.settle_at)).map((flow, index) => {
            const flowAt = Date.parse(flow.settle_at);
            const firstAt = Date.parse(first?.taken_at ?? "");
            const lastAt = Date.parse(last?.taken_at ?? "");
            const x = lastAt === firstAt ? 50 : Math.max(0, Math.min(100, ((flowAt - firstAt) / (lastAt - firstAt)) * 100));
            return <g key={`${flow.settle_at}-${index}`}><title>{`${flow.classification} · ${contractMoney(flow.amount)} · ${flow.confirmation}`}</title><line x1={x} x2={x} y1="11" y2="92" stroke="var(--tv4-amber)" strokeWidth=".8" strokeDasharray="2 2" vectorEffect="non-scaling-stroke" /><circle cx={x} cy="11" r="1.4" fill="var(--tv4-amber)" /></g>;
          }) : null}
          {activePoint ? <line className="v6-chart-cursor" x1={(activeIndex / Math.max(lastIndex, 1)) * 100} x2={(activeIndex / Math.max(lastIndex, 1)) * 100} y1="12" y2="92" /> : null}
        </svg>
        {activePoint && mode === "equity" ? (
          <div className="v6-chart-tooltip" aria-live="polite">
            <time>{chartDate(activePoint.taken_at)} UTC</time>
            <span><i className="broker" />Broker {contractMoney(activePoint.broker_equity)}</span>
            <span><i className="adjusted" />Flow-adjusted {contractMoney(activePoint.flow_adjusted_equity)}</span>
          </div>
        ) : null}
      </div>
      <p className="tv4-note">
        {stampUtc(first?.taken_at)} — {stampUtc(last?.taken_at)} UTC ·{" "}
        {shown.length} authoritative checkpoint{shown.length === 1 ? "" : "s"}
      </p>
    </div>
  );
}

export function CashHistoryChart({ points }: { points: HistoryPoint[] }) {
  const values = points.map((point) => toFinite(point.broker_cash));
  const finite = values.filter((value): value is number => value !== null);
  if (points.length < 2 || finite.length < 2) {
    return <Empty title="CASH HISTORY UNAVAILABLE" detail="The authoritative accounting history does not contain enough cash checkpoints. Exposure and buying-power history are not exposed." />;
  }
  const low = Math.min(...finite);
  const high = Math.max(...finite);
  const span = high === low ? Math.max(Math.abs(high) * 0.02, 1) : high - low;
  const path = pathFor(values, low, span);
  const firstPoint = points.find((point) => point.broker_cash !== null);
  const lastPoint = [...points].reverse().find((point) => point.broker_cash !== null);
  return (
    <div className="v6-liquidity-chart">
      <div className="v6-chart-series-summary single" aria-label="Cash history legend and values">
        <div className="cash"><span>Cash</span><strong>{contractMoney(lastPoint?.broker_cash)}</strong><small>Start {contractMoney(firstPoint?.broker_cash)} → current</small></div>
        <div className="unavailable"><span>Gross Exposure</span><strong>Unavailable</strong><small>No authoritative history series</small></div>
        <div className="unavailable"><span>Buying Power</span><strong>Unavailable</strong><small>No authoritative history series</small></div>
      </div>
      <div className="v6-chart-stage compact"><div className="tv4-chart-scale"><span>{contractMoney(String(high))}</span><span>{contractMoney(String(low))}</span></div><svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="Authoritative broker cash history; gross exposure and buying power history unavailable"><line className="tv4-chart-grid" x1="0" y1="12" x2="100" y2="12" /><line className="tv4-chart-grid" x1="0" y1="52" x2="100" y2="52" /><line className="tv4-chart-grid" x1="0" y1="92" x2="100" y2="92" /><path className="v6-series-cash" d={path} /></svg></div>
      <p className="tv4-note">{points.length} authoritative cash checkpoints · No exposure or buying-power series inferred</p>
    </div>
  );
}

export function SafetyMatrix({
  cells,
}: {
  cells: Array<{
    label: string;
    value: string;
    detail?: string | null;
    tone?: TerminalTone;
  }>;
}) {
  return (
    <div className="tv4-safety-matrix">
      {cells.map((cell) => (
        <div
          className="tv4-safety-cell"
          key={cell.label}
          title={cell.detail ?? undefined}
        >
          <span>{cell.label}</span>
          <Badge tone={cell.tone ?? toneFor(cell.value)}>{cell.value}</Badge>
          {cell.detail ? <p className="tv4-note">{cell.detail}</p> : null}
        </div>
      ))}
    </div>
  );
}

export function DetailDrawer({
  title,
  eyebrow,
  close,
  children,
}: {
  title: string;
  eyebrow: string;
  close: () => void;
  children: ReactNode;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);
  return (
    <>
      <div className="tv4-drawer-overlay" onClick={close} aria-hidden />
      <aside
        className="tv4-detail-drawer"
        role="dialog"
        aria-modal="true"
        aria-label={`${title} detail`}
      >
        <header className="tv4-drawer-head">
          <div>
            <p className="tv4-kicker">{eyebrow}</p>
            <h2>{title}</h2>
          </div>
          <button
            type="button"
            className="tv4-close"
            onClick={close}
            aria-label="Close detail"
          >
            ×
          </button>
        </header>
        {children}
      </aside>
    </>
  );
}
