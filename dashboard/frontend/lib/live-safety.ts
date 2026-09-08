/**
 * The real-money safety panel, as the browser consumes it.
 *
 * Mirrors `autotrader.dashboard.live_safety`. It answers the questions the
 * frozen accounting contract deliberately does not: may this system trade,
 * against which account, and inside what ceilings.
 *
 * **The ceilings are quoted, never computed.** `target_gross`, `hard_gross`,
 * `exposure_bound` and `per_symbol` arrive already resolved by
 * `live.budget.effective_ceilings`, which is the same arithmetic the risk
 * engine enforces. Nothing in the frontend multiplies an equity by a
 * percentage: a dashboard that derived its own ceilings would eventually draw
 * limits the engine is not applying, and the operator would have no way to tell
 * which of the two was real.
 *
 * `status` fields carry the same discipline as the accounting contract:
 * `NOT_VERIFIED`, `NOT_CONFIGURED`, `UNREADABLE` and `UNKNOWN` are distinct
 * values and none of them is zero.
 */

export const LIVE_SAFETY_ENDPOINT = "/api/live-safety/summary";

export type AccountReadStatus = "OK" | "NOT_CONFIGURED" | "UNREADABLE" | "ACCOUNT_MISMATCH";

export type CeilingsStatus = "RESOLVED" | "NOT_VERIFIED";

/** `UNKNOWN` is not `DISARMED`. An unreadable switch has not been proven off. */
export type ArmStateValue = "ARMED" | "DISARMED" | "UNKNOWN";

export type IdentityStatus = "PINNED" | "NOT_PINNED" | "MISMATCH" | "UNKNOWN";

/** `UNKNOWN` is not `INACTIVE`. A day whose cash movements are unknown is not a clean one. */
export type GuardStatus = "ACTIVE" | "INACTIVE" | "UNKNOWN";

/**
 * The Live unit, across the five states it can genuinely be in.
 *
 * `NOT_INSTALLED` is where it stands today by design — Prompt 1 prepared the
 * template and left installation as the first step of the first-day runbook —
 * and the page must not colour it as a fault.
 */
export type LiveServiceState =
  | "NOT_INSTALLED"
  | "DISABLED"
  | "STOPPED"
  | "RUNNING"
  | "UNKNOWN";

export interface LiveArmPanel {
  state: string;
  armed: boolean | null;
  reason: string | null;
  source: string | null;
  changed_at: string | null;
  code_sha: string | null;
}

export interface LiveIdentityPanel {
  status: string;
  fingerprint: string | null;
  fingerprint_short: string | null;
  detail: string | null;
}

export interface LiveAccountFacts {
  status: string;
  equity: string | null;
  cash: string | null;
  buying_power: string | null;
  account_status: string | null;
  account_type: string | null;
  multiplier: string | null;
  shorting_enabled: boolean | null;
  trading_blocked: boolean | null;
  account_blocked: boolean | null;
  transfers_blocked: boolean | null;
  position_count: number | null;
  open_order_count: number | null;
  read_at: string | null;
}

export interface LiveRiskEnvelope {
  status: string;
  policy_id: string;
  policy_config_hash: string;
  capital_bound: string | null;
  daily_loss_halt_fraction: string | null;
  verified_equity: string | null;
  target_gross: string | null;
  hard_gross: string | null;
  exposure_bound: string | null;
  per_symbol: string | null;
  slot: string | null;
  binding: string | null;
  current_gross_exposure: string | null;
  remaining_gross_capacity: string | null;
  universe_size: number | null;
  detail: string | null;
}

export interface DepositDayGuardPanel {
  status: string;
  active: boolean | null;
  risk_day: string | null;
  reason: string | null;
  cash_flow_count: number | null;
}

export interface LiveServicePanel {
  state: string;
  unit: string;
  detail: string | null;
  since: string | null;
}

export interface LiveReconciliationPanel {
  available: boolean;
  status: string | null;
  safe_to_trade: boolean | null;
  completed_at: string | null;
  issues: number | null;
  unresolved: number | null;
  detail: string | null;
}

export interface LiveSafetyPanel {
  generated_at: string;
  environment: string;
  live_ready: boolean | null;
  arm: LiveArmPanel;
  identity: LiveIdentityPanel;
  account: LiveAccountFacts;
  risk: LiveRiskEnvelope;
  deposit_day_guard: DepositDayGuardPanel;
  reconciliation: LiveReconciliationPanel;
  service: LiveServicePanel;
  code_sha: string | null;
  notices: string[];
}

/** The arm state, narrowed. Anything unrecognized reads `UNKNOWN`, never armed. */
export function armStateOf(panel: LiveSafetyPanel | null): ArmStateValue {
  const state = panel?.arm.state;
  if (state === "ARMED") return "ARMED";
  if (state === "DISARMED") return "DISARMED";
  return "UNKNOWN";
}

/**
 * Whether real-money order mutation is enabled right now.
 *
 * Three values, and the third one matters: `null` means the switch could not be
 * read, and the page says so rather than reassuring an operator that a system
 * it cannot see into is disarmed.
 */
export function realMoneyMutationEnabled(panel: LiveSafetyPanel | null): boolean | null {
  const state = armStateOf(panel);
  if (state === "ARMED") return true;
  if (state === "DISARMED") return false;
  return null;
}

/** The service state, narrowed. */
export function serviceStateOf(panel: LiveSafetyPanel | null): LiveServiceState {
  const state = panel?.service.state;
  switch (state) {
    case "NOT_INSTALLED":
    case "DISABLED":
    case "STOPPED":
    case "RUNNING":
      return state;
    default:
      return "UNKNOWN";
  }
}

/** Whether the ceilings on screen are the enforced ones. */
export function ceilingsResolved(panel: LiveSafetyPanel | null): boolean {
  return panel?.risk.status === "RESOLVED";
}

/**
 * Whether the account's own balance, rather than the $100 authorization, is
 * what currently binds the ceilings.
 *
 * The distinction is the whole of the funding-scale explanation: below the
 * authorization the percentages bind and the ceilings grow with the account;
 * at and above it the dollars bind and further funding changes nothing.
 */
export function bindingIsAuthorization(panel: LiveSafetyPanel | null): boolean | null {
  const binding = panel?.risk.binding;
  if (binding === "authorization") return true;
  if (binding === "balance") return false;
  return null;
}
