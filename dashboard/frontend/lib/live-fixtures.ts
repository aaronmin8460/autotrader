/**
 * Deterministic Live fixtures — tests and local development only.
 *
 * Prompt 2 is still implementing the accounting backend, so the Live page is
 * built against the frozen contract's *shape* rather than against a running
 * service. These eleven states are what the page is designed and tested for.
 *
 * **Every fixture satisfies the contract's own formulas.** Not decoration: the
 * point of fixture B is that a $50 deposit moves broker equity to $100 while
 * `flow_adjusted_equity` stays at $50 and trading P&L stays at $0, and a
 * fixture that fudged the arithmetic would let a bug through on exactly the
 * case the whole contract exists to prevent.
 *
 *     net_external_flows           = confirmed_deposits - confirmed_withdrawals
 *     flow_adjusted_equity         = current_broker_equity - net_external_flows
 *     trading_pnl_since_inception  = flow_adjusted_equity - baseline_equity
 *     current_drawdown_...         = flow_adjusted_equity / adjusted_equity_hwm - 1
 *
 * The four ceilings in every safety fixture were produced by running the real
 * `live.budget.effective_ceilings` against that fixture's equity, not typed by
 * hand. $50 gives $45.00 / $47.50 / $50.00 / $5.50; $103 gives $90 / $95 / $100
 * / $11 with `binding: "authorization"`, which is the funding-scale rule made
 * visible.
 *
 * **None of this reaches production.** Nothing in `app/` imports this module;
 * the page reads the two live endpoints. `live.test.ts` asserts that.
 */

import type { LiveAccountingSummary } from "./live-contract";
import type { LiveSafetyPanel } from "./live-safety";

const PINNED_FINGERPRINT = "a6bbf9c116a5679c58719d82d7e4b3e2";
const POLICY_HASH = "ecf003eb9a86814f2583c4e981bf39365e942e0e4dcc683c6c742fb3c00292d5";
const RESERVE_POLICY_HASH = "7c1de5f0c1a94b2e8d3f6a05b9e27c41d8a06f3b5e94c27a1f0b8d6e35c94a72";

/** The shape shared by every accounting fixture. Overridden field by field. */
const BASE_SUMMARY: LiveAccountingSummary = {
  contract_version: "1.0.0",
  generated_at: "2026-09-08T14:05:00+00:00",
  account_fingerprint: PINNED_FINGERPRINT,
  environment: "LIVE",

  accounting_inception_at: "2026-09-08T13:30:00+00:00",
  baseline_equity: "50.00",
  baseline_cash: "50.00",

  current_broker_equity: "50.00",
  current_cash: "50.00",
  broker_withdrawable_cash: null,
  broker_withdrawable_cash_status: "NOT_EXPOSED_BY_BROKER",

  confirmed_deposits: "0.00",
  confirmed_withdrawals: "0.00",
  net_external_flows: "0.00",
  pending_flows_excluded_count: 0,
  unknown_external_flow_count: 0,
  last_external_flow_at: null,

  flow_adjusted_equity: "50.00",
  trading_pnl_since_inception: "0.00",
  realized_pnl: "0.00",
  unrealized_pnl: "0.00",
  realized_pnl_basis_status: "CLEAN",

  time_weighted_return: "0.00",
  twr_convention: "CONSERVATIVE_MIN",
  twr_subperiods: 1,
  twr_bounded_subperiods: 0,

  adjusted_equity_hwm: "50.00",
  adjusted_equity_hwm_at: "2026-09-08T13:30:00+00:00",
  observed_intraday_peak: "50.00",
  current_drawdown_from_adjusted_hwm: "0.00",

  profit_reserve_policy_id: "PROFIT_RESERVE_OBSERVE_V1",
  profit_reserve_policy_hash: RESERVE_POLICY_HASH,
  profit_reserve_rate: "0.20",
  new_hwm_profit: "0.00",
  profit_reserve_accrued: "0.00",
  withdrawal_bucket_balance: "0.00",
  unreserved_withdrawal_total: "0.00",
  withdrawal_preview: "0.00",

  harvest_eligible: false,
  next_harvest_checkpoint_at: "2026-09-30T23:59:59+00:00",
  withdrawal_authorized: false,
  authorized_withdrawal_amount: "0",
  withdrawal_mode: "OBSERVE_ONLY",
  automatic_transfer_enabled: false,

  last_accounting_checkpoint_at: "2026-09-08T14:00:00+00:00",
  last_completed_day_checkpoint_at: "2026-09-07T20:00:00+00:00",
  accounting_status: "CLEAN",
  accounting_status_detail: null,
  data_freshness_seconds: 300,
  data_freshness: "FRESH",
  rebuild_count: 0,
  last_rebuild_at: null,

  live_ready: true,
  live_armed: false,
};

