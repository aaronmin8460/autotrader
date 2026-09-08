# LIVE FIRST DAY — PRODUCTION RUNBOOK

Real money. Deployment, readiness, and a running process are not authorization
to trade. Every check below is a stop point. The deployed first-month stack is
expected to be **READY and DISARMED**, with zero Live broker mutations.

## Fixed identities

| Item | Expected |
|---|---|
| Account fingerprint | `a6bbf9c116a5679c58719d82d7e4b3e2` |
| Safe display | fingerprint `a6bbf9c1…`, account last four `8990` |
| Policy | `LIVE_VALIDATION_100` |
| Policy hash | `ecf003eb9a86814f2583c4e981bf39365e942e0e4dcc683c6c742fb3c00292d5` |
| Runtime | `/opt/autotrader-equity-live/venv/bin/autotrader` |
| Operational DB | `/var/lib/autotrader-equity-live/equity-live.db` |
| Accounting DB | `/var/lib/autotrader-equity-live/live-accounting.db` |
| Accounting API | `127.0.0.1:8005` |
| Safety API | `127.0.0.1:8006` |

At $50 verified broker equity, target/hard/absolute/per-symbol ceilings are
$45.00/$47.50/$50.00/$5.50. At $100 or more they are capped at
$90.00/$95.00/$100.00/$11.00. The API must quote these from
`effective_ceilings`; never calculate them by hand to resolve a discrepancy.

## Exact production status sequence

### 1. Services and the absent environment ARM gate

```bash
sudo systemctl --no-pager --full status autotrader-equity-live.service autotrader-equity-live-reconcile.timer autotrader-live-accounting-api.service autotrader-live-safety-api.service autotrader-live-accounting-sync.timer autotrader-live-daily-close.timer
sudo test ! -e /etc/autotrader/autotrader-equity-live.arm.env
```

Stop unless the three services and three timers are active and the second command
exits zero. During deployment and verification the ARM file must not exist.

`autotrader-equity-live-reconcile.timer` is a broker-read-only, quarter-hourly
convergence pass for the Live operational store. It loads no ARM gate and can
therefore repair a durable local snapshot while Live is disarmed; it never
submits, cancels, or replaces an order.

### 2. Authoritative Live status

```bash
sudo -u ateqlive sh -c 'set -a; . /etc/autotrader/autotrader-equity-live.env; . /etc/autotrader/autotrader-equity-live.secrets.env; [ ! -r /etc/autotrader/autotrader-equity-live.arm.env ] || . /etc/autotrader/autotrader-equity-live.arm.env; set +a; exec /opt/autotrader-equity-live/venv/bin/autotrader live status'
```

Stop unless all of these are true: `live_ready=true`, effective arm state
`DISARMED`, durable state `DISARMED`, environment gate false, account pin
`PINNED`, account `ACTIVE`, account type `CASH`, multiplier `1`, shorting false,
reconciliation `CLEAN` (or a reviewed `REPAIRED`), and every ceiling matches the
current broker equity. `UNKNOWN`, a missing figure, or a mismatch is a stop.

### 3. Reconciliation check

```bash
sudo -u ateqlive sh -c 'set -a; . /etc/autotrader/autotrader-equity-live.env; . /etc/autotrader/autotrader-equity-live.secrets.env; set +a; exec /opt/autotrader-equity-live/venv/bin/autotrader live reconcile --db /var/lib/autotrader-equity-live/equity-live.db'
```

Stop unless `safe_to_trade=true`, status is `CLEAN` or explicitly reviewed
`REPAIRED`, and issues/unresolved are zero. This command reads the broker and
may repair only the dedicated local operational record; it cannot mutate the
broker.

### 4. Accounting status and frozen-contract contact

```bash
curl --fail --silent --show-error http://127.0.0.1:8005/api/live-accounting/summary
systemctl --no-pager --full status autotrader-live-accounting-sync.timer autotrader-live-daily-close.timer
```

