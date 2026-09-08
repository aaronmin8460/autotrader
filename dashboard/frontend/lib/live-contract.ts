/**
 * The frozen Live accounting contract, as the browser consumes it.
 *
 * Fifty-four fields, one canonical name each, transcribed from
 * `live_accounting_contract.json` at contract SHA
 * `a1dc50902c5d56e284033e2f3ae1f7ed4b917134`. The JSON file is the authority;
 * this is its TypeScript face, and `live-contract.test.ts` asserts the two
 * agree field for field. **If Prompt 2 finishes on a different SHA that test
 * fails and integration stops** — the failure is the feature.
 *
 * Three rules from the contract document are enforced here rather than left to
 * each component to remember:
 *
 * 1. **Money is an exact decimal string.** Every money and ratio field is typed
 *    `string`, never `number`, and is formatted by `lib/live-decimal.ts` without
 *    a float round trip. A field typed `number` here would be a field somebody
 *    eventually does arithmetic on.
 * 2. **`null` means unknown, and never zero.** Nullability below is copied from
 *    the contract, not chosen. `parseSummary` preserves `null` and never
 *    substitutes a default.
 * 3. **Non-`CLEAN` status suppresses derived figures.** The backend sends them
 *    as `null`; `derivedFiguresTrustworthy` says whether they may be presented
 *    as current, and the page renders the reason rather than a stale number.
 *
 * There is no forbidden alias in this file. `flow_adjusted_equity` is THE name
 * for broker equity net of external flows; `adjusted_equity`,
 * `performance_equity` and `normalized_equity` appear nowhere in this codebase
 * and the test asserts their absence.
 *
 * Nothing here computes money. There is no formula in this module, no
 * subtraction, and no fallback that reconstructs a suppressed field from the
 * ones beside it. The backend calculates; this displays.
 */

/** The contract SHA this frontend is built against. Changing it is a decision. */
export const LIVE_CONTRACT_SHA = "a1dc50902c5d56e284033e2f3ae1f7ed4b917134";

/** The contract version the payload must announce. */
export const LIVE_CONTRACT_VERSION = "1.0.0";

/** The base SHA the contract was frozen against. */
export const LIVE_CONTRACT_BASE_SHA = "fd6cae4a2ef1f92ec5e6133774a12f847c139e41";

/** The single authoritative route. */
export const LIVE_ACCOUNTING_ENDPOINT = "/api/live-accounting/summary";

/** The staleness horizon the contract document states, in seconds. */
export const STALENESS_HORIZON_SECONDS = 900;

/**
 * The 54 field names, in contract order.
 *
 * Order is preserved so a diff against the JSON reads as a diff rather than as
 * a reshuffle.
 */
export const LIVE_CONTRACT_FIELDS = [
  "contract_version",
  "generated_at",
  "account_fingerprint",
  "environment",
  "accounting_inception_at",
  "baseline_equity",
  "baseline_cash",
  "current_broker_equity",
  "current_cash",
  "broker_withdrawable_cash",
  "broker_withdrawable_cash_status",
  "confirmed_deposits",
  "confirmed_withdrawals",
  "net_external_flows",
  "pending_flows_excluded_count",
  "unknown_external_flow_count",
  "last_external_flow_at",
  "flow_adjusted_equity",
  "trading_pnl_since_inception",
  "realized_pnl",
  "unrealized_pnl",
  "realized_pnl_basis_status",
  "time_weighted_return",
  "twr_convention",
  "twr_subperiods",
  "twr_bounded_subperiods",
  "adjusted_equity_hwm",
  "adjusted_equity_hwm_at",
  "observed_intraday_peak",
  "current_drawdown_from_adjusted_hwm",
  "profit_reserve_policy_id",
  "profit_reserve_policy_hash",
  "profit_reserve_rate",
  "new_hwm_profit",
  "profit_reserve_accrued",
  "withdrawal_bucket_balance",
  "unreserved_withdrawal_total",
  "withdrawal_preview",
  "harvest_eligible",
  "next_harvest_checkpoint_at",
  "withdrawal_authorized",
  "authorized_withdrawal_amount",
  "withdrawal_mode",
  "automatic_transfer_enabled",
  "last_accounting_checkpoint_at",
  "last_completed_day_checkpoint_at",
  "accounting_status",
  "accounting_status_detail",
  "data_freshness_seconds",
  "data_freshness",
  "rebuild_count",
  "last_rebuild_at",
  "live_ready",
  "live_armed",
] as const;

export type LiveContractField = (typeof LIVE_CONTRACT_FIELDS)[number];