/** The shape shared by every safety fixture. */
const BASE_SAFETY: LiveSafetyPanel = {
  generated_at: "2026-09-08T14:05:00+00:00",
  environment: "LIVE",
  live_ready: true,
  arm: {
    state: "DISARMED",
    armed: false,
    reason:
      "No arm state has been recorded in this store, so nothing has ever authorized " +
      "real-money order submission from it.",
    source: "DEFAULT",
    changed_at: null,
    code_sha: null,
  },
  identity: {
    status: "PINNED",
    fingerprint: PINNED_FINGERPRINT,
    fingerprint_short: "a6bbf9c1",
    detail: null,
  },
  account: {
    status: "OK",
    equity: "50.00",
    cash: "50.00",
    buying_power: "50.00",
    account_status: "ACTIVE",
    account_type: "CASH",
    multiplier: "1",
    shorting_enabled: false,
    trading_blocked: false,
    account_blocked: false,
    transfers_blocked: false,
    position_count: 0,
    open_order_count: 0,
    read_at: "2026-09-08T14:05:00+00:00",
  },
  risk: {
    status: "RESOLVED",
    policy_id: "LIVE_VALIDATION_100",
    policy_config_hash: POLICY_HASH,
    capital_bound: "100",
    daily_loss_halt_fraction: "0.02",
    verified_equity: "50.00",
    target_gross: "45.00",
    hard_gross: "47.50",
    exposure_bound: "50.00",
    per_symbol: "5.50",
    slot: "4.50",
    binding: "balance",
    current_gross_exposure: "0.00",
    remaining_gross_capacity: "47.50",
    universe_size: 10,
    detail: null,
  },
  deposit_day_guard: {
    status: "INACTIVE",
    active: false,
    risk_day: "2026-09-08",
    reason: null,
    cash_flow_count: 0,
  },
  reconciliation: {
    available: true,
    status: "CLEAN",
    safe_to_trade: true,
    completed_at: "2026-09-08T13:29:00+00:00",
    issues: 0,
    unresolved: 0,
    detail: null,
  },
  service: {
    state: "NOT_INSTALLED",
    unit: "autotrader-equity-live.service",
    detail:
      "The real-money unit is prepared but not installed. Installing and starting it " +
      "DISARMED is the first step of the first-day runbook.",
    since: null,
  },
  code_sha: "fd6cae4",
  notices: [],
};

function summary(patch: Partial<LiveAccountingSummary>): LiveAccountingSummary {
  return { ...BASE_SUMMARY, ...patch };
}

