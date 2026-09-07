# LIVE FIRST DAY — RUNBOOK

Real money. Read the whole page before starting. Every step is a **stop point**:
if a check does not read as written, stop and do not continue.

The system is **DISARMED** and has placed **zero** orders. Nothing below happens
by itself.

---

## Facts to check against

| | |
|---|---|
| Account fingerprint | `a6bbf9c116a5679c58719d82d7e4b3e2` |
| Account last four | `8990` |
| Policy | `LIVE_VALIDATION_100` |
| Policy hash | `ecf003eb9a86814f2583c4e981bf39365e942e0e4dcc683c6c742fb3c00292d5` |
| Code SHA | `30d43f9` (branch `ops/live-100-readiness`) |
| Expected equity | ~$50 settled (or ~$100 after the second deposit) |
| Ceilings at $50 | target $45.00 · hard $47.50 · bound $50.00 · per symbol $5.50 |
| Ceilings at $100 | target $90.00 · hard $95.00 · bound $100.00 · per symbol $11.00 |

---

## Before the open

### 1 — Verify funding, positions and orders

```bash
cd /Volumes/AUTOTRADER_QA/worktrees/live-100-readiness && set -a && . ~/.config/autotrader/live.secrets.env && set +a && PYTHONPATH=src .venv/bin/python /Volumes/AUTOTRADER_QA/tmp/live_readonly_probe.py
```

**Stop unless:** fingerprint is `a6bbf9c1…`, `status ACTIVE`, `trading_blocked
false`, `account_blocked false`, **0 positions**, **0 open orders**, and equity
is the figure you expect. Do **not** count a deposit the broker has not settled.

### 2 — Verify code and policy identity

```bash
git -C /Volumes/AUTOTRADER_QA/worktrees/live-100-readiness rev-parse --short HEAD
```

**Stop unless** it is the SHA above, or a SHA you deliberately deployed.

### 3 — Verify session status

Market open 09:30 ET. The broker's own clock is the authority and the runtime
reads it again immediately before every order. Do not start on a half day
without checking the close time.

### 4 — Run the DISARMED startup and read the ceilings

```bash
cd /Volumes/AUTOTRADER_QA/worktrees/live-100-readiness && set -a && . ~/.config/autotrader/live.secrets.env && set +a && export AUTOTRADER_LIVE_ACCOUNT_FINGERPRINT=a6bbf9c116a5679c58719d82d7e4b3e2 && PYTHONPATH=src .venv/bin/python /Volumes/AUTOTRADER_QA/tmp/live_soak.py 1
```

**Stop unless:** `result: VERIFIED`, `reconciliation_status: CLEAN`,
`broker_mutation_attempts: 0`, `gateway: REFUSED_DISARMED`, and the ceilings
match the equity you verified in step 1.

---

## Arming

### 5 — Arm the environment gate

Create `/etc/autotrader/autotrader-equity-live.arm.env` from the template, or
export `AUTOTRADER_LIVE_ARMED=true` for a foreground run.

### 6 — Arm the durable row

Requires the exact token `ARM-LIVE-REAL-MONEY` and a recorded reason. **This is
the moment real money becomes possible.**

Both gates are now open. Neither alone would have been enough.

### 7 — Start the runtime

Watch the startup banner. **Stop and disarm unless** it reports the right
account fingerprint, `LIVE_VALIDATION_100`, the policy hash above, and the
ceilings from step 4.

---

## The first order is an operational test, not a trade

### 8 — Watch the first decision cycle

Most cycles submit nothing. That is correct: EDA-1 holds a target, and a target
that has not moved past the deadband is silence.

### 9 — When the first order goes out, verify every one of these

| Check | Expect |
|---|---|
| Symbol | one of the ten |
| Side | **BUY** or **SELL** — never anything else |
| Quantity | fractional, and **≤ the slot** ($4.50 at $50) |
| Notional | ≤ per-symbol ceiling ($5.50 at $50) |
| Target allocation | matches the logged `target_weight` × equity |
| Reference price | close to the current market |
| `client_order_id` | starts `autotrader-` |
| Broker order ID | present |
| Status | `accepted` / `new` / `filled` |
| Fill quantity | **may be 0 — accepted is not filled** |
| Fill price | close to the reference price |
| Remaining quantity | consistent with the fill |
| Local ledger entry | one `order_intents` row, status `SUBMITTED` |
| Position after fill | matches the broker exactly |
| Cash after fill | reduced by roughly the notional |
| **Gross after fill** | **≤ hard ceiling** ($47.50 at $50) |

### 10 — Do not scale on success

A first order that works proves connectivity, not edge. Nothing about the
ceilings changes today, this week, or on the strength of a profitable day.

---

## STOP CONDITIONS — disarm immediately

Disarm on **any** of these. Do not wait to see whether the next cycle fixes it.

- wrong account fingerprint
- wrong symbol, wrong side, or wrong quantity
- gross above the hard ceiling
- any leverage, any short position
- a duplicate order
- an unexplained position mismatch
- reconciliation failure
- a broker timeout with an ambiguous order state
- a local ledger discrepancy
- an unexpected extended-hours order
- a strategy or policy SHA mismatch
- a risk-policy mismatch
- stale data or a clock/calendar anomaly
- **a cash deposit or withdrawal settling mid-session** — the daily-loss halt is
  measuring the transfer, not the trading, and the system blocks new entries for
  that day by design

### How to disarm

Delete the arm file, **or** write the durable DISARMED row. Either alone stops
submission. Both are instant, neither cancels or flattens anything, and neither
deletes any state.

Disarming stops the system **adding**. Existing positions stay exactly as they
are, and the runtime keeps observing so you can see what is happening.

---

## Two things this system will not do for you

1. **It never cancels or liquidates.** There is no cancel, replace or close call
   anywhere in the repository. If you need a position closed today and the
   system is disarmed, close it yourself at the broker.
2. **It never moves money.** No transfer, ACH or withdrawal call exists. Profit
   reserve and withdrawal accounting are Prompt 2.
