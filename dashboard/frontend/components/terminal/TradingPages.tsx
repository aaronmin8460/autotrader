"use client";

import { useMemo } from "react";

import { useDashboard } from "@/lib/dashboard";
import { money, percent, quantity, signedMoney, stampUtc } from "@/lib/format";
import { parseSummary } from "@/lib/live-contract";
import {
  contractMoney,
  contractPercent,
  contractSignedMoney,
  contractSignedPercent,
  contractTone,
} from "@/lib/live-decimal";
import {
  useTerminal,
  type TerminalOrder,
  type TerminalPosition,
} from "@/lib/terminal";

import {
  Badge,
  Empty,
  EquityChart,
  EventTape,
  PageHead,
  Panel,
  toneFor,
} from "./TerminalUI";

function pnlClass(value: string | null | undefined) {
  const tone = contractTone(value);
  return tone === "POSITIVE"
    ? "positive"
    : tone === "NEGATIVE"
      ? "negative"
      : "";
}

/** Presentation-only scale for bars; displayed dollar ceilings remain backend-authoritative. */
function visualPercent(value: string | null | undefined, maximum: string | null | undefined) {
  const amount = Number(value);
  const ceiling = Number(maximum);
  return Number.isFinite(amount) && Number.isFinite(ceiling) && ceiling > 0
    ? (amount / ceiling) * 100
    : 0;
}

function StatusFact({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "good" | "danger" | "warn" | "unknown";
}) {
  return (
    <div className="v5-status-fact">
      <span>{label}</span>
      <Badge tone={tone ?? toneFor(value)}>{value}</Badge>
    </div>
  );
}