function safety(patch: {
  arm?: Partial<LiveSafetyPanel["arm"]>;
  account?: Partial<LiveSafetyPanel["account"]>;
  risk?: Partial<LiveSafetyPanel["risk"]>;
  deposit_day_guard?: Partial<LiveSafetyPanel["deposit_day_guard"]>;
  service?: Partial<LiveSafetyPanel["service"]>;
  identity?: Partial<LiveSafetyPanel["identity"]>;
  reconciliation?: Partial<LiveSafetyPanel["reconciliation"]>;
  live_ready?: boolean | null;
  notices?: string[];
}): LiveSafetyPanel {
  return {
    ...BASE_SAFETY,
    live_ready: patch.live_ready ?? BASE_SAFETY.live_ready,
    notices: patch.notices ?? BASE_SAFETY.notices,
    arm: { ...BASE_SAFETY.arm, ...patch.arm },
    identity: { ...BASE_SAFETY.identity, ...patch.identity },
    account: { ...BASE_SAFETY.account, ...patch.account },
    risk: { ...BASE_SAFETY.risk, ...patch.risk },
    deposit_day_guard: { ...BASE_SAFETY.deposit_day_guard, ...patch.deposit_day_guard },
    reconciliation: { ...BASE_SAFETY.reconciliation, ...patch.reconciliation },
    service: { ...BASE_SAFETY.service, ...patch.service },
  };
}

export interface LiveFixture {
  key: string;
  /** What an operator would be looking at. One line, for the report and the test name. */
  title: string;
  accounting: LiveAccountingSummary;
  safety: LiveSafetyPanel;
}

/** The ceilings at $103 and $108: the authorization binds, not the balance. */
const CEILINGS_ABOVE_AUTHORIZATION = {
  target_gross: "90.00",
  hard_gross: "95.00",
  exposure_bound: "100.00",
  per_symbol: "11.00",
  slot: "9.00",
  binding: "authorization",
};

/* --------------------------------------------------------------- A .. K -- */

/** A — the account as it actually stands today. */
export const FIXTURE_A_BASELINE: LiveFixture = {
  key: "A",
  title: "$50 baseline, READY, DISARMED, no profit",
  accounting: summary({}),
  safety: safety({}),
};

/**
 * B — a $50 deposit lands.
 *
 * The one that matters most. Broker equity doubles; `flow_adjusted_equity` and
 * trading P&L do not move at all. A dashboard that showed +100% here would be
 * telling an operator they had doubled their money by transferring it.
 */
export const FIXTURE_B_DEPOSIT: LiveFixture = {
  key: "B",
  title: "$100 after a confirmed $50 deposit — no fake profit",
  accounting: summary({
    current_broker_equity: "100.00",
    current_cash: "100.00",
    confirmed_deposits: "50.00",
    net_external_flows: "50.00",
    last_external_flow_at: "2026-09-08T14:00:00+00:00",
    flow_adjusted_equity: "50.00",
    trading_pnl_since_inception: "0.00",
    time_weighted_return: "0.00",
    twr_subperiods: 2,
    twr_bounded_subperiods: 1,
    observed_intraday_peak: "50.00",
  }),
  safety: safety({
    account: { equity: "100.00", cash: "100.00", buying_power: "100.00" },
    risk: {
      verified_equity: "100.00",
      target_gross: "90.00",
      hard_gross: "95.00",
      exposure_bound: "100.00",
      per_symbol: "11.00",
      slot: "9.00",
      binding: "balance",
      remaining_gross_capacity: "95.00",
    },
  }),
};

/** C — genuine trading profit on top of the deposit. */
export const FIXTURE_C_TRADING_PROFIT: LiveFixture = {
  key: "C",
  title: "+$3 genuine trading P&L on a deposited account",
  accounting: summary({
    current_broker_equity: "103.00",
    current_cash: "61.40",
    confirmed_deposits: "50.00",
    net_external_flows: "50.00",
    last_external_flow_at: "2026-09-08T14:00:00+00:00",
    flow_adjusted_equity: "53.00",
    trading_pnl_since_inception: "3.00",
    realized_pnl: "1.25",
    unrealized_pnl: "1.75",
    time_weighted_return: "0.06",
    twr_subperiods: 3,
    twr_bounded_subperiods: 1,
    adjusted_equity_hwm: "53.00",
    adjusted_equity_hwm_at: "2026-09-08T20:00:00+00:00",
    observed_intraday_peak: "53.40",
    current_drawdown_from_adjusted_hwm: "0.00",
    last_completed_day_checkpoint_at: "2026-09-08T20:00:00+00:00",
  }),
  safety: safety({
    account: {
      equity: "103.00",
      cash: "61.40",
      buying_power: "61.40",
      position_count: 4,
    },
    risk: {
      ...CEILINGS_ABOVE_AUTHORIZATION,
      verified_equity: "103.00",
      current_gross_exposure: "41.60",
      remaining_gross_capacity: "53.40",
    },
  }),
};

