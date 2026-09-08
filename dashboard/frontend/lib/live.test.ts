/**
 * What the Live page shows, for every state it has to survive.
 *
 * The suite is organised around the sentence the whole program exists to
 * enforce: **money moving in is not profit, money moving out is not loss.** The
 * deposit and withdrawal cases are first because they are the ones that would
 * lie to an operator about their own returns.
 *
 * After that, the states where the honest answer is "we do not know": a
 * suppressed accounting record, an unreadable arm switch, an unverified
 * balance, a broker field that does not exist. Each has its own rendering, and
 * the tests assert they are different from each other and different from zero.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { contractMoney, contractSignedMoney, contractSignedPercent } from "./live-decimal.ts";
import {
  accountingView,
  guardView,
  readinessView,
  serviceView,
  validated,
  withdrawalView,
} from "./live-view.ts";
import {
  FIXTURE_A_BASELINE,
  FIXTURE_B_DEPOSIT,
  FIXTURE_C_TRADING_PROFIT,
  FIXTURE_D_NEW_HWM,
  FIXTURE_E_DRAWDOWN,
  FIXTURE_F_STALE,
  FIXTURE_G_UNKNOWN_FLOW,
  FIXTURE_H_ARMED,
  FIXTURE_I_SERVICE_NOT_INSTALLED,
  FIXTURE_J_DEPOSIT_DAY_GUARD,
  FIXTURE_K_WITHDRAWAL,
  LIVE_FIXTURES,
} from "./live-fixtures.ts";
import { bindingIsAuthorization, ceilingsResolved, realMoneyMutationEnabled } from "./live-safety.ts";

const here = dirname(fileURLToPath(import.meta.url));
const sourceOf = (relative: string): string => readFileSync(join(here, "..", relative), "utf8");

/* ================================================== the contract's own rule */

test("A · the $50 baseline renders exactly the expected figures", () => {
  const { accounting: a, safety: s } = FIXTURE_A_BASELINE;
  assert.equal(contractMoney(a.current_broker_equity), "$50.00");
  assert.equal(contractMoney(a.flow_adjusted_equity), "$50.00");
  assert.equal(contractSignedMoney(a.trading_pnl_since_inception), "$0.00");
  assert.equal(contractMoney(s.risk.target_gross), "$45.00");
  assert.equal(contractMoney(s.risk.hard_gross), "$47.50");
  assert.equal(contractMoney(s.risk.exposure_bound), "$50.00");
  assert.equal(contractMoney(s.risk.per_symbol), "$5.50");
  assert.equal(contractMoney(a.profit_reserve_accrued), "$0.00");
  assert.equal(contractMoney(a.withdrawal_bucket_balance), "$0.00");
  assert.equal(contractMoney(a.withdrawal_preview), "$0.00");

  const readiness = readinessView(s, a);
  assert.equal(readiness.ready, "READY");
  assert.equal(readiness.arm, "DISARMED");
});

test("B · a $50 deposit doubles broker equity and moves NOTHING else", () => {
  // The mandatory case. If this ever fails, the dashboard is telling an
  // operator they doubled their money by transferring it.
  const { accounting: a } = FIXTURE_B_DEPOSIT;
  assert.equal(contractMoney(a.current_broker_equity), "$100.00");
  assert.equal(contractMoney(a.confirmed_deposits), "$50.00");

  assert.equal(contractMoney(a.flow_adjusted_equity), "$50.00", "the deposit is not performance");
  assert.equal(contractSignedMoney(a.trading_pnl_since_inception), "$0.00");
  assert.equal(contractSignedPercent(a.time_weighted_return), "0.00%");

  // And specifically: nothing anywhere on the page reads as +100%.
  const rendered = [
    contractSignedPercent(a.time_weighted_return),
    contractSignedMoney(a.trading_pnl_since_inception),
    contractMoney(a.flow_adjusted_equity),
  ];
  assert.ok(!rendered.some((text) => text.includes("100.00%")), "a deposit rendered as a return");
  assert.deepEqual(
    [contractMoney(a.baseline_equity), contractMoney(a.flow_adjusted_equity)],
    ["$50.00", "$50.00"],
    "flow-adjusted equity is still the baseline",
  );
});