Stop unless the payload passes the frontend `parseSummary` validator,
`accounting_status=CLEAN`, its account fingerprint matches, freshness is within
the contract horizon, and withdrawal mode remains `OBSERVE_ONLY` with
withdrawal authorization and automatic transfer both false. Broker
withdrawable cash remains `NOT_EXPOSED_BY_BROKER`; do not substitute cash,
equity, or buying power.

### 5. Dashboard and deployed identity

```bash
curl --fail --silent --show-error http://127.0.0.1:8006/api/live-safety/summary
git -C /opt/autotrader-equity-live/app rev-parse HEAD
```

Open the authenticated `/live` page and verify `LIVE · REAL MONEY`, `READY`,
`DISARMED`, the exact deployed SHA, current account figures, accounting figures,
and freshness. Also smoke `/`, `/portfolio`, `/orders`, `/risk`, and `/system`.
Unknown values must render as dashes/explanations, never fake zeros, `NaN`,
`undefined`, or `null`.

## Future ARM — not part of deployment

Do not run this sequence merely because all checks pass. It belongs to a later,
separately authorized first-Live session. Replace the reason with a dated,
specific operator authorization before executing it.

First arm the durable row while the independent environment gate is still
closed:

```bash
sudo -u ateqlive sh -c 'set -a; . /etc/autotrader/autotrader-equity-live.env; . /etc/autotrader/autotrader-equity-live.secrets.env; set +a; exec /opt/autotrader-equity-live/venv/bin/autotrader live arm --db /var/lib/autotrader-equity-live/equity-live.db --reason "FIRST LIVE SESSION APPROVED YYYY-MM-DD" --confirm ARM-LIVE-REAL-MONEY'
```

Then, and only in that authorized session, open the environment gate and reload
the process:

```bash
sudo install -o root -g ateqlive -m 0640 /opt/autotrader-equity-live/app/deploy/env/autotrader-equity-live.arm.env.example /etc/autotrader/autotrader-equity-live.arm.env
sudo systemctl restart autotrader-equity-live.service autotrader-live-accounting-api.service autotrader-live-safety-api.service
```

Repeat the full status sequence. Both gates must be visible. The runtime still
requires account pinning, startup reconciliation, broker calendar/clock, cash
settlement protection, deposit-day guard, risk approval, durable intent, and
duplicate preflight before any one order can reach the broker.

## Emergency DISARM — fastest action

The durable row is read immediately before each mutation, so write it first:

```bash
sudo -u ateqlive /opt/autotrader-equity-live/venv/bin/autotrader live disarm --db /var/lib/autotrader-equity-live/equity-live.db --reason "EMERGENCY OPERATOR DISARM"
```

Then remove the environment authorization and restart so the process-level
gate is also closed:

```bash
sudo rm -f /etc/autotrader/autotrader-equity-live.arm.env
sudo systemctl restart autotrader-equity-live.service autotrader-live-accounting-api.service autotrader-live-safety-api.service
```

Removing the file does not change an already-running process's environment;
the restart is required for that half. The durable DISARM takes effect without
a restart. Neither action cancels, replaces, liquidates, transfers, or
withdraws. Existing positions remain at the broker and require separate manual
operator judgment.

## Immediate stop conditions

Stop and DISARM on a wrong fingerprint, non-cash or leveraged account, short
position, unexplained order/position, unresolved reconciliation, unknown cash
flow, stale or rejected accounting payload, risk mismatch, policy/SHA mismatch,
broker ambiguity, duplicate order, extended-hours anomaly, deposit or
withdrawal settling during the session, or any unexpected broker mutation.

The authorized sequence is always:

```text
STATUS → READY=YES → ARMED=NO → PIN=PASS → RECONCILIATION=CLEAN
→ ACCOUNTING=CLEAN → BALANCE/CAPS VERIFIED
→ later, separately authorized session only: ARM
```