/**
 * D — a new high-water mark, and the reserve that accrues against it.
 *
 * The adjusted HWM moves $53 → $58, so new HWM profit is $5 and 20% of it —
 * $1.00 — accrues to the bucket. The other $4 goes nowhere and is not marked.
 * Every dollar stays in the broker account; the bucket is a line in a ledger.
 */
export const FIXTURE_D_NEW_HWM: LiveFixture = {
  key: "D",
  title: "New HWM at $58, $1.00 reserve accrued, preview available",
  accounting: summary({
    current_broker_equity: "108.00",
    current_cash: "58.00",
    confirmed_deposits: "50.00",
    net_external_flows: "50.00",
    last_external_flow_at: "2026-09-08T14:00:00+00:00",
    flow_adjusted_equity: "58.00",
    trading_pnl_since_inception: "8.00",
    realized_pnl: "5.00",
    unrealized_pnl: "3.00",
    time_weighted_return: "0.16",
    twr_subperiods: 12,
    twr_bounded_subperiods: 1,
    adjusted_equity_hwm: "58.00",
    adjusted_equity_hwm_at: "2026-09-30T20:00:00+00:00",
    observed_intraday_peak: "58.90",
    current_drawdown_from_adjusted_hwm: "0.00",
    new_hwm_profit: "5.00",
    profit_reserve_accrued: "1.00",
    withdrawal_bucket_balance: "1.00",
    withdrawal_preview: "1.00",
    harvest_eligible: true,
    next_harvest_checkpoint_at: "2026-10-31T23:59:59+00:00",
    last_completed_day_checkpoint_at: "2026-09-30T20:00:00+00:00",
  }),
  safety: safety({
    account: { equity: "108.00", cash: "58.00", buying_power: "58.00", position_count: 5 },
    risk: {
      ...CEILINGS_ABOVE_AUTHORIZATION,
      verified_equity: "108.00",
      current_gross_exposure: "50.00",
      remaining_gross_capacity: "45.00",
    },
  }),
};

/**
 * E — below the high-water mark.
 *
 * The bucket keeps its $1.00 and the preview is $0.00. Nobody should harvest
 * out of a drawdown, and the number on the screen must not suggest it.
 */
export const FIXTURE_E_DRAWDOWN: LiveFixture = {
  key: "E",
  title: "Drawdown below HWM — bucket persists, preview $0.00",
  accounting: summary({
    current_broker_equity: "103.00",
    current_cash: "55.00",
    confirmed_deposits: "50.00",
    net_external_flows: "50.00",
    last_external_flow_at: "2026-09-08T14:00:00+00:00",
    flow_adjusted_equity: "53.00",
    trading_pnl_since_inception: "3.00",
    realized_pnl: "5.00",
    unrealized_pnl: "-2.00",
    time_weighted_return: "0.06",
    twr_subperiods: 18,
    twr_bounded_subperiods: 1,
    adjusted_equity_hwm: "58.00",
    adjusted_equity_hwm_at: "2026-09-30T20:00:00+00:00",
    observed_intraday_peak: "58.90",
    current_drawdown_from_adjusted_hwm: "-0.086207",
    new_hwm_profit: "0.00",
    profit_reserve_accrued: "1.00",
    withdrawal_bucket_balance: "1.00",
    withdrawal_preview: "0.00",
    harvest_eligible: true,
    last_completed_day_checkpoint_at: "2026-10-05T20:00:00+00:00",
  }),
  safety: safety({
    account: { equity: "103.00", cash: "55.00", buying_power: "55.00", position_count: 5 },
    risk: {
      ...CEILINGS_ABOVE_AUTHORIZATION,
      verified_equity: "103.00",
      current_gross_exposure: "48.00",
      remaining_gross_capacity: "47.00",
    },
  }),
};

