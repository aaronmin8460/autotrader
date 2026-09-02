/**
 * Live service state, and the composition that keeps five services apart.
 *
 * The operations page reads its health panel from the operational API, which
 * derives every runtime row from what a runtime wrote into *that* store. For
 * crypto that is right. For equity it produced the defect this module fixes:
 * the only equity service that ever wrote there is `autotrader-equity.service`,
 * which is masked on the deployed host, so the row read "Equity runtime —
 * STOPPED" while `autotrader-equity-paper.service` was active and submitting
 * paper orders from a store that API never opens.
 *
 * The fix is not a rename. A row cannot be relabelled into correctness while it
 * is still computed from the wrong service, so the runtime rows are *replaced*
 * with rows read from the service manager itself, one query per named unit.
 *
 * The endpoint lives behind `/api/equity-paper/` because that is the process
 * that serves it - the least privileged of the readers - and it is GET-only,
 * no-store, and behind the same authentication as everything else.
 *
 * **Observers are not traders, and the status word must not say they are.**
 * The service manager reports `active` for a process that can place an order
 * and for one that structurally cannot. Each unit therefore carries a `kind`,
 * and a running observer renders as OBSERVING in the observation colour.
 *
 * Deliberately free of React, so the composition below can be run directly by
 * the test suite. The poll that feeds it lives in `lib/api`.
 */

import type { HealthComponent, Tone } from "./types";

/** The one route this module describes. GET, and there is no other. */
export const SERVICES_ENDPOINT = "/api/equity-paper/services";

/** The key prefix the operational API uses for its trail-derived runtime rows. */
export const TRAIL_ROW_PREFIX = "runtime_";

export type ServiceStatus =
  | "RUNNING"
  | "STOPPED"
  | "FAILED"
  | "MASKED"
  | "STARTING"
  | "STOPPING"
  | "NOT INSTALLED"
  | "UNKNOWN";

export type ServiceKind = "TRADING" | "OBSERVER" | "LEGACY";

export interface ServiceUnitRow {
  key: string;
  label: string;
  unit: string;
  status: ServiceStatus;
  tone: Tone;
  /** A standing qualifier: what this service is allowed to do, always shown. */
  note: string;
  detail: string;
  /** In the state this host is configured for. */
  expected: boolean;
  /** Not an error condition. Masked-on-purpose is healthy; stopped is not. */
  healthy: boolean;
  /** Optional on the wire for older API builds; the registry fills it in. */
  kind?: ServiceKind;
  load_state: string | null;
  active_state: string | null;
  sub_state: string | null;
  unit_file_state: string | null;
}

export interface ServiceUnitsPanel {
  available: boolean;
  generated_at: string;
  units: ServiceUnitRow[];
  source: string;
  unavailable_reason: string | null;
}

/** A health row that may carry a standing qualifier under its label. */
export interface HealthRow extends HealthComponent {
  note?: string | null;
  unit?: string | null;
  kind?: ServiceKind;
}

/**
 * The units, their labels, and their unit names - mirroring the backend registry.
 *
 * Duplicated deliberately. When the service endpoint cannot be reached the page
 * must still name the five services and say it does not know their state; the
 * alternative is falling back to the trail-derived rows, which is exactly the
 * misreading this module exists to end. An unreachable status source is
 * reported as unknown, never as stopped and never as running.
 */
export const SERVICE_UNITS: ReadonlyArray<{
  key: string;
  label: string;
  unit: string;
  note: string;
  kind: ServiceKind;
}> = [
  {
    key: "crypto",
    label: "Crypto Paper",
    unit: "autotrader-crypto.service",
    note: "PAPER · NO REAL MONEY",
    kind: "TRADING",
  },
  {
    key: "equity_paper",
    label: "Equity Paper · EDA-1",
    unit: "autotrader-equity-paper.service",
    note: "PAPER · NO REAL MONEY",
    kind: "TRADING",
  },
  {
    key: "equity_shadow",
    label: "Equity Shadow",
    unit: "autotrader-equity-shadow.service",
    note: "OBSERVATION ONLY · ZERO ORDERS",
    kind: "OBSERVER",
  },
  {
    key: "equity_a1b_shadow",
    label: "A1-B U30 Shadow",
    unit: "autotrader-equity-a1b-shadow.service",
    note: "OBSERVATION ONLY · ZERO ORDERS",
    kind: "OBSERVER",
  },
  {
    key: "equity_legacy",
    label: "Legacy Equity Runtime",
    unit: "autotrader-equity.service",
    note: "INTENTIONALLY OFF",
    kind: "LEGACY",
  },
];