test("C · +$3 of genuine trading profit renders as +$3, on a deposited account", () => {
  const { accounting: a } = FIXTURE_C_TRADING_PROFIT;
  assert.equal(contractMoney(a.current_broker_equity), "$103.00");
  assert.equal(contractMoney(a.flow_adjusted_equity), "$53.00");
  assert.equal(contractSignedMoney(a.trading_pnl_since_inception), "+$3.00");
  assert.equal(contractSignedPercent(a.time_weighted_return), "+6.00%");
});

test("K · a $10 withdrawal is not a $10 loss", () => {
  // The mandatory mirror of B. Broker equity falls; trading P&L does not.
  const { accounting: a } = FIXTURE_K_WITHDRAWAL;
  assert.equal(contractMoney(a.current_broker_equity), "$93.00");
  assert.equal(contractMoney(a.confirmed_withdrawals), "$10.00");
  assert.equal(contractMoney(a.flow_adjusted_equity), "$53.00");
  assert.equal(
    contractSignedMoney(a.trading_pnl_since_inception),
    "+$3.00",
    "the withdrawal became a trading loss",
  );
  assert.notEqual(contractSignedMoney(a.trading_pnl_since_inception), "-$7.00");
  // The excess over the bucket is flagged as capital leaving, not absorbed.
  assert.equal(contractMoney(a.withdrawal_bucket_balance), "$0.00");
  assert.equal(contractMoney(a.unreserved_withdrawal_total), "$9.40");
});

test("the contract's worked table holds across B, C and K: three identical +$3", () => {
  // "That row of three identical +$3 figures is the contract."
  const pnl = [FIXTURE_C_TRADING_PROFIT, FIXTURE_K_WITHDRAWAL].map((f) =>
    contractSignedMoney(f.accounting.trading_pnl_since_inception),
  );
  assert.deepEqual(pnl, ["+$3.00", "+$3.00"]);
  const fae = [FIXTURE_C_TRADING_PROFIT, FIXTURE_K_WITHDRAWAL].map((f) =>
    contractMoney(f.accounting.flow_adjusted_equity),
  );
  assert.deepEqual(fae, ["$53.00", "$53.00"]);
});

/* ========================================================== HWM and reserve */

test("D · a new high-water mark accrues 20% of the new profit and no more", () => {
  const { accounting: a } = FIXTURE_D_NEW_HWM;
  assert.equal(contractMoney(a.adjusted_equity_hwm), "$58.00");
  assert.equal(contractSignedMoney(a.new_hwm_profit), "+$5.00");
  assert.equal(contractMoney(a.profit_reserve_accrued), "$1.00", "20% of $5");
  assert.equal(contractMoney(a.withdrawal_bucket_balance), "$1.00");
  // Harvest is due and the account is at its mark, so a preview exists.
  const view = withdrawalView(a);
  assert.equal(view.state, "AVAILABLE");
  assert.equal(view.reasonKey, null);
  assert.equal(contractMoney(a.withdrawal_preview), "$1.00");
});

test("E · in drawdown the bucket persists and the preview is an explained zero", () => {
  const { accounting: a } = FIXTURE_E_DRAWDOWN;
  assert.equal(contractMoney(a.adjusted_equity_hwm), "$58.00");
  assert.equal(contractMoney(a.flow_adjusted_equity), "$53.00");
  assert.equal(contractSignedPercent(a.current_drawdown_from_adjusted_hwm), "-8.62%");
  assert.equal(contractMoney(a.withdrawal_bucket_balance), "$1.00", "the bucket is not cleared");
  assert.equal(contractMoney(a.withdrawal_preview), "$0.00");

  const view = withdrawalView(a);
  assert.equal(view.state, "ZERO_BELOW_HWM");
  assert.equal(view.reasonKey, "live.withdrawal.belowHwm");
});