/** F — the accounting record is older than the 900-second horizon. */
export const FIXTURE_F_STALE: LiveFixture = {
  key: "F",
  title: "Accounting STALE — figures populated, badged stale",
  accounting: summary({
    generated_at: "2026-09-08T15:30:00+00:00",
    current_broker_equity: "103.00",
    confirmed_deposits: "50.00",
    net_external_flows: "50.00",
    flow_adjusted_equity: "53.00",
    trading_pnl_since_inception: "3.00",
    adjusted_equity_hwm: "53.00",
    accounting_status: "STALE",
    accounting_status_detail:
      "The last accounting checkpoint is 5400 seconds old, beyond the 900-second horizon. " +
      "The figures below were true at that checkpoint and have not been refreshed since.",
    data_freshness: "STALE",
    data_freshness_seconds: 5400,
    last_accounting_checkpoint_at: "2026-09-08T14:00:00+00:00",
  }),
  safety: safety({
    account: { equity: "103.00", cash: "61.40" },
    risk: { ...CEILINGS_ABOVE_AUTHORIZATION, verified_equity: "103.00" },
  }),
};

/**
 * G — an activity could not be classified, so accounting fails closed.
 *
 * Every derived money field is `null`. The page must render "unavailable" and
 * must not reconstruct any of them from the fields that survived.
 */
export const FIXTURE_G_UNKNOWN_FLOW: LiveFixture = {
  key: "G",
  title: "UNKNOWN_EXTERNAL_FLOW — derived figures suppressed to null",
  accounting: summary({
    current_broker_equity: "103.00",
    current_cash: "61.40",
    confirmed_deposits: null,
    confirmed_withdrawals: null,
    net_external_flows: null,
    unknown_external_flow_count: 1,
    flow_adjusted_equity: null,
    trading_pnl_since_inception: null,
    realized_pnl: null,
    unrealized_pnl: null,
    time_weighted_return: null,
    adjusted_equity_hwm: null,
    adjusted_equity_hwm_at: null,
    observed_intraday_peak: null,
    current_drawdown_from_adjusted_hwm: null,
    new_hwm_profit: null,
    profit_reserve_accrued: null,
    withdrawal_bucket_balance: null,
    unreserved_withdrawal_total: null,
    withdrawal_preview: null,
    accounting_status: "UNKNOWN_EXTERNAL_FLOW",
    accounting_status_detail:
      "One broker activity could not be classified as a trade or an external flow. " +
      "Every derived figure is withheld until it is resolved.",
  }),
  safety: safety({
    account: { equity: "103.00", cash: "61.40" },
    risk: { ...CEILINGS_ABOVE_AUTHORIZATION, verified_equity: "103.00" },
  }),
};

/** H — real-money order mutation is enabled. The loudest state on the page. */
export const FIXTURE_H_ARMED: LiveFixture = {
  key: "H",
  title: "Live ARMED — real-money order mutation enabled",
  accounting: summary({ live_armed: true }),
  safety: safety({
    arm: {
      state: "ARMED",
      armed: true,
      reason: "First-day validation window opened by the operator.",
      source: "OPERATOR",
      changed_at: "2026-09-08T13:31:00+00:00",
      code_sha: "fd6cae4",
    },
    service: { state: "RUNNING", since: "2026-09-08T13:28:00+00:00", detail: null },
  }),
};

/** I — the unit is prepared and not installed. Expected today; not a fault. */
export const FIXTURE_I_SERVICE_NOT_INSTALLED: LiveFixture = {
  key: "I",
  title: "Live service NOT INSTALLED — the expected pre-deployment state",
  accounting: summary({}),
  safety: safety({ service: { state: "NOT_INSTALLED" } }),
};

