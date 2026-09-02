/**
 * The composition that keeps five services apart, and the navigation that
 * exposes the three sections.
 *
 * The defect these tests pin rendered as one line - "Equity runtime STOPPED" -
 * on a host where equity paper trading was running. It was a provenance bug:
 * the row was computed from the masked legacy service's trail. So most of what
 * is asserted here is which source a row came from, not how it looks - plus
 * the newer rule that a running observer is OBSERVING, never a green RUNNING.
 */

import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { test } from "node:test";

import {
  A1B_SHADOW_KEY,
  LEGACY_EQUITY_KEY,
  SERVICE_UNITS,
  displayStatus,
  healthRows,
  serviceUnit,
  trailPanelLabel,
  unknownServiceRows,
  type HealthRow,
  type ServiceUnitRow,
  type ServiceUnitsPanel,
} from "./services.ts";
import type { HealthComponent } from "./types.ts";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const source = (relative: string): string => readFileSync(join(root, relative), "utf8");

function walk(directory: string, out: string[] = []): string[] {
  for (const entry of readdirSync(directory)) {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) walk(path, out);
    else if (/\.(ts|tsx)$/.test(entry) && !entry.endsWith(".test.ts")) out.push(path);
  }
  return out;
}

const SOURCE_FILES = [...walk(join(root, "lib")), ...walk(join(root, "components")), ...walk(join(root, "app"))];

function unit(overrides: Partial<ServiceUnitRow> & { key: string }): ServiceUnitRow {
  const spec = SERVICE_UNITS.find((candidate) => candidate.key === overrides.key)!;
  return {
    label: spec.label,
    unit: spec.unit,
    status: "RUNNING",
    tone: "POSITIVE",
    note: spec.note,
    detail: "",
    expected: true,
    healthy: true,
    kind: spec.kind,
    load_state: "loaded",
    active_state: "active",
    sub_state: "running",
    unit_file_state: "enabled",
    ...overrides,
  };
}

/** The host as it actually is: four up, the legacy one masked. */
function livePanel(): ServiceUnitsPanel {
  return {
    available: true,
    generated_at: "2026-09-02T18:30:00+00:00",
    source: "SYSTEMD",
    unavailable_reason: null,
    units: [
      unit({ key: "crypto" }),
      unit({ key: "equity_paper" }),
      unit({ key: "equity_shadow" }),
      unit({ key: "equity_a1b_shadow" }),
      unit({
        key: "equity_legacy",
        status: "MASKED",
        tone: "MUTED",
        active_state: "inactive",
        load_state: "masked",
        unit_file_state: "masked",
        detail: "autotrader-equity.service is masked and cannot be started.",
      }),
    ],
  };
}

/** What the operational API sends: two runtime rows, both from one store. */
function operationalHealth(): HealthComponent[] {
  return [
    { key: "account_safety", label: "Account safety", status: "SAFE", tone: "POSITIVE", detail: null },
    { key: "reconciliation", label: "Reconciliation", status: "CLEAN", tone: "POSITIVE", detail: null },
    { key: "runtime_crypto", label: "Crypto runtime", status: "RUNNING", tone: "POSITIVE", detail: null },
    { key: "runtime_equity", label: "Equity runtime", status: "NEVER STARTED", tone: "MUTED", detail: null },
    { key: "broker", label: "Broker", status: "CONNECTED", tone: "POSITIVE", detail: null },
    { key: "database", label: "Database", status: "CONNECTED", tone: "POSITIVE", detail: null },
    { key: "trading_safety", label: "Trading safety", status: "ALLOWED", tone: "NEUTRAL", detail: null },
  ];
}

const byLabel = (rows: HealthRow[], label: string): HealthRow => rows.find((row) => row.label === label)!;

// =========================================================================
// The five rows, and the words on them
// =========================================================================

test("Equity Paper active renders RUNNING in green", () => {
  const row = byLabel(healthRows(operationalHealth(), livePanel()), "Equity Paper · EDA-1");
  assert.equal(row.status, "RUNNING");
  assert.equal(row.tone, "POSITIVE");
  assert.equal(row.unit, "autotrader-equity-paper.service");
});