/**
 * Names this system must never emit for a concept that already has one.
 *
 * From the contract's canonical-naming table. Asserted against the source of
 * the Live modules, because "a second name for one of these numbers is a
 * defect, not a convenience".
 */
export const FORBIDDEN_ALIASES = [
  "adjusted_equity",
  "performance_equity",
  "normalized_equity",
  "peak_equity",
  "high_water",
  "reserved_cash",
  "available_to_withdraw",
] as const;

/** `accounting_status`, exhaustively. */
export type AccountingStatus =
  | "NOT_INITIALIZED"
  | "CLEAN"
  | "STALE"
  | "UNKNOWN_EXTERNAL_FLOW"
  | "REBUILD_REQUIRED"
  | "BROKER_UNAVAILABLE"
  | "ACCOUNT_MISMATCH";

export const ACCOUNTING_STATUSES: readonly AccountingStatus[] = [
  "NOT_INITIALIZED",
  "CLEAN",
  "STALE",
  "UNKNOWN_EXTERNAL_FLOW",
  "REBUILD_REQUIRED",
  "BROKER_UNAVAILABLE",
  "ACCOUNT_MISMATCH",
];

/**
 * The statuses under which the backend suppresses derived money to `null`.
 *
 * Listed from the contract's own table. `STALE` is deliberately NOT here: under
 * `STALE` the figures are populated and are badged stale, which is a different
 * instruction to the reader from "these are not available".
 */
export const SUPPRESSING_STATUSES: readonly AccountingStatus[] = [
  "NOT_INITIALIZED",
  "UNKNOWN_EXTERNAL_FLOW",
  "REBUILD_REQUIRED",
  "BROKER_UNAVAILABLE",
  "ACCOUNT_MISMATCH",
];

export type DataFreshness = "FRESH" | "STALE" | "UNKNOWN";

export type WithdrawableCashStatus = "VERIFIED" | "NOT_EXPOSED_BY_BROKER" | "UNKNOWN";

export type RealizedBasisStatus =
  | "CLEAN"
  | "BASIS_DIVERGENCE"
  | "DEGRADED"
  | "MISMATCH"
  | "UNKNOWN";

/**
 * The payload of `GET /api/live-accounting/summary`.
 *
 * Every money and ratio field is a decimal string. Every `| null` is the
 * contract's own nullability, and means unknown.
 */
export interface LiveAccountingSummary {
  contract_version: string;
  generated_at: string;
  account_fingerprint: string;
  environment: string;

  accounting_inception_at: string | null;
  baseline_equity: string | null;
  baseline_cash: string | null;

  current_broker_equity: string | null;
  current_cash: string | null;
  broker_withdrawable_cash: string | null;
  broker_withdrawable_cash_status: string;

  confirmed_deposits: string | null;
  confirmed_withdrawals: string | null;
  net_external_flows: string | null;
  pending_flows_excluded_count: number;
  unknown_external_flow_count: number;
  last_external_flow_at: string | null;

  flow_adjusted_equity: string | null;
  trading_pnl_since_inception: string | null;
  realized_pnl: string | null;
  unrealized_pnl: string | null;
  realized_pnl_basis_status: string | null;

  time_weighted_return: string | null;
  twr_convention: string;
  twr_subperiods: number;
  twr_bounded_subperiods: number;

  adjusted_equity_hwm: string | null;
  adjusted_equity_hwm_at: string | null;
  observed_intraday_peak: string | null;
  current_drawdown_from_adjusted_hwm: string | null;

  profit_reserve_policy_id: string;
  profit_reserve_policy_hash: string;
  profit_reserve_rate: string;
  new_hwm_profit: string | null;
  profit_reserve_accrued: string | null;
  withdrawal_bucket_balance: string | null;
  unreserved_withdrawal_total: string | null;
  withdrawal_preview: string | null;

  harvest_eligible: boolean;
  next_harvest_checkpoint_at: string | null;
  withdrawal_authorized: boolean;
  authorized_withdrawal_amount: string;
  withdrawal_mode: string;
  automatic_transfer_enabled: boolean;

  last_accounting_checkpoint_at: string | null;
  last_completed_day_checkpoint_at: string | null;
  accounting_status: string;
  accounting_status_detail: string | null;
  data_freshness_seconds: number | null;
  data_freshness: string;
  rebuild_count: number;
  last_rebuild_at: string | null;

  live_ready: boolean | null;
  live_armed: boolean | null;
}

/* ------------------------------------------------------------- validation -- */

