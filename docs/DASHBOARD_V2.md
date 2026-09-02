# Dashboard V2 — deployment and operation

Read-only. Nothing described here can place, cancel or replace an order, change
a limit, start or stop a runtime, or edit a store. This document is the
operator's map of the five API processes, the one frontend, and the deploy path
that keeps every trading and observing venv untouched.

## Processes

| Port | Unit | Identity | Reads | Serves |
|---|---|---|---|---|
| 8000 | `autotrader-dashboard-api` (packaged, pinned crypto tree) | `autotrader` | crypto store, broker account | `/api/dashboard/*` |
| 8001 | `autotrader-equity-shadow-api` | `atshadow` | shadow store | `/api/equity-shadow/*` |
| 8002 | `autotrader-equity-paper-api` + drop-in `10-dashboard-venv.conf` | `ateqpaper` | paper store, crypto store (ro), shadow store (ro), `systemctl show` | `/api/equity-paper/*` incl. `/services`, `/policy`, `/account-orders` |
| 8003 | `autotrader-equity-a1b-shadow-api` | `ata1bshadow` | A1-B store | `/api/equity-a1b-shadow/*` |
| 8004 | `autotrader-market-charts-api` | `ateqpaper` (paper market-data credential, no activation file) | provider bars only, no store | `/api/market-charts/*` |
| 3000 | `autotrader-dashboard-web` + drop-ins `20-equity-paper.conf`, `30-dashboard-v2.conf` | `ateqpaper` | — | the Next.js app; rewrites the five prefixes to loopback |

Caddy publishes `:3000` only, behind `basic_auth`, GET/HEAD only, with
`Cache-Control: no-store` on all five API prefixes and the measured CSP.

## The dashboard tree and venv

`/opt/autotrader-dashboard/app` is a checkout of the dashboard branch and
`/opt/autotrader-dashboard/venv` is a virtualenv with `.[dashboard]`
installed from it. Both are root-owned and world-readable (the code is public;
the venv holds no secret). `:8002`, `:8003` and `:8004` run from this venv.

Why a separate venv: `/opt/autotrader-equity-paper/venv` is shared by the
paper API and the **live** paper trading runtime, `/opt/autotrader/venv` by
the crypto runtime, and the observers each have their own. A `pip install`
uninstalls before it installs; for those seconds no `autotrader` package
exists under a running process. The dashboard venv is shared by nothing that
trades or observes, so redeploying the dashboard can never do that.

The frontend keeps building in `/opt/autotrader-equity-paper/app/dashboard/frontend`
(the proven path), because its `node_modules` lives there and `npm` on this
host is known fragile. The paper tree is reset to the dashboard branch — a
descendant of the paper runtime's own commit, touching only dashboard,
deploy, test and doc files — which changes nothing the running paper runtime
reads: its package is a copy install, its policy is code, its store path is
absolute.

## Deploying a dashboard change

1. Push the branch. On the VPS, as root:
2. `git -C /opt/autotrader-dashboard/app fetch origin && git -C /opt/autotrader-dashboard/app reset --hard origin/<branch>`
3. `/opt/autotrader-dashboard/venv/bin/pip install --no-deps --force-reinstall /opt/autotrader-dashboard/app` — the dashboard venv only; never the paper, crypto or observer venvs.
4. `git -C /opt/autotrader-equity-paper/app fetch origin && git -C /opt/autotrader-equity-paper/app reset --hard origin/<branch>`; then in `dashboard/frontend`: `./node_modules/.bin/next build` and `chown -R ateqpaper:ateqpaper .next`.
5. Install any changed unit or drop-in under `deploy/systemd/` to `/etc/systemd/system/`, the Caddyfile to `/etc/caddy/Caddyfile` (`caddy validate` first), then `systemctl daemon-reload`.
6. Restart **only** dashboard units: `autotrader-equity-paper-api`, `autotrader-equity-a1b-shadow-api`, `autotrader-market-charts-api`, `autotrader-dashboard-web`; `systemctl reload caddy`.
7. Verify `MainPID`/`NRestarts` of `autotrader-crypto`, `autotrader-equity-paper`, `autotrader-equity-shadow`, `autotrader-equity-a1b-shadow` are unchanged and `autotrader-equity` is still masked.

Never `pip install` into a venv a runtime shares. Never add a Caddy route to
an API port. Never load an activation file into a dashboard unit.

## Rollback

Delete the two new units and the two drop-ins, `daemon-reload`, reset the
paper tree to the previous SHA, rebuild the frontend, restart the dashboard
units, reinstall the previous Caddyfile. The dashboard tree can simply be
reset to the previous SHA and the venv reinstalled from it. No trading unit
is involved in either direction.

## What each page is

- **Operations** — the whole broker account: equity, cash, daily P&L against
  the stored UTC baseline, total exposure against the deployed policy's target
  and hard cap, every position with its weight and a price sparkline, recent
  orders merged from both order stores and labelled by store, allocation and
  unrealized P&L per position, the five units' live state, risk limits
  sourced from the paper API's policy panel, account safety, the crypto
  runtime's trail.
- **Equity Paper** — EDA-1 U10: the deployed policy's figures as the runtime
  announced them; target vs actual per symbol from the runtime's own recorded
  decisions and the broker's own positions; regime; paper orders; safety.
- **Shadows** — every observation-only strategy as a card, a comparison table
  that refuses to print a performance figure as a conclusion before each
  observer's sample threshold, and the A1-B universe with a chart on selection.

Every target on screen is a recorded decision or a policy figure read from the
running process; every limit is the policy's; every price is the broker's or
the provider's. Where none exists the page prints N/A.