test("Crypto Paper active renders RUNNING and is never hardcoded", () => {
  assert.equal(byLabel(healthRows(operationalHealth(), livePanel()), "Crypto Paper").status, "RUNNING");
  const stopped = livePanel();
  stopped.units[0] = unit({ key: "crypto", status: "STOPPED", tone: "NEGATIVE", active_state: "inactive", healthy: false, expected: false });
  assert.equal(byLabel(healthRows(operationalHealth(), stopped), "Crypto Paper").status, "STOPPED");
});

test("a running observer renders OBSERVING in the observation colour, never green RUNNING", () => {
  const rows = healthRows(operationalHealth(), livePanel());
  for (const label of ["Equity Shadow", "A1-B U30 Shadow"]) {
    const row = byLabel(rows, label);
    assert.equal(row.status, "OBSERVING", label);
    assert.equal(row.tone, "SHADOW", label);
    assert.equal(row.kind, "OBSERVER", label);
    assert.match(row.note!, /OBSERVATION ONLY/);
    assert.match(row.note!, /ZERO ORDERS/);
  }
});

test("the A1-B U30 Shadow row is visible, named, and mapped to its own unit", () => {
  const row = byLabel(healthRows(operationalHealth(), livePanel()), "A1-B U30 Shadow");
  assert.equal(row.unit, "autotrader-equity-a1b-shadow.service");
  assert.equal(serviceUnit(livePanel(), A1B_SHADOW_KEY)!.unit, "autotrader-equity-a1b-shadow.service");
});

test("an observer that is not running keeps the backend's word and tone", () => {
  assert.deepEqual(displayStatus({ key: "equity_a1b_shadow", status: "FAILED", tone: "NEGATIVE" }), { status: "FAILED", tone: "NEGATIVE" });
  assert.deepEqual(displayStatus({ key: "equity_a1b_shadow", status: "STOPPED", tone: "NEGATIVE" }), { status: "STOPPED", tone: "NEGATIVE" });
  // A trader never becomes OBSERVING, and an older payload without `kind` still resolves from the registry.
  assert.deepEqual(displayStatus({ key: "equity_paper", status: "RUNNING", tone: "POSITIVE" }), { status: "RUNNING", tone: "POSITIVE" });
  assert.deepEqual(displayStatus({ key: "equity_shadow", status: "RUNNING", tone: "POSITIVE" }), { status: "OBSERVING", tone: "SHADOW" });
});

test("legacy equity masked renders MASKED, neutral, with its standing note", () => {
  const row = byLabel(healthRows(operationalHealth(), livePanel()), "Legacy Equity Runtime");
  assert.equal(row.status, "MASKED");
  assert.equal(row.tone, "MUTED");
  assert.equal(row.note, "INTENTIONALLY OFF");
  assert.equal(row.kind, "LEGACY");
});

test("no row on a correctly configured host uses the failure colour", () => {
  const services = healthRows(operationalHealth(), livePanel()).filter((row) => row.key.startsWith("service_"));
  assert.equal(services.length, 5);
  for (const row of services) assert.notEqual(row.tone, "NEGATIVE", `${row.label} is red on a healthy host`);
});

// =========================================================================
// Provenance and order
// =========================================================================

test("Equity Paper status is never derived from autotrader-equity.service", () => {
  for (const legacyStatus of ["MASKED", "STOPPED", "FAILED", "RUNNING"]) {
    const panel = livePanel();
    panel.units[4] = unit({ key: "equity_legacy", status: legacyStatus as ServiceUnitRow["status"], tone: "MUTED" });
    assert.equal(byLabel(healthRows(operationalHealth(), panel), "Equity Paper · EDA-1").status, "RUNNING");
  }
});

test("every trail-derived runtime row is dropped from the health panel", () => {
  const rows = healthRows(operationalHealth(), livePanel());
  assert.equal(rows.filter((row) => row.key.startsWith("runtime_")).length, 0);
  assert.equal(rows.filter((row) => row.label === "Equity runtime").length, 0);
});

test("the panel reads in the required order", () => {
  assert.deepEqual(
    healthRows(operationalHealth(), livePanel()).map((row) => row.label),
    [
      "Account safety",
      "Reconciliation",
      "Crypto Paper",
      "Equity Paper · EDA-1",
      "Equity Shadow",
      "A1-B U30 Shadow",
      "Legacy Equity Runtime",
      "Broker",
      "Database",
      "Trading safety",
    ],
  );
});