function LivePositions({ rows }: { rows: TerminalPosition[] }) {
  if (!rows.length)
    return (
      <Empty
        title="NO LIVE POSITIONS"
        detail="The broker-authoritative Live account is flat. No Paper position is substituted."
      />
    );
  return (
    <div className="tv4-table-wrap">
      <table className="tv4-table v5-positions">
        <thead>
          <tr>
            <th>SYMBOL</th>
            <th>QTY</th>
            <th>AVG PRICE</th>
            <th>PRICE</th>
            <th>MARKET VALUE</th>
            <th>UNREALIZED P&amp;L</th>
            <th>DAY P&amp;L</th>
            <th>% EQUITY</th>
            <th>TARGET</th>
            <th>DELTA</th>
            <th>STANCE</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.symbol}>
              <td className="symbol">{row.symbol}</td>
              <td>{quantity(row.quantity)}</td>
              <td>{contractMoney(row.average_cost)}</td>
              <td>{contractMoney(row.price)}</td>
              <td>{contractMoney(row.market_value)}</td>
              <td className={pnlClass(row.unrealized_pnl)}>
                {contractSignedMoney(row.unrealized_pnl)}
              </td>
              <td className={pnlClass(row.day_pnl)}>
                {contractSignedMoney(row.day_pnl)}
              </td>
              <td>{contractPercent(row.actual_weight)}</td>
              <td>{contractPercent(row.target_weight)}</td>
              <td className={pnlClass(row.drift)}>
                {contractSignedPercent(row.drift)}
              </td>
              <td>
                <Badge tone={row.stance === "LONG" ? "good" : "unknown"}>
                  {row.stance ?? "—"}
                </Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LiveOrders({ rows }: { rows: TerminalOrder[] }) {
  if (!rows.length)
    return (
      <Empty
        title="NO LIVE ORDERS RECORDED"
        detail="The Live execution store contains no broker-linked orders."
      />
    );
  return (
    <div className="tv4-table-wrap">
      <table className="tv4-table">
        <thead>
          <tr>
            <th>TIME (ET)</th>
            <th>SYMBOL</th>
            <th>SIDE</th>
            <th>QTY</th>
            <th>AVG FILL</th>
            <th>STATUS</th>
            <th>CLIENT ORDER ID</th>
            <th>BROKER ORDER ID</th>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 6).map((row) => (
            <tr key={row.client_order_id}>
              <td>{stampUtc(row.submitted_at ?? row.intent_created_at)}</td>
              <td className="symbol">{row.symbol}</td>
              <td className={row.side.toLowerCase()}>{row.side}</td>
              <td>{quantity(row.filled_quantity ?? row.quantity)}</td>
              <td>{contractMoney(row.fill_price)}</td>
              <td>
                <Badge tone={toneFor(row.status)}>
                  {row.status.replaceAll("_", " ")}
                </Badge>
              </td>
              <td title={row.client_order_id}>
                {row.client_order_id.slice(0, 16)}
              </td>
              <td title={row.broker_order_id}>
                {row.broker_order_id.slice(0, 16)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RiskBar({
  label,
  value,
  detail,
  width,
  tone = "good",
}: {
  label: string;
  value: string;
  detail: string;
  width: number;
  tone?: "good" | "warn" | "danger";
}) {
  return (
    <div className="v5-risk-row">
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
      </div>
      <div className="v5-risk-track" aria-hidden>
        <i
          className={tone}
          style={{ width: `${Math.max(2, Math.min(100, width))}%` }}
        />
      </div>
      <small>{detail}</small>
    </div>
  );
}

function DayPnl({ rows }: { rows: TerminalPosition[] }) {
  const points = rows
    .map((row) => ({ symbol: row.symbol, value: Number(row.day_pnl ?? 0) }))
    .filter((row) => Number.isFinite(row.value));
  const max = Math.max(...points.map((row) => Math.abs(row.value)), 0.01);
  if (!points.length)
    return (
      <Empty
        title="DAY P&L UNAVAILABLE"
        detail="No authoritative per-symbol values were returned."
      />
    );
  return (
    <div
      className="v5-pnl-bars"
      role="img"
      aria-label="Authoritative day profit and loss by symbol"
    >
      {points.map((point) => (
        <div key={point.symbol}>
          <div className="v5-pnl-value">
            {point.value >= 0 ? "+" : ""}
            {point.value.toFixed(2)}
          </div>
          <div className="v5-pnl-column">
            <i
              className={point.value < 0 ? "negative" : "positive"}
              style={{
                height: `${Math.max(4, (Math.abs(point.value) / max) * 78)}%`,
              }}
            />
          </div>
          <span>{point.symbol}</span>
        </div>
      ))}
    </div>
  );
}

export function LiveTradingPage() {
  const data = useTerminal();
  const live = data.safety.data;
  const terminal = data.terminal.data;
  const history = data.history.data;
  const parsed = data.accounting.data
    ? parseSummary(data.accounting.data)
    : { summary: null, problems: [] };
  const summary = parsed.summary;
  const rows = useMemo(
    () => terminal?.positions.rows ?? [],
    [terminal?.positions.rows],
  );
  const auth = live?.arm.state ?? "UNKNOWN";
  const envelope = live?.risk.exposure_bound;

  return (
    <div className="tv4-stack v5-trading-page">
      <PageHead
        title="Live Trading"
        description="REAL CAPITAL TRADING"
        kicker="AUTOTRADER TERMINAL V5"
        meta={
          <>
            <Badge tone={auth === "ARMED" ? "danger" : toneFor(auth)}>
              AUTH {auth}
            </Badge>
            <span>STAGE C · {live?.risk.policy_id ?? "POLICY UNKNOWN"}</span>
          </>
        }
      />
      {parsed.problems.length ? (
        <div className="tv4-callout">
          <div>
            <strong>ACCOUNTING CONTRACT NOT TRUSTED</strong>
            <p>{parsed.problems[0]}</p>
          </div>
          <Badge tone="danger">FAIL CLOSED</Badge>
        </div>
      ) : null}
      <section className="v5-primary-strip" aria-label="Live account status">
        <div>
          <span>Equity</span>
          <strong>{contractMoney(summary?.current_broker_equity)}</strong>
          <small>Broker authoritative</small>
        </div>
        <div>
          <span>Today&apos;s P&amp;L</span>
          <strong className={pnlClass(history?.today_trading_pnl)}>
            {contractSignedMoney(history?.today_trading_pnl)}
          </strong>
          <small>
            {history
              ? `${history.sample_size} checkpoints`
              : "Awaiting history"}
          </small>
        </div>
        <div>
          <span>Gross Exposure</span>
          <strong>{contractMoney(live?.risk.current_gross_exposure)}</strong>
          <small>
            {live?.risk.binding ? `${live.risk.binding} binding` : "—"}
          </small>
        </div>
        <div>
          <span>Buying Power</span>
          <strong>{contractMoney(live?.account.buying_power)}</strong>
          <small>{live?.account.account_type ?? "Broker account"}</small>
        </div>
        <StatusFact
          label="Account Safety"
          value={live?.account_safety.state ?? "UNKNOWN"}
          tone={live?.account_safety.state === "SAFE" ? "good" : live?.account_safety.state ? "danger" : "unknown"}
        />
        <StatusFact
          label="Reconciliation"
          value={live?.reconciliation.status ?? "UNKNOWN"}
        />
        <StatusFact
          label="Accounting"
          value={
            summary?.accounting_status === "CLEAN" &&
            summary?.data_freshness === "FRESH"
              ? "CLEAN · FRESH"
              : `${summary?.accounting_status ?? "UNKNOWN"} · ${summary?.data_freshness ?? "UNKNOWN"}`
          }
        />
      </section>
      <div className="v5-main-grid">
        <div className="tv4-stack">
          <Panel
            title="Equity Curve"
            meta={<small>AUTHORITATIVE CHECKPOINTS · USD</small>}
          >
            <EquityChart
              points={history?.points ?? []}
              flows={history?.flows ?? []}
            />
          </Panel>
          <Panel
            title={`Positions (${rows.length})`}
            meta={<small>BROKER POSITION + RECORDED TARGETS</small>}
            body={false}
          >
            <LivePositions rows={rows} />
          </Panel>
        </div>
        <aside className="tv4-stack">
          <Panel title="Account Summary" body={false}>
            <dl className="v5-summary-list">
              <div>
                <dt>Equity</dt>
                <dd>{contractMoney(summary?.current_broker_equity)}</dd>
              </div>
              <div>
                <dt>Cash</dt>
                <dd>{contractMoney(summary?.current_cash)}</dd>
              </div>
              <div>
                <dt>Buying Power</dt>
                <dd>{contractMoney(live?.account.buying_power)}</dd>
              </div>
              <div>
                <dt>Gross Exposure</dt>
                <dd>{contractMoney(live?.risk.current_gross_exposure)}</dd>
              </div>
              <div>
                <dt>Unrealized P&amp;L</dt>
                <dd className={pnlClass(summary?.unrealized_pnl)}>
                  {contractSignedMoney(summary?.unrealized_pnl)}
                </dd>
              </div>
              <div>
                <dt>Trading P&amp;L</dt>
                <dd className={pnlClass(summary?.trading_pnl_since_inception)}>
                  {contractSignedMoney(summary?.trading_pnl_since_inception)}
                </dd>
              </div>
              <div>
                <dt>TWR</dt>
                <dd>{contractSignedPercent(summary?.time_weighted_return)}</dd>
              </div>
            </dl>
          </Panel>
          <Panel
            title="Risk & Exposure"
            meta={
              <Badge tone={toneFor(live?.risk.status)}>
                {live?.risk.status ?? "UNKNOWN"}
              </Badge>
            }
          >
            <div className="v5-risk-list">
              <RiskBar
                label="Current Gross"
                value={contractMoney(live?.risk.current_gross_exposure)}
                detail={`Target ${contractMoney(live?.risk.target_gross)}`}
                width={visualPercent(live?.risk.current_gross_exposure, envelope)}
              />
              <RiskBar
                label="Target Gross Ceiling"
                value={contractMoney(live?.risk.target_gross)}
                detail="Backend-resolved"
                width={visualPercent(live?.risk.target_gross, envelope)}
              />
              <RiskBar
                label="Hard Gross Ceiling"
                value={contractMoney(live?.risk.hard_gross)}
                detail="Backend-resolved"
                width={visualPercent(live?.risk.hard_gross, envelope)}
                tone="warn"
              />
              <RiskBar
                label="Absolute Exposure"
                value={contractMoney(live?.risk.exposure_bound)}
                detail="Backend-resolved"
                width={visualPercent(envelope, envelope)}
                tone="danger"
              />
              <RiskBar
                label="Per-Symbol Ceiling"
                value={contractMoney(live?.risk.per_symbol)}
                detail={`${live?.risk.universe_size ?? "—"} symbol universe`}
                width={visualPercent(live?.risk.per_symbol, envelope)}
              />
            </div>
          </Panel>
          <Panel title="Live System & Safety">
            <div className="v5-safety-grid">
              <StatusFact
                label="Ready"
                value={
                  live?.live_ready
                    ? "YES"
                    : live?.live_ready === false
                      ? "NO"
                      : "UNKNOWN"
                }
              />
              <StatusFact
                label="Authorization"
                value={auth}
                tone={auth === "ARMED" ? "danger" : "unknown"}
              />
              <StatusFact
                label="Open Orders"
                value={String(live?.account.open_order_count ?? "UNKNOWN")}
              />
              <StatusFact
                label="Duplicates"
                value={String(
                  terminal?.execution.duplicate_client_order_ids ?? "UNKNOWN",
                )}
              />
              <StatusFact
                label="Unknown"
                value={String(terminal?.execution.unknown_count ?? "UNKNOWN")}
              />
              <StatusFact
                label="Account Pin"
                value={live?.identity.status ?? "UNKNOWN"}
              />
              <StatusFact
                label="Safety Source"
                value={live?.account_safety.source ?? "UNKNOWN"}
                tone={live?.account_safety.state === "SAFE" ? "good" : "unknown"}
              />
            </div>
          </Panel>
        </aside>
      </div>
      <div className="v5-bottom-grid">
        <Panel
          title="Recent Orders"
          meta={<small>LIVE ONLY · BROKER LINKED</small>}
          body={false}
        >
          <LiveOrders rows={terminal?.orders ?? []} />
        </Panel>
        <Panel
          title="Day P&L by Symbol"
          meta={<small>POSITION READ MODEL</small>}
        >
          <DayPnl rows={rows} />
        </Panel>
      </div>
      <Panel
        title="Operational Events"
        meta={<small>RECORDED SYSTEM EVENTS</small>}
        body={false}
      >
        <EventTape events={(terminal?.events ?? []).slice(0, 8)} />
      </Panel>
    </div>
  );
}

export function PaperTradingPage() {
  const { account, paper } = useDashboard();
  const snapshot = paper.data;
  const overview = account.data;
  const positions =
    overview?.positions?.rows.filter((row) => row.asset_class === "EQUITY") ??
    [];
  const metrics = overview?.metrics;
  const running =
    snapshot?.service.running === true && snapshot?.service.stale !== true;
  return (
    <div className="tv4-stack v5-trading-page paper">
      <PageHead
        title="Paper Trading"
        description="SIMULATED TRADING · NO REAL CAPITAL"
        kicker="AUTOTRADER TERMINAL V5"
        scope="paper"
        meta={
          <>
            <Badge tone={running ? "good" : snapshot ? "warn" : "unknown"}>
              {running
                ? "SIMULATION ACTIVE"
                : snapshot
                  ? "SIMULATION INACTIVE"
                  : "UNKNOWN"}
            </Badge>
            <span>
              STAGE {snapshot?.service.stage ?? "—"} ·{" "}
              {snapshot?.service.sizing_policy ?? "POLICY UNKNOWN"}
            </span>
          </>
        }
      />
      <div className="v5-paper-boundary">
        <strong>PAPER ACCOUNT</strong>
        <span>
          Balances, positions, orders, performance, and status below are
          simulation records only. Live authorization is intentionally absent.
        </span>
      </div>
      <section
        className="v5-primary-strip paper-strip"
        aria-label="Paper account status"
      >
        <div>
          <span>Simulated Equity</span>
          <strong>{money(metrics?.equity.value)}</strong>
          <small>Paper account read model</small>
        </div>
        <div>
          <span>Today&apos;s P&amp;L</span>
          <strong
            className={
              metrics?.daily_pnl.value && metrics.daily_pnl.value < 0
                ? "negative"
                : "positive"
            }
          >
            {signedMoney(metrics?.daily_pnl.value)}
          </strong>
          <small>{percent(metrics?.daily_pnl_fraction)}</small>
        </div>
        <div>
          <span>Exposure</span>
          <strong>{money(metrics?.exposure.value)}</strong>
          <small>{percent(metrics?.exposure_fraction)}</small>
        </div>
        <div>
          <span>Cash</span>
          <strong>{money(metrics?.cash.value)}</strong>
          <small>Simulated balance</small>
        </div>
        <StatusFact
          label="Simulation"
          value={running ? "ACTIVE" : snapshot ? "INACTIVE" : "UNKNOWN"}
        />
        <StatusFact
          label="Reconciliation"
          value={snapshot?.safety.reconciliation_status ?? "UNKNOWN"}
        />
        <StatusFact
          label="Account Safety"
          value={snapshot?.safety.account_safety ?? "UNKNOWN"}
        />
      </section>
      <div className="v5-main-grid">
        <div className="tv4-stack">
          <Panel
            title={`Paper Positions (${positions.length})`}
            meta={<small>SIMULATED ACCOUNT ONLY</small>}
            body={false}
          >
            {positions.length ? (
              <div className="tv4-table-wrap">
                <table className="tv4-table">
                  <thead>
                    <tr>
                      <th>SYMBOL</th>
                      <th>QTY</th>
                      <th>AVG PRICE</th>
                      <th>PRICE</th>
                      <th>MARKET VALUE</th>
                      <th>UNREALIZED P&amp;L</th>
                      <th>UPDATED</th>
                    </tr>
                  </thead>
                  <tbody>
                    {positions.map((row) => (
                      <tr key={row.symbol}>
                        <td className="symbol">{row.symbol}</td>
                        <td>{quantity(row.quantity)}</td>
                        <td>{money(row.average_entry_price)}</td>
                        <td>{money(row.price)}</td>
                        <td>{money(row.market_value)}</td>
                        <td
                          className={
                            row.unrealized_pnl && row.unrealized_pnl < 0
                              ? "negative"
                              : "positive"
                          }
                        >
                          {signedMoney(row.unrealized_pnl)}
                        </td>
                        <td>{stampUtc(row.updated_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty
                title="NO PAPER POSITIONS"
                detail="The simulated equity account is flat or unavailable."
              />
            )}
          </Panel>
          <Panel
            title="Paper Orders"
            meta={<small>SIMULATED EXECUTION</small>}
            body={false}
          >
            {snapshot?.orders.length ? (
              <div className="tv4-table-wrap">
                <table className="tv4-table">
                  <thead>
                    <tr>
                      <th>TIME</th>
                      <th>SYMBOL</th>
                      <th>SIDE</th>
                      <th>QTY</th>
                      <th>AVG FILL</th>
                      <th>STATUS</th>
                      <th>CLIENT ORDER ID</th>
                    </tr>
                  </thead>
                  <tbody>
                    {snapshot.orders.map((row) => (
                      <tr key={row.client_order_id}>
                        <td>{stampUtc(row.created_at)}</td>
                        <td className="symbol">{row.symbol}</td>
                        <td className={row.side.toLowerCase()}>{row.side}</td>
                        <td>
                          {quantity(
                            row.filled_quantity ?? row.approved_quantity,
                          )}
                        </td>
                        <td>{money(row.filled_average_price)}</td>
                        <td>
                          <Badge
                            tone={toneFor(row.broker_status ?? row.status)}
                          >
                            {row.broker_status ?? row.status}
                          </Badge>
                        </td>
                        <td>{row.client_order_id.slice(0, 18)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty
                title="NO PAPER ORDERS"
                detail="No simulated equity orders are recorded."
              />
            )}
          </Panel>
        </div>
        <aside className="tv4-stack">
          <Panel title="Paper Account Summary" body={false}>
            <dl className="v5-summary-list">
              <div>
                <dt>Equity</dt>
                <dd>{money(metrics?.equity.value)}</dd>
              </div>
              <div>
                <dt>Cash</dt>
                <dd>{money(metrics?.cash.value)}</dd>
              </div>
              <div>
                <dt>Exposure</dt>
                <dd>{money(metrics?.exposure.value)}</dd>
              </div>
              <div>
                <dt>Daily P&amp;L</dt>
                <dd>{signedMoney(metrics?.daily_pnl.value)}</dd>
              </div>
              <div>
                <dt>Execution universe</dt>
                <dd>{snapshot?.service.execution_universe.length ?? "—"}</dd>
              </div>
              <div>
                <dt>Open intents</dt>
                <dd>{snapshot?.service.unresolved_intents ?? "—"}</dd>
              </div>
            </dl>
          </Panel>
          <Panel title="Paper Strategy">
            <dl className="v5-summary-list">
              <div>
                <dt>Mode</dt>
                <dd>{snapshot?.service.mode ?? "—"}</dd>
              </div>
              <div>
                <dt>Regime</dt>
                <dd>
                  {snapshot?.regime.participate === null ||
                  snapshot?.regime.participate === undefined
                    ? "—"
                    : snapshot.regime.participate
                      ? "PARTICIPATE"
                      : "DEFENSIVE"}
                </dd>
              </div>
              <div>
                <dt>Policy</dt>
                <dd>{snapshot?.service.sizing_policy ?? "—"}</dd>
              </div>
              <div>
                <dt>Last cycle</dt>
                <dd>{stampUtc(snapshot?.service.last_cycle_at)}</dd>
              </div>
              <div>
                <dt>Target gross</dt>
                <dd>
                  {snapshot?.policy
                    ? percent(snapshot.policy.target_gross)
                    : "—"}
                </dd>
              </div>
              <div>
                <dt>Per symbol</dt>
                <dd>
                  {snapshot?.policy
                    ? percent(snapshot.policy.hard_symbol_cap)
                    : "—"}
                </dd>
              </div>
            </dl>
          </Panel>
          <Panel title="Paper Safety">
            <div className="v5-safety-grid">
              <StatusFact
                label="Runtime"
                value={running ? "ACTIVE" : "INACTIVE"}
              />
              <StatusFact
                label="Safety"
                value={snapshot?.safety.account_safety ?? "UNKNOWN"}
              />
              <StatusFact
                label="Reconciliation"
                value={snapshot?.safety.reconciliation_status ?? "UNKNOWN"}
              />
              <StatusFact
                label="Parity mismatches"
                value={String(snapshot?.safety.parity_mismatches ?? "UNKNOWN")}
              />
            </div>
          </Panel>
        </aside>
      </div>
    </div>
  );
}
