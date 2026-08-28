# Deployment

How the AutoTrader system runs on a VPS: what goes where, what starts it, what
stops it, and what the difference is between deploying it and letting it trade.

Everything here targets **one Alpaca paper account** and **one SQLite
database**, with four services against them: the 24/7 crypto runtime, the
market-session equity runtime, the dashboard API, and the dashboard frontend.

A host does not have to run all four. The first posture this supports is
**crypto-only**: the 24/7 runtime active, the equity runtime masked so that it
cannot be started at all, and the dashboard on loopback. See "Crypto-only
posture".

## Contents

- [Deploy is not activation](#deploy-is-not-activation)
- [Filesystem layout](#filesystem-layout)
- [Secrets](#secrets)
- [The services](#the-services)
- [Restart policy](#restart-policy)
- [SQLite](#sqlite)
- [Networking](#networking)
- [Publishing the dashboard](#publishing-the-dashboard)
- [Runbook](#runbook)
- [What is still pinned to Combined Integration](#what-is-still-pinned-to-combined-integration)
- [Crypto-only posture](#crypto-only-posture)

---

## Deploy is not activation

This is the property the rest of the design serves, so it goes first.

`autotrader-deploy` ships code. `autotrader-enable-paper-trading` authorizes
order submission. They are different programs, and there is no flag on the
first that reaches the second.

The reason is not ceremony. A single command that both installed a change and
authorized it to trade would make "ship the fix" and "start trading with the
fix" one keystroke, and after the fact there would be no way to tell which one
someone meant. Splitting them means the trading decision has its own moment,
its own typed confirmation, and its own line in the shell history.

The mechanism is one file. `/etc/autotrader/autotrader.trading.env` does not
exist on a freshly deployed host. The runtime units load it *last* and mark it
optional:

```
EnvironmentFile=/etc/autotrader/autotrader.env          # safe defaults
EnvironmentFile=-/etc/autotrader/autotrader.secrets.env
EnvironmentFile=-/etc/autotrader/autotrader.trading.env # activation, absent by default
```

Absent, the safe defaults stand and the runtimes start with `--observe-only`:
they fetch bars, validate them, evaluate the strategy, record signals and
reconcile at startup, and construct no execution path at all. Submission is not
refused, it is unavailable.

Present, it overrides three variables and the runtimes start with
`--confirm-paper-runtime PAPER` and the environment gate open.

Deleting the file and restarting returns the host to observing. That round trip
is why the activation values live in that file and **not** in
`autotrader.env`: the off switch works by deleting the on switch, and it cannot
delete a `true` written somewhere else. A host with `AUTOTRADER_PAPER_TRADING_ENABLED=true`
in `autotrader.env` would look deactivated after `--disable` and keep trading.

### The third gate

Neither script can open it. Every runtime start reconciles local state against
the broker, and a start that comes back `UNRESOLVED` or `FAILED` prints
`RECONCILIATION NOT SAFE - TRADING DISABLED`, keeps observing, and submits
nothing — whatever the environment says. No file on disk reaches it.

### There is no live mode to deploy

The trading client is constructed with `paper=True` hardcoded. There is no
flag, option, or environment variable in the application that selects live
trading, and there is none in this deployment package either. A live API key in
`autotrader.secrets.env` does not enable live trading; it makes every broker
call fail authentication against the paper endpoint.

---

## Filesystem layout

Four concerns, four locations, deliberately not overlapping.

```
/opt/autotrader/
    app/                     git checkout, detached at the deployed SHA
    venv/                    Python virtualenv

/var/lib/autotrader/         PERSISTENT STATE - survives every deploy
    autotrader.db            the one operational database
    autotrader.db-wal        write-ahead log
    autotrader.db-shm        shared-memory index
    autotrader.db.runtime.lock         crypto single-instance lock
    autotrader.db.equity.runtime.lock  equity single-instance lock
    backups/                 timestamped SQLite backups
    deploy-history.log       append-only: when, which SHA, which ref

/etc/autotrader/
    autotrader.env           shared config + safe defaults   0644 root:root
    autotrader.secrets.env   Alpaca paper credentials        0640 root:autotrader
    autotrader.trading.env   ACTIVATION - absent by default  0640 root:autotrader

/usr/lib/systemd/system/     the units, as deployed
/etc/systemd/system/         operator overrides only - this is where a mask lives
```

Logs are not in this list because there are no log files: everything goes to
journald. See [Logs](#logs).

**Why the code is a git checkout.** Deploying means checking out a SHA, so
"which commit is on this box" is answerable with `git -C /opt/autotrader/app
rev-parse HEAD` rather than by trusting a directory name. It is checked out
*detached*, so the deployed tree is one named commit that no later `git pull`
elsewhere can move underneath it. It also makes "refuse to deploy over
uncommitted changes" a real check rather than a hope.

**Why state is not under `/opt`.** `/opt/autotrader/app` is replaced wholesale
on every deploy and rollback. Anything inside it is disposable by definition.
The database is the opposite of disposable, so it lives in `/var/lib`, and the
deploy script never writes there apart from appending one line to
`deploy-history.log`.

`/var/lib/autotrader` is created by systemd, not by the deploy script:
every unit declares `StateDirectory=autotrader`, which creates the directory
with the service user's ownership before `ExecStart` and leaves it alone across
restarts, redeploys and `systemctl disable`.

### The service user

```bash
useradd --system --home-dir /opt/autotrader --shell /usr/sbin/nologin autotrader
```

No login shell and no password. It owns the state directory and reads the
secrets file through group membership; it cannot write the secrets file, and it
cannot write its own code in `/opt/autotrader/app`.

---

## Secrets

Two rules, and they are the whole section.

**Credentials live in one root-owned file, and reach the processes through
`EnvironmentFile`.** Not in Git, not in a unit body, not in the frontend, not
in a dashboard JSON payload, and not on a command line where `ps` shows it to
every user on the box.

```bash
install -o root -g autotrader -m 0640 /dev/null /etc/autotrader/autotrader.secrets.env
${EDITOR:-vi} /etc/autotrader/autotrader.secrets.env
```

```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
```

Verify with `stat -c '%U:%G %a' /etc/autotrader/autotrader.secrets.env` →
`root:autotrader 640`.

**Write them with an editor.** `echo "ALPACA_API_KEY=..." >> file` puts a live
credential in root's shell history, where it outlives the session and comes
back the next time somebody presses the up arrow.

### Who gets them

| Service | Credentials | Why |
|---|---|---|
| `autotrader-crypto` | yes | submits paper orders |
| `autotrader-equity` | yes | submits paper orders |
| `autotrader-dashboard-api` | yes | reads account value and positions |
| `autotrader-dashboard-web` | **no** | renders bytes into a browser |

The frontend unit does not load `autotrader.secrets.env`, and that absence is
the security boundary rather than an oversight. The dashboard API does not load
`autotrader.trading.env` for the same reason in reverse: a process that cannot
submit an order has no business carrying the variable that authorizes one.

Never copy a developer `.env` onto the server. The repository's `.env` is
git-ignored and its layout is a local convenience, not this file.

---

## The services

| Unit | What it is | Binds |
|---|---|---|
| `autotrader-crypto.service` | 24/7 crypto runtime, BTC/USD + ETH/USD | nothing |
| `autotrader-equity.service` | equity runtime, ten-symbol universe | nothing |
| `autotrader-dashboard-api.service` | read-only FastAPI backend | `127.0.0.1:8000` |
| `autotrader-dashboard-web.service` | Next.js production server | `127.0.0.1:3000` |
| `autotrader-backup.service` + `.timer` | daily SQLite backup | — |

### The dashboard cannot affect trading, and vice versa

There is no `Requires=`, `BindsTo=`, or `PartOf=` between the dashboard units
and the trading units, in either direction. A dashboard that could stop the
runtimes by failing would be a monitoring tool that causes the outage it
reports; a frontend crash that restarted a trading process would be worse.

The frontend `Wants=` the API — starting the frontend pulls the API up, which
is what an operator means by "start the dashboard" — but does not `Require=` it,
so the page survives the API going down. That matters: the frontend keeps the
last good payload on screen and says the connection dropped, which is exactly
the context you need to work out why.

### No cron for market hours

The equity runtime is a long-running service, and there is deliberately no
timer firing at 09:30 and stopping at 16:00.

Session logic is application-owned: `equity-run` reads the broker's own
calendar, so it knows about holidays and early closes, and a cycle outside the
regular session does nothing at all — no fetch, no strategy, no checkpoint, no
order, no provider call. A cron entry would be a second, dumber copy of that
logic in a place that cannot see the calendar, and the two would disagree the
first time the NYSE closed early.

### Two runtimes, one database, no collision

Both write `/var/lib/autotrader/autotrader.db`. One Alpaca account, one
`client_order_id` space, one audit trail.

They do not fight over the single-instance lock because the lock is scoped:
the crypto runner takes `autotrader.db.runtime.lock` and the equity runner
takes `autotrader.db.equity.runtime.lock`. Each still refuses a second copy of
*itself* — which is the property that prevents duplicate trading — without
blocking the other product.

The lock is `flock`, held by the open file description, so the kernel releases
it when the process dies for any reason including `SIGKILL` and power loss. A
stale lock file cannot wedge the next start.

---

## Restart policy

The runtimes' exit codes are not interchangeable, and treating them as if they
were is how a safety halt becomes a retry loop.

| Exit | Meaning | systemd |
|---|---|---|
| 0 | clean stop, including a clean `SIGTERM` | no restart; not a failure |
| 1 | controlled refusal or fatal cycle failure — held lock, unusable configuration, a cycle that died | restart, up to 5 times in 10 minutes |
| 2 | **TRADING PAUSED** — a submission outcome is `UNKNOWN` and an order may exist at the broker | **never restarted** |

```ini
Restart=on-failure
RestartSec=30
RestartPreventExitStatus=2
StartLimitIntervalSec=600
StartLimitBurst=5
```

`RestartPreventExitStatus=2` is the important line. Exit 2 means the runtime
stopped *because* it could not establish what happened at the broker.
Restarting it would ask a process that just refused to keep trading to try
again against state nobody has reconciled. It stays down, in `failed`, until a
human runs `autotrader reconcile`.

Exit 1 is retried because some of its causes are transient — a provider outage
at 03:00 is worth surviving unattended. `StartLimitBurst` bounds that: five
failures in ten minutes and systemd stops trying and leaves the unit failed and
visible, rather than retrying an unusable configuration forever.

**A known limitation.** Click — which Typer is built on — also exits 2 on a
command-line usage error, so a malformed `ExecStart` is indistinguishable from
a trading pause at the systemd layer. The collision resolves in the safe
direction: both leave the service down and visible instead of looping. The
journal distinguishes them immediately; a trading pause prints
`TRADING PAUSED - SUBMISSION OUTCOME UNKNOWN`.

### Shutdown

`KillSignal=SIGTERM` with `TimeoutStopSec=120`. The runtimes install a handler,
finish the cycle they are in, and exit 0. The generous timeout is for a cycle
mid-submission: killing that produces exactly the `UNKNOWN` state the runtime
exists to avoid.

---

## SQLite

### One database, and it must be local disk

`/var/lib/autotrader/autotrader.db`, in WAL mode, with foreign keys enforced
and a five-second busy timeout — all set by the application when it connects.

**No NFS, no CIFS, no network filesystem, no shared block device.** SQLite's
locking depends on the filesystem implementing POSIX advisory locks correctly,
and network filesystems mostly do not. On NFS the failure is not a clean error;
it is silent corruption under concurrent access. The runtime lock is `flock`,
with the same requirement.

Do not copy the database to a second location and run something against the
copy. There is one file, and the WAL and shared-memory index beside it are part
of it.

### Nothing recreates it on startup

A runtime that opens the database applies pending schema migrations and
otherwise leaves it alone. There is no "initialize if missing" that also
truncates, and no deploy step that deletes it. The deploy script's only write
under `/var/lib/autotrader` is one appended line in `deploy-history.log`.

### Backups: why not `cp`

Because it silently produces an empty database.

The system is 24/7 and the database is in WAL mode, so at any instant the
committed state is spread across the `.db` file and the `-wal` beside it.
Copying only the `.db` gets a snapshot missing every transaction still in the
log. Copying all three files with three `cp` calls is worse: three files read
at three different instants, producing something that looks complete and is
torn.

Measured on a database with 2000 committed rows and the WAL not yet
checkpointed: the main file was 4 KB and the WAL held 49 KB. `cp` of the `.db`
alone produced a database that answered `no such table` for the only table in
it. The online backup produced all 2000 rows and `PRAGMA integrity_check` →
`ok`.

`deploy/bin/autotrader-backup` uses SQLite's online backup API, which copies
pages under a reader's locking discipline and folds in the WAL. The runtimes
keep trading throughout.

```bash
sudo systemctl enable --now autotrader-backup.timer
```

Daily, with a randomized delay so it does not stack on the 00:00 UTC bar
boundary, `Persistent=true` so a missed run happens at next boot, and the
newest seven kept.

Daily is a deliberate choice rather than an aspiration: the database is durable
intent and an audit trail, not the authority on positions. The broker is that,
and reconciliation rebuilds local rows from the account after any crash. Losing
a day of audit rows costs history, not a position.

---

## Networking

```
public        22/tcp    SSH
              80/tcp    only if the dashboard is exposed - ACME + redirect
              443/tcp   only if the dashboard is exposed

loopback      8000      dashboard API      (127.0.0.1, hardcoded in the app)
              3000      dashboard frontend (127.0.0.1, via --hostname)

nothing       trading runtimes - they listen on no port at all
              SQLite - it is a file; it has no port and must never have one
```

By default the dashboard is reachable over an SSH tunnel and from nowhere else:

```bash
ssh -N -L 3000:127.0.0.1:3000 you@your-vps
```

That is a legitimate end state, and it is the default. Putting the dashboard on
a public hostname instead is one command and its own section: see
[Publishing the dashboard](#publishing-the-dashboard).

**`--hostname 127.0.0.1` on the frontend unit is load-bearing.** `next start`
binds `0.0.0.0` by default, which on a VPS means the unauthenticated dashboard
is on the public internet the moment the service comes up — no firewall mistake
required, just the default. The API needs no equivalent flag because its entry
point hardcodes the host and takes no `--host` argument.

**No public unauthenticated dashboard.** Every route is a GET and none of them
can place an order, so the risk is disclosure rather than control — but what it
discloses is account value, positions, orders and strategy behaviour in real
time. There is no login page behind the proxy. `basic_auth` in the Caddyfile is
the entire authentication boundary.

The proxy adds one guarantee the application already makes, so that it stops
depending on the application making it: `@write not method GET HEAD` returns
405 at the edge. The dashboard is GET-only today, and a route that accidentally
accepts a POST tomorrow is not reachable from the internet on the day it is
written.

---

## Publishing the dashboard

One command, and it is independent of the other three. `autotrader-deploy`
ships code, `autotrader-enable-paper-trading` opens the submission gate,
`autotrader-emergency-stop` closes everything. This one changes **who can look
at the result** and touches no trading unit, no database, no broker credential
and no gate.

```bash
/opt/autotrader/app/deploy/bin/autotrader-publish-web --domain dash.example.com
```

It installs Caddy, installs `deploy/caddy/Caddyfile` verbatim, writes
`/etc/autotrader/autotrader.web.env`, opens 80 and 443, starts the service, and
waits until the hostname answers `401` over a certificate a real client
trusts — then prints a generated password **once**.

`--dry-run` prints the whole plan and changes nothing. `--rotate-credential`
issues a new password. `--unpublish` stops Caddy and closes both ports, leaving
the dashboard running on loopback and the configuration on disk.

### The password is printed once

Only its bcrypt hash reaches disk, in a file that is `0640 root:caddy` rather
than world-readable like `autotrader.env`. Nothing on the host can recover the
plaintext, the script does not log it, and it is hashed through stdin rather
than an argument so that it is never visible in `ps`. Lose it and
`--rotate-credential` issues another; there is no recovery path and there is
not meant to be one.

The hash does not reach the journal either. Caddy's packaged unit starts the
server with `--environ`, which prints the whole process environment at every
start — and the drop-in has just put the hash in that environment. The drop-in
therefore replaces the command with one that omits the flag. Without it,
journald keeps a copy of the dashboard's only credential, readable by every
member of `adm`, for as long as it keeps anything.

### The hostname has to resolve first

Caddy proves control of the name to Let's Encrypt by answering a request
addressed to it. The script refuses to continue if the name does not resolve,
or resolves somewhere other than this host, because the alternative is Caddy
retrying against a rate-limited ACME endpoint while the operator reads a
firewall rule looking for the mistake.

A host with no domain of its own can use the wildcard DNS resolver
`sslip.io`, which answers `<ip>.sslip.io` with `<ip>`. It is a real, publicly
resolvable name that Let's Encrypt will issue for, it costs nothing, and it
needs no registrar:

```bash
/opt/autotrader/app/deploy/bin/autotrader-publish-web --domain 203.0.113.10.sslip.io
```

The tradeoff is that the name is derived from the address, so it is guessable
by anyone scanning the range. That is exactly why `basic_auth` is not optional:
obscurity was never the boundary, and with an IP-derived hostname there is no
obscurity left to lean on.

### What stays private

Publishing is additive. The dashboard processes are not restarted, not
reconfigured, and not read — they keep listening on `127.0.0.1:3000` and
`127.0.0.1:8000` exactly as before, and the only change is that something on
the same machine now connects to 3000. The Caddy admin API, which can rewrite
the running configuration, is pinned to `127.0.0.1:2019` in the Caddyfile
rather than left at its default.

---

## Runbook

### 1. Initial install

Once per machine, as root.

```bash
useradd --system --home-dir /opt/autotrader --shell /usr/sbin/nologin autotrader
apt-get install -y git python3 python3-venv nodejs npm
install -d -o root -g root -m 0755 /opt/autotrader /etc/autotrader
git clone <repository-url> /opt/autotrader/app
chown -R root:autotrader /opt/autotrader/app
```

The checkout is owned by **root**, group `autotrader`, not by the service user.
The runtimes only ever read their own code, and the deploy script runs as root:
making the service user the owner would let a trading process rewrite the
program it is executing, and would make every root `git` command in the deploy
script trip over `detected dubious ownership`. The one exception is the
frontend build directory, which Next.js writes at runtime — see
[The services](#the-services).

`/var/lib/autotrader` is not created here; systemd creates it on first start.

### 2. Configure

```bash
install -o root -g root -m 0644 \
  /opt/autotrader/app/deploy/env/autotrader.env.example \
  /etc/autotrader/autotrader.env

install -o root -g autotrader -m 0640 /dev/null /etc/autotrader/autotrader.secrets.env
${EDITOR:-vi} /etc/autotrader/autotrader.secrets.env   # paste the paper keys
```

Do **not** create `autotrader.trading.env`. Its absence is the observe-only
posture.

### 3. Deploy a SHA

```bash
/opt/autotrader/app/deploy/bin/autotrader-deploy <sha> --require-observe-only --run-tests
```

Resolves the ref to a commit, refuses a dirty tree, refuses to run at all if
this host is already activated, checks out detached, updates the venv, runs
`npm ci` and `npm run build`, runs pytest and ruff, installs any changed unit
and reloads systemd only if one changed, and restarts only the services that
were already running.

It does not enable trading. It cannot.

### 4. Observe-only staging

The point of this step is to prove the *infrastructure* — units, paths,
permissions, network, dashboard — with the execution path switched off, before
anything is authorized to trade.

```bash
systemctl enable --now autotrader-dashboard-api autotrader-dashboard-web
systemctl enable --now autotrader-backup.timer

# one cycle, then exit - the cheapest possible first contact
sudo -u autotrader /opt/autotrader/venv/bin/autotrader crypto-run \
    --db /var/lib/autotrader/autotrader.db --once --observe-only
sudo -u autotrader /opt/autotrader/venv/bin/autotrader equity-run \
    --db /var/lib/autotrader/autotrader.db --once --observe-only

# then leave them up, still observing
systemctl enable --now autotrader-crypto autotrader-equity
```

With no activation file, the services run `--observe-only` continuously. Let
that sit for a while. It exercises the schedule, the provider calls, the
database, the checkpoints and the dashboard, and it cannot place an order.

### 5. Check services

```bash
systemctl status autotrader-crypto autotrader-equity --no-pager
systemctl status autotrader-dashboard-api autotrader-dashboard-web --no-pager
systemctl list-timers autotrader-backup.timer --no-pager
```

### 6. Logs

Everything goes to journald; there are no log files to rotate.

```bash
# follow
journalctl -u autotrader-crypto.service -f
journalctl -u autotrader-equity.service -f
journalctl -u autotrader-dashboard-api.service -f
journalctl -u autotrader-dashboard-web.service -f

# last 100 lines
journalctl -u autotrader-crypto.service -n 100 --no-pager

# since this boot
journalctl -u autotrader-crypto.service -b --no-pager

# the previous boot, when the box rebooted under you
journalctl -u autotrader-crypto.service -b -1 --no-pager

# what happened before the most recent crash: the window around the last exit
journalctl -u autotrader-crypto.service --since "1 hour ago" --no-pager

# all four at once, interleaved, which is what you want during an incident
journalctl -u autotrader-crypto -u autotrader-equity \
           -u autotrader-dashboard-api -u autotrader-dashboard-web \
           -f

# errors only
journalctl -u autotrader-crypto.service -p err --no-pager

# why did it stop?
systemctl show autotrader-crypto.service -p ExecMainStatus -p Result
```

`ExecMainStatus=2` on a trading unit means the runtime paused itself. Go to
step 12.

### 7. Verify the database and the runtimes

```bash
sudo -u autotrader /opt/autotrader/app/deploy/bin/autotrader-healthcheck
```

Read-only. Reports unit states, dashboard reachability, WAL mode, integrity,
schema version, the latest reconciliation result, per-symbol checkpoint
freshness, and any `UNKNOWN` order intent. Exits 0 healthy, 1 degraded, 2
unhealthy.

It never repairs anything, and it never calls reconciliation — a health check
that repaired what it found would be an unattended process making
trading-safety decisions on a timer.

### 8. Verify the dashboard

```bash
curl -fsS http://127.0.0.1:8000/api/dashboard/overview | head -c 400
ssh -N -L 3000:127.0.0.1:3000 you@your-vps   # then open http://127.0.0.1:3000
```

### 9. Enable paper trading — the separate step

Only after observe-only staging has been up long enough to trust, and after the
paper smoke gate.

```bash
/opt/autotrader/app/deploy/bin/autotrader-enable-paper-trading
```

Prompts for `ENABLE PAPER TRADING`, typed in full. Refuses if the credentials
file is missing or empty. Writes the activation file and restarts whichever
runtimes are running.

Then confirm what the runtimes actually decided, because the third gate is
theirs:

```bash
journalctl -u autotrader-crypto.service -n 50 --no-pager
```

A runtime printing `RECONCILIATION NOT SAFE - TRADING DISABLED` is observing
despite the activation file. That is the system working.

### 10. Pause, and restart safely

```bash
# stop new autonomous activity, keep the dashboard up
systemctl stop autotrader-crypto autotrader-equity

# back to observing without stopping anything
/opt/autotrader/app/deploy/bin/autotrader-enable-paper-trading --disable

# restart: SIGTERM, finish the cycle, exit 0, start again, reconcile at startup
systemctl restart autotrader-crypto
```

A restart is not a cheap operation to the runtime and does not need to be
treated as one: it reconciles against the broker on every start before it will
trade.

### 11. Back up

```bash
systemctl start autotrader-backup            # on demand
ls -la /var/lib/autotrader/backups
```

### 12. When a runtime pauses itself (exit 2)

```bash
journalctl -u autotrader-crypto.service -n 100 --no-pager

# read the broker, write nothing
sudo -u autotrader /opt/autotrader/venv/bin/autotrader reconcile \
    --db /var/lib/autotrader/autotrader.db --dry-run
```

`CLEAN`/`REPAIRED` → safe to start again. `UNRESOLVED` → an order may exist
that local state cannot account for; settle it against the account by hand
before any runtime starts. Drop `--dry-run` to let reconciliation write its
repairs.

```bash
systemctl start autotrader-crypto
```

### 13. Rollback

```bash
/opt/autotrader/app/deploy/bin/autotrader-rollback              # proposes the previous SHA
/opt/autotrader/app/deploy/bin/autotrader-rollback <sha> --yes
```

Checks schema compatibility **before stopping anything**, takes a backup, stops
the services, checks out the old SHA, rebuilds the venv and the frontend, and
brings back only the dashboard.

The trading runtimes stay stopped on purpose. Restarting a trading process as
the last step of an incident response is a decision made with reconciliation
output in front of you, not by a script that just finished a `git checkout`.

**The database is never rolled back.** It is an audit trail of things that
actually happened at a broker, and the broker's record does not rewind because
this host checked out an older tree. Restoring yesterday's file would produce
local state that disagrees with the account, which reconciliation would then
overwrite from the account anyway.

If the schema has moved past what the target commit understands, the rollback
**fails closed** and changes nothing — the older code would refuse to open the
newer file rather than downgrade it and discard data. Choose a newer rollback
target, restore a pre-migration backup deliberately, or fix forward.

### 14. Publish the dashboard — optional, and separate again

```bash
/opt/autotrader/app/deploy/bin/autotrader-publish-web --dry-run --domain dash.example.com
/opt/autotrader/app/deploy/bin/autotrader-publish-web --domain dash.example.com
```

Copy the printed password into a password manager before closing the terminal.
Then check the boundary from somewhere that is not this host:

```bash
curl -sI  https://dash.example.com/                    # expect 401
curl -sI -u user:pass https://dash.example.com/        # expect 200
curl -sI -u user:pass -X POST https://dash.example.com/  # expect 405
curl -sS  http://dash.example.com/ -o /dev/null -w '%{http_code} %{redirect_url}\n'  # expect 308
```

`autotrader-publish-web --unpublish` reverses it. See
[Publishing the dashboard](#publishing-the-dashboard).

### 15. Emergency stop

```bash
/opt/autotrader/app/deploy/bin/autotrader-emergency-stop
/opt/autotrader/app/deploy/bin/autotrader-emergency-stop --disable-boot
```

`SIGTERM` to both runtimes; they finish the cycle they are in and exit cleanly.

**It places no orders.** Nothing sells a position, cancels a resting order, or
flattens the account. An automated unwind is itself automated trading, fired at
the exact moment an operator has decided the automation cannot be trusted. If a
position needs closing, a human closes it knowing what it is.

Afterwards: every position is still open, any accepted order is still working,
the dashboard is still up, and the database is intact. Run `reconcile
--dry-run` before starting anything again.

---

## What is still pinned to Combined Integration

`feat/combined-integration` has landed. These artifacts were originally written
against `main` plus the published command surfaces of `feat/equity-v0.2` and
`feat/dashboard-v0.1`, and this table was the list of predictions to re-check
once that branch was GREEN. Every row has now been checked against the merged
source rather than against the plan.

| Where | What was checked | Result |
|---|---|---|
| `autotrader.env` → `AUTOTRADER_EQUITY_ARGS` | that `equity-run` still takes `--observe-only`, `--confirm-paper-runtime`, `--db` | **unchanged** — all three exist on the merged `equity-run`, spelled the same way |
| `autotrader-crypto.service`, `autotrader-equity.service` → `ExecStart` | the subcommand names `crypto-run` and `equity-run` | **unchanged** — both are still separate top-level commands; there is no combined runner and no shared account-safety flag |
| `autotrader.env` → `AUTOTRADER_DASHBOARD_DB` | that the dashboard still reads `AUTOTRADER_DASHBOARD_DB` | **unchanged** — `autotrader.dashboard.api.DATABASE_PATH_ENV` is still that name |
| `RestartPreventExitStatus=2` on both runtimes | that exit 2 still means "paused, needs reconciliation" | **unchanged** — 1 is a controlled refusal, 2 is a paused runtime, and the shared account halt added no third code |
| `deploy/bin/autotrader-healthcheck` → `_check_account_safety` | replace the name probe with the real table | **adapted** — the table is `account_safety_state`, a single row pinned by `CHECK (id = 1)`, and the check now reports its `state`: `SAFE` is OK, `UNSAFE_UNKNOWN` and `UNSAFE_RECONCILIATION` are FAIL |
| `deploy/bin/autotrader-rollback` → schema gate | that `SCHEMA_VERSION` and `MIN_MIGRATABLE_SCHEMA_VERSION` are still module-level assignments | **unchanged** — both are still plain assignments in `src/autotrader/state/sqlite.py`; the current schema is v6 |
| lock scoping | that crypto and equity still take differently-scoped locks | **unchanged** — crypto takes the unscoped runtime lock, equity takes a scoped one, and the shared account execution lock is a third, separate file. The two services still run concurrently |
| `deploy/caddy/Caddyfile` | the domain and the bcrypt hash | **closed** — both are now `{$VAR}` reads from `/etc/autotrader/autotrader.web.env`, so the file installs verbatim and neither value is in Git. `autotrader-publish-web` writes that file. See "Publishing the dashboard" |

One further adaptation was made for the crypto-only posture described below:
`autotrader-healthcheck` now reports a **masked** unit as OK rather than as a
degraded trading runtime. Masking is a deliberate, root-only, reversible act,
and a host that has masked the equity runtime on purpose should not report
DEGRADED forever for having done what its operator intended.

## Crypto-only posture

A host may run the crypto runtime while the equity runtime is not merely
stopped but *unstartable*. That is a stronger statement than "the market is
closed", and it is the posture this deployment supports first:

```
systemctl mask autotrader-equity.service
```

`mask` symlinks the unit to `/dev/null` in `/etc/systemd/system`.
`systemctl start` on it fails, a dependency cannot pull it up, and a reboot
cannot resume it. It is reversible with `systemctl unmask`.

**This is why the units are deployed to `/usr/lib/systemd/system`.** systemd
reads the admin directory first and the vendor directory last, so a mask in
`/etc` outranks the unit in `/usr/lib` — and `systemctl mask` refuses to run at
all when a regular file already occupies the path it wants to symlink. A
deployment that installed its units into `/etc` would therefore make a trading
runtime *unmaskable*; worse, deploying into a path where a mask already exists
replaces the symlink with a regular file, silently re-arming a service an
operator deliberately switched off. `install` does not preserve a symlink.

So the two layers are kept apart: `autotrader-deploy` writes only the vendor
directory, and it additionally refuses any unit whose target path is a mask
symlink, reporting it as left alone. It also warns when a regular file in
`/etc` shadows a unit it just installed, because that host would keep running
the old unit while `git rev-parse` truthfully reported the new SHA.

Verify the posture, rather than assuming it:

```
systemctl is-active autotrader-equity.service     # inactive
systemctl is-enabled autotrader-equity.service    # masked
systemctl is-active autotrader-crypto.service     # active
systemctl is-enabled autotrader-crypto.service    # enabled
```

Note what masking does **not** do. `/etc/autotrader/autotrader.trading.env`
sets `AUTOTRADER_EQUITY_ARGS` as well as the crypto one, because activation is
account-wide rather than per-product. A masked unit never reads it. Unmasking
the equity runtime is therefore a decision that re-enables equity paper
submission on the next start, and it should be made with the same deliberation
as activation itself.
