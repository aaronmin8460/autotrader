import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { contractMoney, contractSignedMoney } from "./live-decimal.ts";
import { TERMINAL_EDGE_FIXTURES, TERMINAL_HISTORY, TERMINAL_NO_POSITIONS, TERMINAL_POSITION_HEAVY } from "./terminal-fixtures.ts";

const here = dirname(fileURLToPath(import.meta.url));

test("Terminal fixtures cover required edge states without null becoming zero", () => {
  const keys = TERMINAL_EDGE_FIXTURES.map((item) => item.key).join(" ");
  for (const required of ["baseline", "disarmed", "armed", "participate", "position-heavy", "defensive", "negative", "drawdown", "stale", "unknown-external-flow", "withdrawal", "broker-unavailable", "reconciliation-failure"]) assert.match(keys, new RegExp(required));
  assert.equal(contractMoney(null), "—");
  assert.equal(contractSignedMoney(null), "—");
});

test("rich fixture has partial fill and reject while at-most-once still passes", () => {
  assert.ok(TERMINAL_POSITION_HEAVY.orders.some((row) => row.status === "PARTIALLY_FILLED"));
  assert.ok(TERMINAL_POSITION_HEAVY.orders.some((row) => row.status === "REJECTED"));
  assert.equal(TERMINAL_POSITION_HEAVY.execution.at_most_once_status, "PASS");
  assert.equal(TERMINAL_POSITION_HEAVY.execution.duplicate_client_order_ids, 0);
});

test("history makes the deposit discontinuity explicit without calling it P&L", () => {
  assert.equal(TERMINAL_HISTORY.flows[0]?.classification, "DEPOSIT");
  assert.equal(TERMINAL_HISTORY.points[1]?.flow_adjusted_equity, TERMINAL_HISTORY.points[2]?.flow_adjusted_equity);
  assert.notEqual(TERMINAL_HISTORY.points[1]?.broker_equity, TERMINAL_HISTORY.points[2]?.broker_equity);
});

test("no-position state is authoritative and not a missing payload", () => {
  assert.equal(TERMINAL_NO_POSITIONS.positions.status, "OK");
  assert.equal(TERMINAL_NO_POSITIONS.positions.largest_position, null);
  assert.deepEqual(TERMINAL_NO_POSITIONS.positions.rows, []);
});

test("production modules never import the test-only Terminal fixtures", () => {
  const production = [
    "app/page.tsx",
    "components/terminal/CorePages.tsx",
    "components/terminal/OpsPages.tsx",
    "components/terminal/TerminalUI.tsx",
    "lib/terminal.tsx",
  ].map((relative) => readFileSync(join(here, "..", relative), "utf8")).join("\n");
  assert.doesNotMatch(production, /terminal-fixtures/);
});

test("Risk uses the server-selected largest position rather than row order", () => {
  const source = readFileSync(join(here, "..", "components/terminal/OpsPages.tsx"), "utf8");
  assert.match(source, /positions\.largest_position/);
  assert.doesNotMatch(source, /positions\.rows\[0\]/);
});

test("System identifies the unchanged trading hash as the Live runtime SHA", () => {
  const source = readFileSync(join(here, "..", "components/terminal/OpsPages.tsx"), "utf8");
  assert.match(source, /label: "Live runtime SHA"/);
  assert.doesNotMatch(source, /label: "Production SHA"/);
});

test("Paper and Live values stay in separate columns and sources", () => {
  const source = readFileSync(join(here, "..", "components/terminal/OpsPages.tsx"), "utf8");
  assert.match(source, /PAPER · SIMULATED CAPITAL/);
  assert.match(source, /LIVE · REAL MONEY/);
  assert.doesNotMatch(source, /paper[^\n]*\+[^\n]*live/i);
});
