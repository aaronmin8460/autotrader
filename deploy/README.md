# `deploy/`

Version-controlled deployment artifacts for running AutoTrader on a VPS.

Nothing here has been run against a server, and nothing here installs itself.
The operator runbook is [`docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md); this
file is the index.

```
deploy/
  systemd/    unit templates -> /etc/systemd/system
  env/        environment file templates -> /etc/autotrader
  bin/        operational scripts
  caddy/      optional reverse proxy, not installed by anything
```

## The one thing to know

**Deploying does not enable trading.** `autotrader-deploy` ships code and
leaves the runtimes observing; `autotrader-enable-paper-trading` authorizes
order submission. They are separate programs and there is no flag on the first
that reaches the second. See
[Deploy is not activation](../docs/DEPLOYMENT.md#deploy-is-not-activation).

## Scripts

| Script | Writes | Trades |
|---|---|---|
| `autotrader-deploy` | `/opt/autotrader`, units, config if absent | never |
| `autotrader-enable-paper-trading` | `autotrader.trading.env` only | authorizes it |
| `autotrader-emergency-stop` | nothing | stops it; places no orders |
| `autotrader-rollback` | `/opt/autotrader`, a backup | never |
| `autotrader-backup` | `backups/` only | never |
| `autotrader-healthcheck` | **nothing** | never |

Every script takes `--dry-run` except `autotrader-healthcheck`, which changes
nothing by construction, and `autotrader-backup`, whose only effect is a new
file.

## Units

| Unit | Restart | Notes |
|---|---|---|
| `autotrader-crypto.service` | `on-failure`, never on exit 2 | 24/7 |
| `autotrader-equity.service` | `on-failure`, never on exit 2 | session logic is the app's, not a cron's |
| `autotrader-dashboard-api.service` | `always` | `127.0.0.1:8000`, read-only |
| `autotrader-dashboard-web.service` | `always` | `127.0.0.1:3000` |
| `autotrader-backup.timer` | — | daily |

`RestartPreventExitStatus=2` on the two runtimes is the load-bearing line: exit
2 means a submission outcome is `UNKNOWN` and an order may exist at the broker,
so systemd must not turn a deliberate safety halt into a retry loop. See
[Restart policy](../docs/DEPLOYMENT.md#restart-policy).

## Tests

`tests/test_deployment_artifacts.py` is a static audit of this directory: it
parses every unit, and asserts the safety properties these files are supposed
to have — no live flags, no secret literals, loopback binding, activation
separated from deploy, persistent state never destroyed. It runs offline and
touches no server.

```bash
pytest -q tests/test_deployment_artifacts.py
```