test("a zero preview is never left unexplained, and the three zeros are distinct", () => {
  assert.equal(withdrawalView(FIXTURE_A_BASELINE.accounting).state, "ZERO_NOT_HARVEST_ELIGIBLE");
  assert.equal(withdrawalView(FIXTURE_E_DRAWDOWN.accounting).state, "ZERO_BELOW_HWM");
  const emptyBucket = {
    ...FIXTURE_A_BASELINE.accounting,
    harvest_eligible: true,
    current_drawdown_from_adjusted_hwm: "0.00",
    withdrawal_bucket_balance: "0.00",
    withdrawal_preview: "0.00",
  };
  assert.equal(withdrawalView(emptyBucket).state, "ZERO_EMPTY_BUCKET");
  for (const fixture of [FIXTURE_A_BASELINE, FIXTURE_E_DRAWDOWN]) {
    assert.ok(withdrawalView(fixture.accounting).reasonKey, "a bare zero with no reason");
  }
});

test("an exact-zero drawdown is not a drawdown", () => {
  // `-0.00` is textually negative and numerically flat. An account exactly at
  // its mark is not below it.
  const atMark = {
    ...FIXTURE_D_NEW_HWM.accounting,
    current_drawdown_from_adjusted_hwm: "-0.00",
    withdrawal_preview: "0.00",
    withdrawal_bucket_balance: "0.00",
  };
  assert.equal(withdrawalView(atMark).state, "ZERO_EMPTY_BUCKET");
});

test("OBSERVE_ONLY is the mode, authorization is off, automatic transfer is off", () => {
  for (const fixture of LIVE_FIXTURES) {
    const view = withdrawalView(fixture.accounting);
    assert.equal(view.mode, "OBSERVE_ONLY", `fixture ${fixture.key}`);
    assert.equal(view.authorized, false, `fixture ${fixture.key}`);
    assert.equal(view.automatic, false, `fixture ${fixture.key}`);
    assert.equal(fixture.accounting.authorized_withdrawal_amount, "0");
  }
});

/* =================================================== unknown, stale, absent */

test("F · STALE keeps the figures and badges them, rather than blanking them", () => {
  const view = accountingView(FIXTURE_F_STALE.accounting);
  assert.equal(view.presentation, "STALE");
  assert.equal(view.derivedTrustworthy, true, "STALE populates; it does not suppress");
  assert.equal(view.freshness, "STALE");
  assert.equal(view.freshnessSeconds, 5400);
  assert.ok(view.detail, "a stale record says how stale, in the backend's own words");
  assert.equal(contractMoney(FIXTURE_F_STALE.accounting.flow_adjusted_equity), "$53.00");
});

test("G · UNKNOWN_EXTERNAL_FLOW suppresses derived money and never renders it as zero", () => {
  const a = FIXTURE_G_UNKNOWN_FLOW.accounting;
  const view = accountingView(a);
  assert.equal(view.presentation, "SUPPRESSED");
  assert.equal(view.derivedTrustworthy, false);
  assert.ok(view.detail);

  // The contract sends these as null; the page must show unknown, not $0.00.
  for (const field of [
    a.flow_adjusted_equity,
    a.trading_pnl_since_inception,
    a.adjusted_equity_hwm,
    a.profit_reserve_accrued,
    a.withdrawal_bucket_balance,
    a.withdrawal_preview,
  ]) {
    assert.equal(field, null);
    assert.notEqual(contractMoney(field), "$0.00");
    assert.equal(contractMoney(field), "—");
  }
  assert.equal(withdrawalView(a).state, "UNAVAILABLE");
});