/** The unit whose masked state must never be read as current equity trading. */
export const LEGACY_EQUITY_KEY = "equity_legacy";

export const A1B_SHADOW_KEY = "equity_a1b_shadow";

/**
 * Which unit each of the operational API's trail panels is actually about.
 *
 * The operational API sends its own labels, and the deployed build of it is
 * pinned to the crypto production checkout - so the page cannot rely on those
 * strings being the corrected ones. Names this specific are exactly what the
 * defect was about, so the frontend takes them from the registry above and
 * treats the payload's `label` as advisory.
 */
export const TRAIL_PANEL_UNITS: Readonly<Record<string, string>> = {
  crypto: "crypto",
  equity: LEGACY_EQUITY_KEY,
};

/** The label for a trail panel, by the unit it is really about. */
export function trailPanelLabel(panelKey: string, fallback: string): string {
  const unitKey = TRAIL_PANEL_UNITS[panelKey];
  return SERVICE_UNITS.find((spec) => spec.key === unitKey)?.label ?? fallback;
}

export function kindOf(key: string, sent?: ServiceKind): ServiceKind {
  return sent ?? SERVICE_UNITS.find((spec) => spec.key === key)?.kind ?? "TRADING";
}

/**
 * The word and colour a unit's status should carry on screen.
 *
 * A running observer says OBSERVING in the observation colour: green would
 * read as "trading", and that is precisely the claim it cannot make. A
 * masked legacy unit stays neutral; every other mapping is the backend's own.
 */
export function displayStatus(row: {
  key: string;
  status: string;
  tone: Tone;
  kind?: ServiceKind;
}): { status: string; tone: Tone } {
  const kind = kindOf(row.key, row.kind);
  if (kind === "OBSERVER" && row.status === "RUNNING") {
    return { status: "OBSERVING", tone: "SHADOW" };
  }
  return { status: row.status, tone: row.tone };
}

/** Rows for when the status source itself could not be read. */
export function unknownServiceRows(): HealthRow[] {
  return SERVICE_UNITS.map((spec) => ({
    key: `service_${spec.key}`,
    label: spec.label,
    status: "UNKNOWN",
    tone: "ATTENTION" as Tone,
    note: spec.note,
    unit: spec.unit,
    kind: spec.kind,
    detail: `The service manager could not be asked about ${spec.unit}. This is a statement about the query, not about the service.`,
  }));
}

function toHealthRow(unit: ServiceUnitRow): HealthRow {
  const shown = displayStatus(unit);
  return {
    key: `service_${unit.key}`,
    label: unit.label,
    status: shown.status,
    tone: shown.tone,
    note: unit.note,
    unit: unit.unit,
    kind: kindOf(unit.key, unit.kind),
    detail: unit.detail,
  };
}

/**
 * The health panel: the operational API's rows, with live service rows spliced
 * in where the trail-derived runtime rows used to be.
 *
 * Position matters as much as content. The service rows land exactly where the
 * old runtime rows were - after account safety and reconciliation, before
 * broker and database - so the panel still reads "may anything trade, is each
 * service up, is the plumbing connected" in that order.
 */
export function healthRows(
  components: HealthComponent[] | null | undefined,
  services: ServiceUnitsPanel | null,
): HealthRow[] {
  const source = components ?? [];
  const serviceRows = services?.units?.length
    ? services.units.map(toHealthRow)
    : unknownServiceRows();

  const kept: HealthRow[] = [];
  let insertAt = -1;
  for (const component of source) {
    // Every trail-derived runtime row is dropped, including the crypto one.
    // Keeping crypto and replacing only equity would put two rows about the
    // same service on one panel, sourced differently, free to disagree.
    if (component.key.startsWith(TRAIL_ROW_PREFIX)) {
      if (insertAt < 0) insertAt = kept.length;
      continue;
    }
    kept.push(component);
  }

  if (insertAt < 0) {
    // No runtime rows in the payload at all: seat the services after
    // reconciliation, which is where they belong in the reading order.
    const afterReconciliation = kept.findIndex((row) => row.key === "reconciliation");
    insertAt = afterReconciliation < 0 ? kept.length : afterReconciliation + 1;
  }

  return [...kept.slice(0, insertAt), ...serviceRows, ...kept.slice(insertAt)];
}

/** One service row by key, or null when the source could not be read. */
export function serviceUnit(
  services: ServiceUnitsPanel | null,
  key: string,
): ServiceUnitRow | null {
  return services?.units?.find((unit) => unit.key === key) ?? null;
}
