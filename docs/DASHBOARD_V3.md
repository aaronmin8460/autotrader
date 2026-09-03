# Dashboard V3 — Institutional Glass

The read-only operations console, redesigned. This document is the deployable
description of it: what changed, what did not, how to deploy it, and how to put
it back.

Dashboard V3 is a **frontend change**. It adds no endpoint, changes no Python,
installs no unit, edits no Caddy rule and introduces no write path. Every route
the API serves is still a GET, and the edge still answers `405` to anything
else.

---

## What did not change

* **The five API processes**, their ports, identities, records and prefixes.
  `next.config.mjs` rewrites the same five prefixes to the same five loopback
  origins, read at build time, none of them `NEXT_PUBLIC_`.
* **Every deployed route.** `/`, `/equity-paper`, `/shadows` keep their exact
  paths, and `/equity-shadow` keeps its permanent redirect. Four routes were
  added; none was renamed, moved or removed, so no bookmark breaks.
* **Poll cadence.** Account 5 s, services 5 s, paper record 15 s, realized
  ledger 30 s, chart bars TTL-cached. These are now polled **once for the whole
  application** rather than once per page, so an eight-route dashboard does not
  make eight times the requests a three-route one did.
* **Every trading semantic.** Target is still the runtime's own recorded row,
  never recomputed. Actual is still the broker's. `PENDING_NEW` is still not
  `FILLED`. A running observer still reads `OBSERVING` in violet and never
  green. `MASKED` on the legacy unit is still neutral. Risk limits still come
  from `/api/equity-paper/policy` and never from the `:8000` risk panel.

## The design language

Three surface levels and nothing between them:

| level | what | treatment |
|---|---|---|
| 0 | the page | `--app-bg`, no border |
| 1 | `Surface` / `Card` — where every number lives | opaque, one hairline, `--radius-md` |
| 2 | sidebar, status bar, drawer, overlay | the only translucency in the system |

Grouping *inside* a level 1 surface is done with space and type. There are no
bordered boxes inside bordered boxes.

Colour carries five meanings and nothing else: positive, negative, warning,
interactive, and violet for observation-only. Status always prints its word;
the dot beside it is a redundancy. **A 90 % gross book under a 95 % cap is
normal and looks normal** — red means a cap was actually breached.

Tokens live in one place, `app/globals.css`: colour, a type scale named by
role, spacing, radius, depth, motion duration and easing, and z-index layers.
Dark is the designed mode and the explicit default, written before first paint
by an inline bootstrap script so the palette never depends on the operator's
operating system and never flashes.

## Information architecture

```
Overview      /             account truth, market state, risk, positions, orders
Portfolio     /portfolio    positions, target vs actual, allocation, P&L
Strategies    /strategies   what trades, what only observes, what is off
  Equity Paper /equity-paper
  Shadows      /shadows
Orders        /orders       the account-wide merged stream, unabridged
Risk          /risk         the deployed policy and operational safety
System        /system       services, dashboard APIs, reconciliation, freshness
```

The Overview answers the operator's questions in order, and the numeric
hierarchy is that order made visible: account equity is the only `display`-size
figure on any page. The **EDA-1 session regime is a first-class panel** there —
at V2 it existed only as a pill on another page, so the page an operator opens
could not say whether the system was participating.

Process plumbing — API budgets, processed-bar checkpoints, the last failure
event, the crypto runtime's trail, reconciliation detail — moved to System.
Nothing was deleted; each panel has more room than it had.

## Three P&L numbers, three meanings

The dashboard renders three and never adds them:

* **Today's change** — account equity against the stored UTC-day baseline,
  whole account, crypto included. This is the figure the daily-loss halt is
  measured on. It is *not* realized P&L and the label says so.
* **Realized** — what confirmed equity sales released, from the accounting
  ledger, under weighted-average cost. Always rendered with its reconciliation
  status and its tracking horizon beside it.