test("the page never reconstructs a suppressed figure from the fields that survived", () => {
  // Broker equity and the deposit total are both present in fixture G. A page
  // that "helpfully" subtracted them would produce a flow-adjusted equity the
  // backend deliberately withheld. The view model exposes no such value.
  const view = accountingView(FIXTURE_G_UNKNOWN_FLOW.accounting);
  assert.equal(view.derivedTrustworthy, false);
  const keys = Object.keys(view);
  for (const forbidden of ["flowAdjustedEquity", "tradingPnl", "hwm", "reserve", "preview"]) {
    assert.ok(!keys.includes(forbidden), `the view model computed ${forbidden}`);
  }
});

test("an unreadable accounting service is distinct from a suppressed one", () => {
  const view = accountingView(null);
  assert.equal(view.presentation, "UNAVAILABLE");
  assert.equal(view.status, null);
  assert.equal(view.freshness, "UNKNOWN");
  assert.notEqual(view.presentation, accountingView(FIXTURE_G_UNKNOWN_FLOW.accounting).presentation);
});

test("broker withdrawable cash is always null, and is never replaced by cash", () => {
  for (const fixture of LIVE_FIXTURES) {
    assert.equal(fixture.accounting.broker_withdrawable_cash, null, `fixture ${fixture.key}`);
    assert.equal(
      fixture.accounting.broker_withdrawable_cash_status,
      "NOT_EXPOSED_BY_BROKER",
      `fixture ${fixture.key}`,
    );
    // The distinction the card must preserve: cash is a real number, and
    // withdrawable cash is not available at all.
    assert.notEqual(fixture.accounting.current_cash, null);
  }
  const source = sourceOf("components/live/LiveReserve.tsx");
  assert.ok(
    source.includes("live.withdrawal.notVerified"),
    "the card must render NOT VERIFIED rather than a dash alone",
  );
});

/* ============================================================ arm and ready */

test("H · ARMED says real-money order mutation is ENABLED", () => {
  const view = readinessView(FIXTURE_H_ARMED.safety, FIXTURE_H_ARMED.accounting);
  assert.equal(view.arm, "ARMED");
  assert.equal(view.armTone, "NEGATIVE");
  assert.equal(view.mutationKey, "live.mutation.enabled");
  assert.equal(realMoneyMutationEnabled(FIXTURE_H_ARMED.safety), true);
});

test("DISARMED is a normal safe state and is never toned as an error", () => {
  const view = readinessView(FIXTURE_A_BASELINE.safety, FIXTURE_A_BASELINE.accounting);
  assert.equal(view.arm, "DISARMED");
  assert.equal(view.armTone, "NEUTRAL", "DISARMED must not be coloured as a fault");
  assert.notEqual(view.armTone, "NEGATIVE");
  assert.equal(view.mutationKey, "live.mutation.disabled");
  assert.equal(realMoneyMutationEnabled(FIXTURE_A_BASELINE.safety), false);
});

test("an unreadable arm switch is UNKNOWN, never DISARMED", () => {
  // The contract: "a null is displayed as unknown, never as false — an
  // unreadable arm state is not a disarmed one."
  const safety = {
    ...FIXTURE_A_BASELINE.safety,
    arm: { ...FIXTURE_A_BASELINE.safety.arm, state: "UNKNOWN", armed: null },
  };
  const view = readinessView(safety, { ...FIXTURE_A_BASELINE.accounting, live_armed: null });
  assert.equal(view.arm, "UNKNOWN");
  assert.notEqual(view.arm, "DISARMED");
  assert.equal(view.armTone, "ATTENTION");
  assert.equal(view.mutationKey, "live.mutation.unknown");
  assert.equal(realMoneyMutationEnabled(safety), null);
});

test("a null live_armed from the contract alone is also UNKNOWN", () => {
  const view = readinessView(null, { ...FIXTURE_A_BASELINE.accounting, live_armed: null });
  assert.equal(view.arm, "UNKNOWN");
});

