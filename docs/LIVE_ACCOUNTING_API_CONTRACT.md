# Live Accounting API contract — FROZEN v1.0.0

Read-only. `GET /api/live-accounting/summary`.

`live_accounting_contract.json` at the repository root is the machine-readable
form and is the authority. This document explains it. When the two disagree the
JSON is right and this document is stale — but they cannot disagree for long,
because `tests/test_live_accounting_contract.py` asserts the implementation
against the JSON, field by field.

**Frozen against base SHA `fd6cae4a2ef1f92ec5e6133774a12f847c139e41`.**

Prompt 3 may build against this document and that JSON file. If implementation
work later needs a field to change, the change stops and is explained first; it
is not made silently.

---

## The rule the whole contract exists to enforce

> Money moving in is not profit. Money moving out is not loss.

Everything below is machinery for that one sentence.

---

## Two words that are not the same word

**EDA-1 Cash Reserve** is a *trading* policy. It holds back 10% of equity at the
target gross and a further 5% to the hard cap, it lives in
`autotrader.equity.allocation`, and it changes how large a position may be.

**Profit Reserve** is an *accounting* earmark. It is a notional share of newly
created high-water-mark profit set aside against a possible future withdrawal.
It moves no money, segregates no broker cash, and changes no position size.

They share no field name, no table and no module. Every Profit Reserve field in
this contract is prefixed `profit_reserve_` or is named `withdrawal_bucket_*`.
No field here is an input to any sizing, risk or trading decision, and the suite
asserts that direction of dependency.

---

## Canonical naming

One name per concept.

| Concept | Canonical name | Forbidden aliases |
|---|---|---|
| Broker equity net of external flows | **`flow_adjusted_equity`** | `adjusted_equity`, `performance_equity`, `normalized_equity` |
| Authoritative completed-day peak | **`adjusted_equity_hwm`** | `hwm`, `peak_equity`, `high_water` |
| Notional earmark of new HWM profit | **`profit_reserve_accrued`** | `reserve`, `cash_reserve`, `reserved_cash` |
| Virtual withdrawal ledger balance | **`withdrawal_bucket_balance`** | `available_to_withdraw`, `withdrawable` |

The middle column is what the backend emits, what the API serves, and what the
frontend must display. A second name for one of these numbers is a defect, not
a convenience.

---

## Representation

- **Money** is an exact decimal **string**: `"50.00"`, `"-1.25"`. Never a float,
  never exponent notation. Parse with an exact decimal type, not `Number`.
- **Ratios** (`time_weighted_return`, `current_drawdown_from_adjusted_hwm`,
  `profit_reserve_rate`) are also decimal strings. `"0.06"` is +6%.
- **Timestamps** are ISO 8601 with an explicit UTC offset.
- **`null` means unknown, and must be displayed as unknown.** It never means
  zero. A dash or "—" is correct; `$0.00` is a lie.

---

## The formulas, stated once

    net_external_flows            = confirmed_deposits - confirmed_withdrawals
    flow_adjusted_equity          = current_broker_equity - net_external_flows
    trading_pnl_since_inception   = flow_adjusted_equity - baseline_equity
    current_drawdown_from_adjusted_hwm
                                  = flow_adjusted_equity / adjusted_equity_hwm - 1
    new_hwm_profit                = max(0, flow_adjusted_equity - prior_reserve_hwm)
    profit_reserve_accrual        = profit_reserve_rate * new_hwm_profit

Worked, in the funding pattern this account is actually in:

| Event | Broker equity | Net flows | `flow_adjusted_equity` | Trading P&L |
|---|---|---|---|---|
| inception | $50 | $0 | $50 | $0 |
| +$50 deposit | $100 | +$50 | **$50** | **$0** |
| +$3 trading | $103 | +$50 | **$53** | **+$3** |
| −$10 withdrawal | $93 | +$40 | **$53** | **+$3** |