/** J — a non-trade cash movement landed on the risk day. */
export const FIXTURE_J_DEPOSIT_DAY_GUARD: LiveFixture = {
  key: "J",
  title: "Deposit-day guard ACTIVE — new entries blocked, exits available",
  accounting: summary({
    current_broker_equity: "100.00",
    current_cash: "100.00",
    confirmed_deposits: "50.00",
    net_external_flows: "50.00",
    last_external_flow_at: "2026-09-08T14:00:00+00:00",
    flow_adjusted_equity: "50.00",
    trading_pnl_since_inception: "0.00",
  }),
  safety: safety({
    account: { equity: "100.00", cash: "100.00", buying_power: "100.00" },
    risk: {
      verified_equity: "100.00",
      target_gross: "90.00",
      hard_gross: "95.00",
      exposure_bound: "100.00",
      per_symbol: "11.00",
      slot: "9.00",
      binding: "balance",
      remaining_gross_capacity: "95.00",
    },
    deposit_day_guard: {
      status: "ACTIVE",
      active: true,
      risk_day: "2026-09-08",
      cash_flow_count: 1,
      reason:
        "1 non-trade cash movement(s) (CSD) settled on 2026-09-08. The UTC-day loss " +
        "baseline was taken against a different amount of capital, so today's equity " +
        "change is not a trading result and the daily-loss halt would be measuring the " +
        "transfer. No new entries today; exits remain available. The next risk day " +
        "establishes a fresh baseline against the settled balance.",
    },
    notices: [
      "Deposit-day guard active: new entries are blocked; exits remain available.",
    ],
  }),
};

/**
 * K — a $10 manual withdrawal settles.
 *
 * Broker equity falls to $93. Trading P&L stays at **+$3**: money leaving is
 * not a loss. The withdrawal exceeded the $0.60 then in the bucket, so the
 * bucket goes to zero — never negative — and $9.40 is recorded as unreserved,
 * meaning capital rather than harvested profit left the account.
 */
export const FIXTURE_K_WITHDRAWAL: LiveFixture = {
  key: "K",
  title: "-$10 withdrawal — trading P&L stays +$3",
  accounting: summary({
    current_broker_equity: "93.00",
    current_cash: "51.40",
    confirmed_deposits: "50.00",
    confirmed_withdrawals: "10.00",
    net_external_flows: "40.00",
    last_external_flow_at: "2026-09-09T14:00:00+00:00",
    flow_adjusted_equity: "53.00",
    trading_pnl_since_inception: "3.00",
    realized_pnl: "1.25",
    unrealized_pnl: "1.75",
    time_weighted_return: "0.06",
    twr_subperiods: 6,
    twr_bounded_subperiods: 2,
    adjusted_equity_hwm: "53.00",
    adjusted_equity_hwm_at: "2026-09-08T20:00:00+00:00",
    observed_intraday_peak: "53.40",
    current_drawdown_from_adjusted_hwm: "0.00",
    new_hwm_profit: "0.00",
    profit_reserve_accrued: "0.60",
    withdrawal_bucket_balance: "0.00",
    unreserved_withdrawal_total: "9.40",
    withdrawal_preview: "0.00",
    last_completed_day_checkpoint_at: "2026-09-08T20:00:00+00:00",
  }),
  safety: safety({
    account: { equity: "93.00", cash: "51.40", buying_power: "51.40", position_count: 4 },
    risk: {
      verified_equity: "93.00",
      target_gross: "83.70",
      hard_gross: "88.35",
      exposure_bound: "93.00",
      per_symbol: "10.23",
      slot: "8.37",
      binding: "balance",
      current_gross_exposure: "41.60",
      remaining_gross_capacity: "46.75",
    },
  }),
};

export const LIVE_FIXTURES: readonly LiveFixture[] = [
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
];

export function fixture(key: string): LiveFixture {
  const found = LIVE_FIXTURES.find((candidate) => candidate.key === key);
  if (!found) throw new Error(`No Live fixture ${key}`);
  return found;
}