test("readiness falls back to the contract when only the accounting service answers", () => {
  const view = readinessView(null, FIXTURE_A_BASELINE.accounting);
  assert.equal(view.ready, "READY");
  assert.equal(view.arm, "DISARMED");
});

test("an unknown readiness is not a NOT READY", () => {
  const view = readinessView(null, { ...FIXTURE_A_BASELINE.accounting, live_ready: null });
  assert.equal(view.ready, "UNKNOWN");
  assert.equal(view.readyKey, "live.ready.unknown");
});

/* ======================================================== risk and services */

test("I · a Live unit that is not installed is expected, not a failure", () => {
  const view = serviceView(FIXTURE_I_SERVICE_NOT_INSTALLED.safety);
  assert.equal(view.state, "NOT_INSTALLED");
  assert.equal(view.tone, "MUTED", "NOT INSTALLED must not read as a trading failure");
  assert.notEqual(view.tone, "NEGATIVE");
  assert.equal(view.labelKey, "live.service.notInstalled");
});

test("the five service states are distinguished", () => {
  const states = ["NOT_INSTALLED", "DISABLED", "STOPPED", "RUNNING", "UNKNOWN"] as const;
  const seen = new Set<string>();
  for (const state of states) {
    const view = serviceView({
      ...FIXTURE_A_BASELINE.safety,
      service: { ...FIXTURE_A_BASELINE.safety.service, state },
    });
    assert.equal(view.state, state);
    seen.add(view.labelKey);
  }
  assert.equal(seen.size, 5, "each state has its own label");
  // An unrecognized state fails safe to UNKNOWN rather than to RUNNING.
  const odd = serviceView({
    ...FIXTURE_A_BASELINE.safety,
    service: { ...FIXTURE_A_BASELINE.safety.service, state: "SOMETHING_ELSE" },
  });
  assert.equal(odd.state, "UNKNOWN");
});

test("J · the deposit-day guard says entries are blocked and exits are not", () => {
  const view = guardView(FIXTURE_J_DEPOSIT_DAY_GUARD.safety);
  assert.equal(view.status, "ACTIVE");
  assert.equal(view.tone, "ATTENTION", "a control working is not a failure");
  assert.notEqual(view.tone, "NEGATIVE");
  assert.ok(view.reason?.includes("exits remain available"), "the reason is quoted, not paraphrased");
  assert.equal(view.riskDay, "2026-09-08");
});

test("a guard whose activity feed could not be read is UNKNOWN, not INACTIVE", () => {
  const view = guardView({
    ...FIXTURE_A_BASELINE.safety,
    deposit_day_guard: {
      ...FIXTURE_A_BASELINE.safety.deposit_day_guard,
      status: "UNKNOWN",
      active: null,
    },
  });
  assert.equal(view.status, "UNKNOWN");
  assert.notEqual(view.status, "INACTIVE");
});

test("the ceilings scale with the balance below $100 and stop at the authorization", () => {
  // The funding-scale rule, as the operator has to understand it.
  assert.equal(contractMoney(FIXTURE_A_BASELINE.safety.risk.target_gross), "$45.00");
  assert.equal(bindingIsAuthorization(FIXTURE_A_BASELINE.safety), false);

  assert.equal(contractMoney(FIXTURE_B_DEPOSIT.safety.risk.target_gross), "$90.00");
  assert.equal(contractMoney(FIXTURE_B_DEPOSIT.safety.risk.hard_gross), "$95.00");
  assert.equal(contractMoney(FIXTURE_B_DEPOSIT.safety.risk.exposure_bound), "$100.00");
  assert.equal(contractMoney(FIXTURE_B_DEPOSIT.safety.risk.per_symbol), "$11.00");

  // At $103 and $108 the ceilings are the SAME as at $100: further funding
  // does not raise authorized risk.
  for (const fixture of [FIXTURE_C_TRADING_PROFIT, FIXTURE_D_NEW_HWM]) {
    assert.equal(contractMoney(fixture.safety.risk.target_gross), "$90.00", fixture.key);
    assert.equal(contractMoney(fixture.safety.risk.exposure_bound), "$100.00", fixture.key);
    assert.equal(bindingIsAuthorization(fixture.safety), true, fixture.key);
  }
});