* **Unrealized** — the broker's own mark on open equity positions.

The account-equity **curve** does not exist: no runtime persists that series
and no endpoint serves one. It renders as an explicit `Not tracked` with the
reason, and is not reconstructed from price bars.

## Bilingual — English and Korean

`lib/i18n/` holds two catalogues typed against each other, so a missing or
misspelled Korean key is a **build error**, not an English word appearing in a
Korean page. The switcher is in the global controls, preserves the route, the
open drawer and the selected symbol, and persists per browser.

**Authoritative identifiers are never translated.** `PARTICIPATE`,
`DEFENSIVE`, `LONG`, `FLAT`, `BUY`, `SELL`, `FILLED`, `PENDING_NEW`, `NEW`,
`ACCEPTED`, `REJECTED`, `UNKNOWN`, `RUNNING`, `OBSERVING`, `MASKED`, `CLEAN`,
`DEGRADED`, `MISMATCH`, `EDA-1`, `V3`, `A1-B U30`, ticker symbols, policy ids,
config hashes, order ids and unit names print identically in both locales. A
Korean gloss sits **beside** such a value and never in place of it, and
`lib/i18n.test.ts` asserts that against the identifier list directly.

Currency is never converted: `$101,995.05` is the account's own currency in
both locales. Dates localise (`Sep 3, 2026` / `2026. 9. 3.`); the clock does
not, because the risk day is a UTC day and a 12-hour local rendering would put
a reader one conversion away from the rule that halts trading.

## Two constraints the deployed edge imposes

1. **`style-src 'self'` has no `style-src-attr`.** An inline `style` attribute
   present in *server-rendered* markup is blocked outright by the browser. Table
   minimum widths are therefore literal Tailwind classes, not `style` props.
   Dynamic geometry (bars, rails, sparklines) is written after hydration through
   the CSSOM, which the policy permits. The QA harness asserts the SSR payload
   of every route contains zero `style="` attributes.
2. **`script-src` must keep `'unsafe-inline'`.** The App Router bootstrap
   requires it, and so does the pre-paint theme/locale script. Nothing else was
   widened: no external font, no `data:` image, no new connect origin.

## Verifying a change

```
cd dashboard/frontend
./node_modules/.bin/tsc --noEmit
./node_modules/.bin/eslint .
node --test --experimental-strip-types lib/*.test.ts
./node_modules/.bin/next build
```

Browser QA must be run **behind the production headers**, not against the
Next.js server directly — a CSP failure arrives inside an HTTP 200 and curl
cannot see it. The harness and the header-replaying proxy live in the QA
workspace at `reports/dashboard-v3/tools/`.

## Deploying

V3 changes the frontend only, so steps 2, 3 and 5 of the V2 procedure — the
dashboard venv, the API tree and the unit/Caddy install — are **not needed**,
and no API or trading process is restarted.

1. Push the branch. On the VPS, as root:
2. `git -C /opt/autotrader-equity-paper/app fetch origin && git -C /opt/autotrader-equity-paper/app reset --hard origin/<branch>`
3. In `dashboard/frontend`: `./node_modules/.bin/next build`, then
   `chown -R ateqpaper:ateqpaper .next`.
4. `systemctl restart autotrader-dashboard-web` — **that unit and no other.**
5. Verify `MainPID` and `NRestarts` of `autotrader-crypto`,
   `autotrader-equity-paper`, `autotrader-equity-shadow`,
   `autotrader-equity-a1b-shadow` are unchanged, `autotrader-equity` is still
   masked, and the four API units were not restarted.

`.next` and `node_modules` are gitignored and survive the reset. `npm` on this
host is fragile; run `next` from `node_modules/.bin` directly and never
`npm ci`.

## Rollback

Reset the paper tree to the previous SHA, rebuild, `chown`, restart
`autotrader-dashboard-web`. Nothing else is involved in either direction, and
no trading unit is touched going back any more than going forward.