/** Which fields must be present and non-null, by wire type. */
const REQUIRED_STRINGS: readonly LiveContractField[] = [
  "contract_version",
  "generated_at",
  "account_fingerprint",
  "environment",
  "broker_withdrawable_cash_status",
  "twr_convention",
  "profit_reserve_policy_id",
  "profit_reserve_policy_hash",
  "profit_reserve_rate",
  "authorized_withdrawal_amount",
  "withdrawal_mode",
  "accounting_status",
  "data_freshness",
];

const REQUIRED_INTEGERS: readonly LiveContractField[] = [
  "pending_flows_excluded_count",
  "unknown_external_flow_count",
  "twr_subperiods",
  "twr_bounded_subperiods",
  "rebuild_count",
];

const REQUIRED_BOOLEANS: readonly LiveContractField[] = [
  "harvest_eligible",
  "withdrawal_authorized",
  "automatic_transfer_enabled",
];

export interface ParseResult {
  /** The payload, when it satisfied the contract. */
  summary: LiveAccountingSummary | null;
  /** What was wrong, in words. Empty when `summary` is set. */
  problems: string[];
}

/**
 * Validate a payload against the frozen contract before anything renders it.
 *
 * Refuses rather than coerces. A payload missing a field, carrying an
 * unexpected type, or announcing a different `contract_version` comes back with
 * `summary: null` and the reasons — because a page that quietly rendered a
 * partial payload would be exactly the silent adaptation the freeze exists to
 * prevent.
 *
 * Unknown extra keys are reported but do not invalidate: an added field is a
 * contract change worth telling the operator about, and is not a reason to
 * blank a screen that is otherwise correct.
 */
export function parseSummary(payload: unknown): ParseResult {
  const problems: string[] = [];
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return { summary: null, problems: ["The payload is not a JSON object."] };
  }
  const record = payload as Record<string, unknown>;

  for (const field of LIVE_CONTRACT_FIELDS) {
    if (!(field in record)) problems.push(`Missing contract field: ${field}`);
  }
  for (const key of Object.keys(record)) {
    if (!(LIVE_CONTRACT_FIELDS as readonly string[]).includes(key)) {
      problems.push(`Unknown field not in the frozen contract: ${key}`);
    }
  }

  for (const field of REQUIRED_STRINGS) {
    const value = record[field];
    if (typeof value !== "string" || value === "") {
      problems.push(`${field} must be a non-empty string.`);
    }
  }
  for (const field of REQUIRED_INTEGERS) {
    const value = record[field];
    if (typeof value !== "number" || !Number.isInteger(value)) {
      problems.push(`${field} must be an integer.`);
    }
  }
  for (const field of REQUIRED_BOOLEANS) {
    if (typeof record[field] !== "boolean") problems.push(`${field} must be a boolean.`);
  }
  for (const field of ["live_ready", "live_armed"] as const) {
    const value = record[field];
    if (value !== null && typeof value !== "boolean") {
      problems.push(`${field} must be a boolean or null.`);
    }
  }

  const version = record.contract_version;
  if (typeof version === "string" && version !== LIVE_CONTRACT_VERSION) {
    problems.push(
      `Contract version ${version} is not the frozen ${LIVE_CONTRACT_VERSION}. ` +
        "Integration must stop rather than adapt.",
    );
  }

  const fatal = problems.filter((problem) => !problem.startsWith("Unknown field"));
  if (fatal.length > 0) return { summary: null, problems };
  return { summary: record as unknown as LiveAccountingSummary, problems };
}

/** `accounting_status`, narrowed, or `null` when the backend sent an unknown one. */
export function accountingStatusOf(summary: LiveAccountingSummary): AccountingStatus | null {
  const status = summary.accounting_status as AccountingStatus;
  return ACCOUNTING_STATUSES.includes(status) ? status : null;
}

/**
 * Whether the HWM, reserve, bucket and preview may be shown as current.
 *
 * `false` under every suppressing status **and** under an unrecognized one: a
 * status this build does not know is not a status it may treat as clean.
 * `STALE` returns true, and the caller badges it stale — the contract populates
 * the figures in that state and asks for a badge, not a blank.
 */
export function derivedFiguresTrustworthy(summary: LiveAccountingSummary): boolean {
  const status = accountingStatusOf(summary);
  if (status === null) return false;
  return !SUPPRESSING_STATUSES.includes(status);
}

/** Whether the payload is scoped to the account the dashboard expects. */
export function fingerprintMatches(
  summary: LiveAccountingSummary,
  expected: string | null | undefined,
): boolean | null {
  if (!expected) return null;
  return summary.account_fingerprint === expected;
}

/** The first 8 characters of the fingerprint. Never the account number. */
export function shortFingerprint(fingerprint: string | null | undefined): string | null {
  if (!fingerprint) return null;
  return fingerprint.slice(0, 8);
}