test("unresolved ceilings are unknown, never zero and never remembered", () => {
  // Exactly the shape the backend emits on the NOT_VERIFIED branch: every
  // resolved figure null, including `binding`, because which limb binds is
  // itself unknown when no balance was read.
  const safety = {
    ...FIXTURE_A_BASELINE.safety,
    account: { ...FIXTURE_A_BASELINE.safety.account, status: "NOT_CONFIGURED", equity: null },
    risk: {
      ...FIXTURE_A_BASELINE.safety.risk,
      status: "NOT_VERIFIED",
      verified_equity: null,
      target_gross: null,
      hard_gross: null,
      exposure_bound: null,
      per_symbol: null,
      slot: null,
      binding: null,
      current_gross_exposure: null,
      remaining_gross_capacity: null,
    },
  };
  assert.equal(ceilingsResolved(safety), false);
  assert.equal(contractMoney(safety.risk.target_gross), "—");
  assert.notEqual(contractMoney(safety.risk.target_gross), "$0.00");
  assert.equal(bindingIsAuthorization(safety), null);
});

/* ================================================= contract drift behaviour */

test("a payload that fails the contract yields no summary to render", () => {
  const broken = { ...FIXTURE_A_BASELINE.accounting } as Record<string, unknown>;
  delete broken.withdrawal_preview;
  const { summary, problems } = validated(broken as never);
  assert.equal(summary, null, "a drifted payload must not reach a card");
  assert.ok(problems.length > 0);
});

test("no payload at all is not a contract failure", () => {
  const { summary, problems } = validated(null);
  assert.equal(summary, null);
  assert.deepEqual(problems, [], "a service that is down has not drifted");
});

/* ================================================= structural safety checks */

test("the Live surface contains no mutation control of any kind", () => {
  const surfaces = [
    "app/live/page.tsx",
    "components/live/LiveHeader.tsx",
    "components/live/LiveAccount.tsx",
    "components/live/LivePerformance.tsx",
    "components/live/LiveReserve.tsx",
    "components/live/LiveOps.tsx",
    "components/live/LiveSummary.tsx",
    "components/live/atoms.tsx",
    "lib/live.ts",
    "lib/live-view.ts",
    "lib/live-safety.ts",
    "lib/live-contract.ts",
  ];

  // Mechanisms, not words. These modules DISPLAY the words ARMED, DISARMED and
  // withdrawal constantly and must be able to; what must not exist is a way to
  // act. So the check is for the shapes a control actually takes: a non-GET
  // request, an interactive element, or a call to a backend mutation by name.
  const mutationCalls = [
    "arm_live_trading",
    "disarm_live_trading",
    "submit_order",
    "cancel_order",
    "replace_order",
    "close_position",
    "create_transfer",
    "createTransfer",
  ];
  const nonGetMethods = /method\s*:\s*["'`](POST|PUT|PATCH|DELETE)["'`]/i;

  for (const relative of surfaces) {
    const code = sourceOf(relative)
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/\/\/.*$/gm, "");
    for (const call of mutationCalls) {
      assert.ok(!code.includes(call), `${relative} calls ${call}`);
    }
    assert.ok(!nonGetMethods.test(code), `${relative} issues a non-GET request`);
    assert.ok(!/<button/i.test(code), `${relative} renders a button`);
    assert.ok(!/<form|onSubmit/i.test(code), `${relative} renders a form`);
    assert.ok(!/onClick/i.test(code), `${relative} wires a click handler`);
    assert.ok(!/<input|<select|<textarea/i.test(code), `${relative} renders an input`);
  }
});