The deposit produces no return. The withdrawal produces no loss. That row of
three identical `+$3` figures is the contract.

---

## Inception

`accounting_inception_at` is the instant the baseline was frozen, and
`baseline_equity` is the broker equity at that instant.

**The opening equity is capital, not profit, and not a deposit.** The broker
activity that delivered it is recorded in the flow ledger — it is real history
and is not discarded — but it is classified `PRE_INCEPTION` and contributes
nothing to `confirmed_deposits`. Counting it would make the account look as
though it had doubled on day one.

The boundary is exclusive on the later side: a flow counts as "since inception"
only if its settle time is **strictly after** `accounting_inception_at`.

---

## Time-weighted return

`twr_convention` is always `CONSERVATIVE_MIN`, and it is the one piece of this
contract worth reading twice.

For a sub-period running from equity `B` to equity `E` containing net confirmed
external flow `F`, the two standard endpoint assumptions are

    flow at end:    (E - F) / B     - 1
    flow at start:   E / (B + F)    - 1

and the sub-period return is the **lower of the two**. Sub-period returns are
then chained.

Why the lower one. A deposit flatters the flow-at-end limb: money that arrived
at noon and rode an afternoon rally gets credited to the manager as though it
had been there all along. A withdrawal flatters the flow-at-start limb, in the
mirror image. Taking the minimum means neither kind of flow can be used to make
the strategy look better than it was.

Three properties this gives, all tested:

1. **With no flow, `F = 0`**, both limbs collapse to `E / B - 1`. The ordinary
   case is not distorted at all.
2. **With zero market movement, `E = B + F`**, both limbs are exactly zero. A
   deposit alone moves TWR by nothing; a withdrawal alone moves it by nothing.
3. It is a **bound**, not an estimate, and it is honest about being one:
   `twr_bounded_subperiods` reports how many sub-periods were bounded rather
   than measured. Zero means the figure is exact.

The bound tightens automatically as checkpoints get denser, and vanishes when a
flow lands on a checkpoint boundary.

---

## High-water mark, and why intraday cannot touch it

`adjusted_equity_hwm` is the maximum `flow_adjusted_equity` over **completed-day
checkpoints only** (`DAILY_CLOSE`), floored at `baseline_equity`.

A deposit cannot raise it, because it is a maximum over a quantity from which
flows have already been subtracted. That is a structural property, not a check.

Intraday reads are still recorded, and the highest one is published as
`observed_intraday_peak` — clearly labelled informational. It confers nothing. A
spike at 11:04 that is gone by the close must never create a permanent
withdrawal entitlement, so no reserve, bucket or preview figure may be derived
from it.

---

## Profit Reserve and the withdrawal bucket

Policy identity `PROFIT_RESERVE_OBSERVE_V1`, rate **0.20**.

The rate is a **provisional first-month observation figure**, not a permanent
investment decision. Prompt 4 revisits it after roughly a month of live
evidence.

When a new authoritative HWM is set, `profit_reserve_rate x new_hwm_profit`
accrues to the bucket. If the adjusted HWM moves $50 → $55, new HWM profit is $5
and the accrual is $1. The other $4 goes nowhere and is not marked. **Every
dollar stays in the broker account.** The bucket is a line in a ledger.

Recovering to a previous HWM accrues nothing — only growth *above* the prior HWM
is new profit. Drawdown accrues nothing.

When a real withdrawal settles, it is allocated against the bucket. A withdrawal
larger than the bucket takes the bucket to zero — never negative — and the
excess is recorded in `unreserved_withdrawal_total`, which means capital rather
than harvested profit left the account. It is flagged, not absorbed.

**Accrual is continuous; harvest is monthly.** `harvest_eligible` turns true only
at a completed monthly checkpoint. This system produces no daily transfer
recommendation.

---

## The withdrawal preview, and the drawdown rule

    withdrawal_preview = withdrawal_bucket_balance
                         when harvest_eligible
                          and flow_adjusted_equity >= adjusted_equity_hwm
                       = 0 otherwise

