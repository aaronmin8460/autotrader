import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const trading = readFileSync(
  new URL("../components/terminal/TradingPages.tsx", import.meta.url),
  "utf8",
);
const charts = readFileSync(
  new URL("../components/terminal/TerminalUI.tsx", import.meta.url),
  "utf8",
);

test("V6 Live presents the three authoritative capital series distinctly", () => {
  for (const label of ["Broker Equity", "Flow-Adjusted Equity", "External Cash Flow"]) {
    assert.match(charts, new RegExp(label));
  }
  assert.match(trading, /Live Capital & Performance/);
  assert.match(trading, /Net External Flows/);
});

test("V6 Paper has first-class chart surfaces without inventing history", () => {
  assert.match(trading, /Paper Equity Curve/);
  assert.match(trading, /PAPER EQUITY HISTORY NOT RECORDED/);
  assert.match(trading, /Paper Exposure & Cash History/);
  assert.match(trading, /No flat or synthetic curve is drawn/);
});

test("cash-earned language stays bounded by authoritative accounting", () => {
  assert.match(trading, /Accounting-defined performance since inception/);
  assert.match(trading, /Cash Performance \/ Cash Earned/);
  assert.match(trading, /NOT EXPOSED/);
  assert.match(trading, /not a realized-cash measure/);
});

test("V6 chart summaries display exact endpoint strings rather than client-computed money", () => {
  assert.match(charts, /contractMoney\(last\?\.broker_equity\)/);
  assert.match(charts, /contractMoney\(first\?\.flow_adjusted_equity\)/);
  assert.doesNotMatch(charts, /brokerCurrent\s*-\s*brokerStart/);
  assert.doesNotMatch(charts, /adjustedCurrent\s*-\s*adjustedStart/);
});