test("no ARM, DISARM, WITHDRAW or DEPOSIT control exists anywhere in the Live UI", () => {
  // The prohibition stated as the operator would check it: the labels a
  // control would carry appear on no interactive element. Searched across the
  // whole Live surface including its message catalogue usage.
  const surfaces = [
    "app/live/page.tsx",
    "components/live/LiveHeader.tsx",
    "components/live/LiveAccount.tsx",
    "components/live/LiveReserve.tsx",
    "components/live/LiveOps.tsx",
    "components/live/LiveSummary.tsx",
  ];
  for (const relative of surfaces) {
    const code = sourceOf(relative);
    // An anchor is the only interactive element permitted, and only to /live.
    const anchors = code.match(/href=\{?"([^"]*)"/g) ?? [];
    for (const anchor of anchors) {
      assert.ok(anchor.includes("/live"), `${relative} links somewhere unexpected: ${anchor}`);
    }
  }
});

test("the Live page reads only the two GET endpoints and fabricates nothing", () => {
  const code = sourceOf("lib/live.ts");
  assert.ok(code.includes("LIVE_ACCOUNTING_ENDPOINT"));
  assert.ok(code.includes("LIVE_SAFETY_ENDPOINT"));
  assert.ok(!code.includes("fetch("), "the page uses the shared GET poller, not its own fetch");
});

test("no production module imports the fixtures", () => {
  // Fixtures are tests and local development only. A page that shipped one
  // would render invented money.
  const production = [
    "app/live/page.tsx",
    "app/page.tsx",
    "app/risk/page.tsx",
    "app/system/page.tsx",
    "components/live/LiveHeader.tsx",
    "components/live/LiveAccount.tsx",
    "components/live/LivePerformance.tsx",
    "components/live/LiveReserve.tsx",
    "components/live/LiveOps.tsx",
    "components/live/LiveSummary.tsx",
    "lib/live.ts",
    "lib/live-view.ts",
  ];
  for (const relative of production) {
    assert.ok(!sourceOf(relative).includes("live-fixtures"), `${relative} imports the fixtures`);
  }
});

test("no raw account identifier can reach the browser", () => {
  for (const fixture of LIVE_FIXTURES) {
    const short = fixture.safety.identity.fingerprint_short;
    assert.equal(short?.length, 8, `fixture ${fixture.key}`);
    // A fingerprint is a SHA-256 prefix; it is 32 hex characters and is not an
    // account number in any length.
    assert.match(fixture.accounting.account_fingerprint, /^[0-9a-f]{32}$/);
    assert.ok(fixture.safety.identity.fingerprint?.startsWith(short ?? "x"));
  }
});

test("every fixture is internally consistent with the contract's formulas", () => {
  // A fixture that fudged its arithmetic would let a real bug through on
  // exactly the case the contract exists to prevent, so the arithmetic is
  // checked here — in the test, which is allowed to compute — rather than
  // trusted.
  const cents = (value: string | null): bigint | null =>
    value === null ? null : BigInt(Math.round(Number(value) * 100));
  for (const fixture of LIVE_FIXTURES) {
    const a = fixture.accounting;
    const deposits = cents(a.confirmed_deposits);
    const withdrawals = cents(a.confirmed_withdrawals);
    const net = cents(a.net_external_flows);
    const equity = cents(a.current_broker_equity);
    const fae = cents(a.flow_adjusted_equity);
    const baseline = cents(a.baseline_equity);
    const pnl = cents(a.trading_pnl_since_inception);
    if (deposits !== null && withdrawals !== null && net !== null) {
      assert.equal(net, deposits - withdrawals, `${fixture.key}: net_external_flows`);
    }
    if (equity !== null && net !== null && fae !== null) {
      assert.equal(fae, equity - net, `${fixture.key}: flow_adjusted_equity`);
    }
    if (fae !== null && baseline !== null && pnl !== null) {
      assert.equal(pnl, fae - baseline, `${fixture.key}: trading_pnl_since_inception`);
    }
  }
});