Below the authoritative HWM the preview is **zero**, even when the bucket holds
a balance. The bucket stays recorded; availability returns when the account is
back at its high-water mark and a harvest checkpoint occurs. Nobody should
harvest out of a drawdown, and the number on the screen should not suggest it.

The preview is informational. It is **not** a guaranteed broker-transferable
amount, and it makes no claim that the broker would settle it — see the
withdrawable-cash limitation below.

---

## OBSERVE_ONLY

For the first month, unconditionally:

| | |
|---|---|
| `withdrawal_mode` | `OBSERVE_ONLY` |
| `withdrawal_authorized` | `false` |
| `authorized_withdrawal_amount` | `"0"` |
| `automatic_transfer_enabled` | `false` |

These are not computed values that could turn true. There is no code path in
this package that sets any of them otherwise.

Underneath the policy sits a stronger fact: **this broker's Trading API exposes
no transfer endpoint at all.** Probed read-only, `/transfers` and `/withdrawals`
both return `40410000 endpoint not found`. Money movement is a Broker-API
capability that the surface this repository can reach does not have. The package
also contains no ACH call, no wire call, no scheduler and no transfer client, and
the suite asserts that no such name is reachable from it.

**No write route exists.** `ALLOWED_METHODS` is `{GET, HEAD}` and is asserted
against the assembled route table, so adding a POST fails the suite rather than
shipping. There is no withdraw button backend to build against.

---

## Withdrawable cash — the declared limitation

`broker_withdrawable_cash` is **always `null`**, with
`broker_withdrawable_cash_status = "NOT_EXPOSED_BY_BROKER"`.

The account payload carries `cash`, `buying_power`, `effective_buying_power`,
`regt_buying_power`, `non_marginable_buying_power` and `transfers_blocked`, and
nothing that means "settled cash you may withdraw today". None of those is that
number, and this system will not pretend one of them is.

This matters more here than it would on a margin account. The live account is a
**cash account** (`multiplier: 1`), so proceeds from a sale are unsettled for
T+1. `cash` includes unsettled proceeds. Displaying `cash` as withdrawable would
invite an operator to request money the broker would refuse to send.

Frontend requirement: display this as **"not verified"**. Never substitute
another field.

---

## `accounting_status` — and what the frontend must do with it

| Status | Meaning | Derived money fields |
|---|---|---|
| `NOT_INITIALIZED` | inception not yet frozen | `null` |
| `CLEAN` | trustworthy | populated |
| `STALE` | last checkpoint is older than the staleness horizon | populated, badged stale |
| `UNKNOWN_EXTERNAL_FLOW` | an activity could not be classified | **suppressed to `null`** |
| `REBUILD_REQUIRED` | a backdated flow needs a replay | **suppressed to `null`** |
| `BROKER_UNAVAILABLE` | the account could not be read | **suppressed to `null`** |
| `ACCOUNT_MISMATCH` | fingerprint is not the pinned one | **suppressed to `null`** |

Suppression is the point. When accounting cannot be trusted, the HWM, the
reserve, the bucket and the preview are **not fabricated** — they come back
`null` and `accounting_status_detail` says why. "Unknown" on screen is always
better than a precise wrong figure.

The staleness horizon is **900 seconds**. Beyond it `data_freshness` is `STALE`.

---

## Account scoping

Every field is scoped to `account_fingerprint`. A payload whose fingerprint is
not the pinned live account must be discarded rather than displayed, and
balances are never combined across accounts. Wrong account fails closed as
`ACCOUNT_MISMATCH`.

---

## `live_ready` and `live_armed`

Both are **passed through** from the Prompt-1 live safety state. This package
never computes, sets, clears or influences either one; they are here so the
dashboard can show them beside the accounting figures without a second source of
truth.

`null` means the safety state could not be read. Display that as **unknown**,
never as `false` — an unreadable arm state is not a disarmed one.
