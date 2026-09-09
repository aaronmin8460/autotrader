# AutoTrader Terminal V5 — implementation and QA

## Outcome

Terminal V5 replaces the flat V4 workspace list with a restrained institutional operations hierarchy. Live and Paper are now separate primary pages with different account sources, headers, status ribbons, balances, positions, orders, strategy, and safety semantics.

## V5 route map

| Navigation | Route | Page/source |
| --- | --- | --- |
| Trading / Live | `/live` (and `/`) | Real-capital dashboard from Live safety, terminal, accounting, and history read models |
| Trading / Paper | `/paper` (`/equity-paper` redirects) | Simulated account from Paper/account read models |
| Portfolio / Positions | `/positions` | Broker-authoritative Live position inspector |
| Portfolio / Orders | `/orders` | Live execution tape |
| Portfolio / Performance | `/performance` | Authoritative accounting history |
| Portfolio / Reports | `/reports` | Capital/accounting report |
| Risk / Risk Monitor | `/risk` | Live fail-closed risk center |
| Risk / Limits | `/limits` | Same authoritative risk source, dedicated route |
| Risk / Reconciliation | `/reconciliation` | System reconciliation view |
| System / Strategy | `/strategy` | EDA-1 runtime/decision records |
| System / Services | `/services` | Service and API topology |
| System / Audit | `/audit` | Structured operational event view |

Legacy `/execution`, `/capital`, `/compare`, `/system`, `/strategies`, `/portfolio`, and research routes remain valid.

## Design system

- Near-black canvas, charcoal panels, thin neutral borders, 5px radii.
- Compact 35px navigation rows, 31px financial table rows, restrained spacing.
- Tabular monospace for money, quantity, timestamps, hashes, IDs, and risk values.
- Green only for safe/clean/fresh/active and positive P&L; red for ARMED, faults, and losses; amber for warning/partial; blue for Paper/navigation information.
- No marketing surfaces, decorative gradients, donut overload, or order controls.
- Desktop uses an equity/positions workbench with a compact account/risk/safety rail. Tablet becomes one column. Mobile keeps the status ribbon and Live/Paper/Positions/Orders bottom navigation; tables scroll horizontally.

## Components changed

- Added `TradingPages.tsx`: dedicated Live and Paper dashboards, authoritative tables, risk bars, and per-symbol day-P&L view.
- Reworked `AppShell.tsx`: institutional grouped navigation, V5 identity, context-sensitive Live/Paper top ribbon, and mobile route priorities.
- Refined `TerminalUI.tsx`: V5 labels and six chart ranges; unsupported history ranges are visibly disabled.
- Refined `terminal.css`: V5 density, responsive grids, semantic surfaces, and compact tables.
- Added dedicated route modules for Paper, Positions, Reports, Limits, Reconciliation, Services, and Audit.

## Live/Paper isolation

Live reads only Live safety/terminal/accounting/history records and labels ARMED as cautionary real-money authorization. Paper reads only the Paper runtime and simulated broker-account records. Paper never displays Live ARM state, Live balances, Live orders, or Live positions. The persistent shell changes from `AUTOTRADER · LIVE` to `AUTOTRADER · PAPER`, and Paper adds an explicit “NO REAL CAPITAL” boundary before any number.

## Graph authority

- Live equity curve: backend accounting checkpoints only; broker and flow-adjusted series remain distinct.
- Range controls: 1D, 1W, 1M, 3M, 1Y, ALL. A range is disabled when the returned authoritative history does not span it.
- Day P&L by symbol: the `day_pnl` values in the Live terminal position read model. No missing symbols or values are invented.
- Risk bars: dollar labels are backend-resolved values. Bar length is a presentation ratio of resolved value to resolved absolute envelope and is not exposed as an official limit or accounting field.
- No Paper equity curve was fabricated because the current Paper read model does not expose an account-equity history series.

## Visual QA

- Baseline: 4 viewports × 10 V4 routes captured before edits.
- Pass 1, structure: 1440×900 × 8 V5 routes — passed.
- Pass 2, institutional simplification: 1920×1080 × 8 V5 routes — passed.
- Pass 3, responsive: 834×1112 and 390×844 × 8 V5 routes — passed.
- Checks: HTTP success, one H1, no document overflow, no NaN/Infinity/undefined, no ARM/DISARM/BUY/SELL/WITHDRAW/DEPOSIT controls.

Screenshots are in `reports/terminal-v5/screenshots/`.

## Functional QA

- TypeScript: passed.
- ESLint: passed.
- Frontend unit/contract suite: 163/163 passed.
- Visual/routing/responsive checks: all passes, zero failures.
- Production build: recorded after final candidate build.
- Secret scan and protected-path diff: recorded after candidate finalization.

## Promotion state

No trading state was mutated while developing or testing this frontend. Production deployment must use the repository deployment runbook and pass the pre/post Live health gates; deployment evidence is recorded separately if that gate is available.
