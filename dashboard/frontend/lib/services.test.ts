/**
 * The composition that keeps three equity services apart, and the nav that
 * exposes all three pages.
 *
 * The defect these tests pin rendered as one line - "Equity runtime STOPPED" -
 * on a host where equity paper trading was running. It was a provenance bug:
 * the row was computed from the masked legacy service's trail. So most of what
 * is asserted here is which source a row came from, not how it looks.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { test } from "node:test";

import {
  LEGACY_EQUITY_KEY,
  SERVICE_UNITS,
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
const source = (relative: string): string =>
  readFileSync(join(here, "..", relative), "utf8");

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
    load_state: "loaded",
    active_state: "active",
    sub_state: "running",
    unit_file_state: "enabled",
    ...overrides,
  };
}

/** The host as it actually is: three up, the legacy one masked. */
function livePanel(): ServiceUnitsPanel {
  return {
    available: true,
    generated_at: "2026-08-31T18:30:00+00:00",
    source: "SYSTEMD",
    unavailable_reason: null,
    units: [
      unit({ key: "crypto" }),
      unit({ key: "equity_paper" }),
      unit({ key: "equity_shadow" }),
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
    { key: "reconciliation", label: "Reconciliation", status: "REPAIRED", tone: "NEUTRAL", detail: null },
    { key: "runtime_crypto", label: "Crypto Paper", status: "RUNNING", tone: "POSITIVE", detail: null },
    {
      key: "runtime_equity",
      label: "Legacy Equity Runtime",
      status: "STOPPED",
      tone: "MUTED",
      detail: "This runtime recorded a clean shutdown.",
    },
    { key: "broker", label: "Broker", status: "CONNECTED", tone: "POSITIVE", detail: null },
    { key: "database", label: "Database", status: "CONNECTED", tone: "POSITIVE", detail: null },
    { key: "trading_safety", label: "Trading safety", status: "ALLOWED", tone: "NEUTRAL", detail: null },
  ];
}

const byLabel = (rows: HealthRow[], label: string): HealthRow =>
  rows.find((row) => row.label === label)!;

// =========================================================================
// 1-3. The four rows, and the words on them
// =========================================================================

test("Equity Paper active renders RUNNING", () => {
  const rows = healthRows(operationalHealth(), livePanel());
  const row = byLabel(rows, "Equity Paper · EDA-1");

  assert.equal(row.status, "RUNNING");
  assert.equal(row.tone, "POSITIVE");
  assert.equal(row.unit, "autotrader-equity-paper.service");
});

test("Equity Shadow active renders RUNNING", () => {
  const row = byLabel(healthRows(operationalHealth(), livePanel()), "Equity Shadow");

  assert.equal(row.status, "RUNNING");
  assert.equal(row.tone, "POSITIVE");
  assert.equal(row.unit, "autotrader-equity-shadow.service");
});

test("Crypto Paper active renders RUNNING and is never hardcoded", () => {
  const rows = healthRows(operationalHealth(), livePanel());
  assert.equal(byLabel(rows, "Crypto Paper").status, "RUNNING");

  const stopped = livePanel();
  stopped.units[0] = unit({
    key: "crypto",
    status: "STOPPED",
    tone: "NEGATIVE",
    active_state: "inactive",
    healthy: false,
    expected: false,
  });
  assert.equal(byLabel(healthRows(operationalHealth(), stopped), "Crypto Paper").status, "STOPPED");
});

test("legacy equity masked renders MASKED, not a generic STOPPED", () => {
  const row = byLabel(healthRows(operationalHealth(), livePanel()), "Legacy Equity Runtime");

  assert.equal(row.status, "MASKED");
  assert.notEqual(row.status, "STOPPED");
  assert.equal(row.unit, "autotrader-equity.service");
});

// =========================================================================
// 4. Masked is neutral, not an error
// =========================================================================

test("the masked legacy row is neutral rather than an error", () => {
  const row = byLabel(healthRows(operationalHealth(), livePanel()), "Legacy Equity Runtime");

  assert.equal(row.tone, "MUTED");
  assert.notEqual(row.tone, "NEGATIVE");
  assert.notEqual(row.tone, "ATTENTION");
});

test("no row on a correctly configured host uses the failure colour", () => {
  const rows = healthRows(operationalHealth(), livePanel());
  const services = rows.filter((row) => row.key.startsWith("service_"));

  assert.equal(services.length, 4);
  for (const row of services) {
    assert.notEqual(row.tone, "NEGATIVE", `${row.label} is red on a healthy host`);
  }
});

test("the legacy row carries a standing note that it is off on purpose", () => {
  const row = byLabel(healthRows(operationalHealth(), livePanel()), "Legacy Equity Runtime");

  assert.equal(row.note, "INTENTIONALLY OFF");
});

test("the trading rows say no real money whatever their status", () => {
  const rows = healthRows(operationalHealth(), livePanel());

  assert.match(byLabel(rows, "Equity Paper · EDA-1").note!, /NO REAL MONEY/);
  assert.match(byLabel(rows, "Crypto Paper").note!, /NO REAL MONEY/);
  assert.match(byLabel(rows, "Equity Shadow").note!, /ZERO ORDERS/);
});

// =========================================================================
// 5. Provenance
// =========================================================================

test("Equity Paper status is never derived from autotrader-equity.service", () => {
  // The operational payload's only equity row says STOPPED, from the legacy
  // service's trail. The paper row must be unmoved by it in every direction.
  for (const legacyStatus of ["MASKED", "STOPPED", "FAILED", "RUNNING"]) {
    const panel = livePanel();
    panel.units[3] = unit({
      key: "equity_legacy",
      status: legacyStatus as ServiceUnitRow["status"],
      tone: "MUTED",
    });
    const row = byLabel(healthRows(operationalHealth(), panel), "Equity Paper · EDA-1");
    assert.equal(row.status, "RUNNING");
  }
});

test("every trail-derived runtime row is dropped from the health panel", () => {
  const rows = healthRows(operationalHealth(), livePanel());

  assert.equal(rows.filter((row) => row.key.startsWith("runtime_")).length, 0);
  assert.equal(rows.filter((row) => row.label === "Equity runtime").length, 0);
});

test("each service row maps to exactly one unit", () => {
  const rows = healthRows(operationalHealth(), livePanel()).filter((row) =>
    row.key.startsWith("service_"),
  );
  const units = rows.map((row) => row.unit);

  assert.equal(new Set(units).size, units.length);
  assert.deepEqual(units, [
    "autotrader-crypto.service",
    "autotrader-equity-paper.service",
    "autotrader-equity-shadow.service",
    "autotrader-equity.service",
  ]);
});

test("the panel reads in the required order", () => {
  const rows = healthRows(operationalHealth(), livePanel());

  assert.deepEqual(
    rows.map((row) => row.label),
    [
      "Account safety",
      "Reconciliation",
      "Crypto Paper",
      "Equity Paper · EDA-1",
      "Equity Shadow",
      "Legacy Equity Runtime",
      "Broker",
      "Database",
      "Trading safety",
    ],
  );
});

test("an unreadable status source reports unknown, never stopped or running", () => {
  const rows = healthRows(operationalHealth(), null);
  const services = rows.filter((row) => row.key.startsWith("service_"));

  assert.equal(services.length, 4);
  for (const row of services) {
    assert.equal(row.status, "UNKNOWN");
  }
  // And it must not have fallen back to the misleading trail row.
  assert.equal(rows.filter((row) => row.status === "STOPPED").length, 0);
});

test("the fallback rows name the same four units as the live ones", () => {
  assert.deepEqual(
    unknownServiceRows().map((row) => row.unit),
    livePanel().units.map((row) => row.unit),
  );
  assert.deepEqual(
    unknownServiceRows().map((row) => row.label),
    livePanel().units.map((row) => row.label),
  );
});

test("serviceUnit finds the legacy unit by key and nothing else by accident", () => {
  assert.equal(serviceUnit(livePanel(), LEGACY_EQUITY_KEY)!.unit, "autotrader-equity.service");
  assert.equal(serviceUnit(null, LEGACY_EQUITY_KEY), null);
  assert.equal(serviceUnit(livePanel(), "nope"), null);
});

test("a payload with no runtime rows still seats the services after reconciliation", () => {
  const withoutRuntimes = operationalHealth().filter((row) => !row.key.startsWith("runtime_"));
  const rows = healthRows(withoutRuntimes, livePanel());

  assert.equal(rows[1]!.key, "reconciliation");
  assert.equal(rows[2]!.label, "Crypto Paper");
  assert.equal(rows[6]!.label, "Broker");
});

// =========================================================================
// 6. Navigation
// =========================================================================

test("both navigations expose /equity-paper and /equity-shadow", () => {
  for (const file of ["components/Header.tsx", "components/ShadowNav.tsx"]) {
    const text = source(file);
    assert.ok(text.includes('href="/equity-paper"'), `${file} is missing /equity-paper`);
    assert.ok(text.includes('href="/equity-shadow"'), `${file} is missing /equity-shadow`);
    assert.ok(text.includes('href="/"'), `${file} is missing the operations link`);
  }
});

test("neither page was removed", () => {
  for (const page of ["app/equity-paper/page.tsx", "app/equity-shadow/page.tsx"]) {
    assert.ok(source(page).length > 0, `${page} is empty`);
  }
});

test("the operations page is not described as crypto-only", () => {
  const header = source("components/Header.tsx");

  assert.ok(header.includes("Broker account"));
  assert.ok(!header.includes("Crypto · paper"));
});

// =========================================================================
// 7 & 10. Still read-only
// =========================================================================

test("no component or lib issues a mutating request", () => {
  const files = [
    "lib/services.ts",
    "lib/api.ts",
    "components/SystemHealth.tsx",
    "components/Runtime.tsx",
    "components/Header.tsx",
    "components/Positions.tsx",
    "app/page.tsx",
  ];
  for (const file of files) {
    const text = source(file);
    for (const forbidden of [
      'method: "POST"',
      'method: "PUT"',
      'method: "PATCH"',
      'method: "DELETE"',
      "<form",
      "<button",
      "onSubmit",
    ]) {
      assert.ok(!text.includes(forbidden), `${file} contains ${forbidden}`);
    }
  }
});

test("the services endpoint is a GET with no control path", () => {
  const text = source("lib/services.ts");

  assert.ok(text.includes('"/api/equity-paper/services"'));
  for (const verb of ["/start", "/stop", "/restart", "/unmask", "/enable", "/disable"]) {
    assert.ok(!text.includes(verb), `lib/services.ts names ${verb}`);
  }
});

// =========================================================================
// 8 & 9. No regression to what already worked
// =========================================================================

test("the crypto page's non-runtime rows survive untouched", () => {
  const rows = healthRows(operationalHealth(), livePanel());

  for (const key of ["account_safety", "reconciliation", "broker", "database", "trading_safety"]) {
    const original = operationalHealth().find((row) => row.key === key)!;
    const rendered = rows.find((row) => row.key === key)!;
    assert.equal(rendered.status, original.status);
    assert.equal(rendered.tone, original.tone);
    assert.equal(rendered.label, original.label);
  }
});

test("positions stay the broker-global account view", () => {
  const text = source("components/Positions.tsx");

  assert.ok(text.includes("Broker account positions"));
  assert.ok(text.includes("Alpaca paper account"));
  // The panel renders whatever rows the broker returns; nothing filters by
  // asset class, which is what makes it the account rather than a book.
  assert.ok(!text.includes("CRYPTO\"") && !text.includes("filter("));
});

// =========================================================================
// The runtime cards take their names from the registry, not the payload
// =========================================================================

test("a trail panel is titled by the unit it is really about", () => {
  // The deployed operational API is pinned and still sends its own labels.
  assert.equal(trailPanelLabel("crypto", "Crypto runtime"), "Crypto Paper");
  assert.equal(trailPanelLabel("equity", "Equity runtime"), "Legacy Equity Runtime");
});

test("an unrecognised trail panel keeps its own label rather than guessing", () => {
  assert.equal(trailPanelLabel("something-new", "Something New"), "Something New");
});

test("no runtime card can be titled with the ambiguous name", () => {
  for (const [key, sent] of [
    ["crypto", "Crypto runtime"],
    ["equity", "Equity runtime"],
  ] as const) {
    assert.notEqual(trailPanelLabel(key, sent), "Equity runtime");
  }
});