test("each service row maps to exactly one unit", () => {
  const units = healthRows(operationalHealth(), livePanel())
    .filter((row) => row.key.startsWith("service_"))
    .map((row) => row.unit);
  assert.equal(new Set(units).size, units.length);
  assert.deepEqual(units, [
    "autotrader-crypto.service",
    "autotrader-equity-paper.service",
    "autotrader-equity-shadow.service",
    "autotrader-equity-a1b-shadow.service",
    "autotrader-equity.service",
  ]);
});

test("an unreadable status source reports unknown, never stopped or running", () => {
  const rows = healthRows(operationalHealth(), null);
  const services = rows.filter((row) => row.key.startsWith("service_"));
  assert.equal(services.length, 5);
  for (const row of services) assert.equal(row.status, "UNKNOWN");
  assert.equal(rows.filter((row) => row.status === "STOPPED").length, 0);
});

test("the fallback rows name the same five units as the live ones", () => {
  assert.deepEqual(unknownServiceRows().map((row) => row.unit), livePanel().units.map((row) => row.unit));
});

test("serviceUnit finds the legacy unit by key and nothing else by accident", () => {
  assert.equal(serviceUnit(livePanel(), LEGACY_EQUITY_KEY)!.unit, "autotrader-equity.service");
  assert.equal(serviceUnit(null, LEGACY_EQUITY_KEY), null);
  assert.equal(serviceUnit(livePanel(), "nope"), null);
});

test("a trail panel is titled by the unit it is really about", () => {
  assert.equal(trailPanelLabel("crypto", "Crypto runtime"), "Crypto Paper");
  assert.equal(trailPanelLabel("equity", "Equity runtime"), "Legacy Equity Runtime");
  assert.equal(trailPanelLabel("something-new", "Something New"), "Something New");
});

// =========================================================================
// Navigation
// =========================================================================

test("the navigation exposes exactly the three sections", () => {
  const nav = source("components/Nav.tsx");
  for (const href of ['href: "/"', 'href: "/equity-paper"', 'href: "/shadows"']) {
    assert.ok(nav.includes(href), `Nav.tsx is missing ${href}`);
  }
  assert.ok(!nav.includes('"/equity-shadow"'), "a per-strategy shadow tab crept back into the nav");
  assert.ok(!nav.includes('"/a1b"'), "a per-strategy shadow tab crept back into the nav");
});

test("the old shadow route still resolves, as a redirect into the workspace", () => {
  assert.ok(source("app/equity-shadow/page.tsx").includes('redirect("/shadows")'));
  for (const page of ["app/page.tsx", "app/equity-paper/page.tsx", "app/shadows/page.tsx"]) {
    assert.ok(source(page).length > 0, `${page} is empty`);
  }
});

test("positions stay the broker-global account view", () => {
  const text = source("components/Positions.tsx");
  assert.ok(text.includes("Broker account positions"));
  assert.ok(text.includes("Alpaca paper account"));
  assert.ok(!text.includes("filter("));
});

// =========================================================================
// Still read-only
// =========================================================================

test("no component, page or lib issues a mutating request or renders a form", () => {
  for (const file of SOURCE_FILES) {
    const text = readFileSync(file, "utf8");
    for (const forbidden of ['method: "POST"', 'method: "PUT"', 'method: "PATCH"', 'method: "DELETE"', "<form", "onSubmit"]) {
      assert.ok(!text.includes(forbidden), `${file} contains ${forbidden}`);
    }
    // Buttons exist only for view state (ranges, closing a drawer) and are never submit buttons.
    const buttons = text.match(/<button\b[^>]*>/gs) ?? [];
    for (const button of buttons) assert.ok(button.includes('type="button"'), `${file}: ${button.slice(0, 60)}`);
  }
});

test("the services endpoint is a GET with no control path", () => {
  const text = source("lib/services.ts");
  assert.ok(text.includes('"/api/equity-paper/services"'));
  for (const verb of ["/start", "/stop", "/restart", "/unmask", "/enable", "/disable"]) {
    assert.ok(!text.includes(verb), `lib/services.ts names ${verb}`);
  }
});

test("every fetch in the frontend is a plain GET", () => {
  for (const file of SOURCE_FILES) {
    const text = readFileSync(file, "utf8");
    for (const match of text.matchAll(/fetch\(([^)]*)\)/gs)) {
      assert.ok(!/method\s*:/.test(match[1] ?? ""), `${file} sets a fetch method`);
    }
  }
});
