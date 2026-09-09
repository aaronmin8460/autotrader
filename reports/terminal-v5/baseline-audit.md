# Terminal V5 baseline audit

Baseline: Terminal V4 at `85d9c3706cbf2d8d9280b07c799cb7f38fb02b26`.
Production lineage to preserve: `3d03968f19cf8b7c129c4648d555f65b02b2b12e`.

## Architecture

- Next.js 15 App Router, React 19, TypeScript, hand-authored wire contracts.
- One shared `AppShell`, `DashboardProvider`, and `TerminalProvider`.
- Thin route modules delegate to domain page components.
- Custom SVG/CSS line, bar, allocation, exposure, contribution, and sparkline charts; no external chart library.
- Dark mode is token-based through `data-theme`; light mode remains supported.
- Dense tables use tabular/monospace numerals and horizontal overflow on small screens.
- Every browser request is a poll to a read-only endpoint. Existing tests reject forms and mutating fetch methods.

## V4 route and navigation map

| Route | Presentation domain |
| --- | --- |
| `/` | Combined Live command center |
| `/live`, `/portfolio` | Live portfolio |
| `/strategy`, `/strategies` | Strategy and decision tape |
| `/execution`, `/orders` | Live execution |
| `/risk` | Live risk center |
| `/performance` | Live accounting performance |
| `/capital` | Capital/accounting |
| `/compare`, `/equity-paper` | Paper-vs-Live comparison |
| `/research`, `/shadows`, `/equity-shadow` | Observation-only research |
| `/system` | Services, reconciliation, API topology, audit events |

The V4 shell was a flat numbered list. Live was emphasized but Paper was only a comparison workspace, not a separate primary account page.

## Read-model authority

- Live safety: `/api/live-safety/summary`
- Live portfolio/execution/strategy/events: `/api/live-safety/terminal`
- Frozen Live accounting: `/api/live-accounting/summary`
- Live accounting checkpoints/history: `/api/live-accounting/history`
- Paper account: `/api/dashboard/overview`
- Paper runtime/policy/orders/safety: `/api/equity-paper/overview`
- Services: `/api/equity-paper/services`
- Shadow and A1-B observation stores remain separate.

The browser does not derive account truth, risk ceilings, order lifecycle state, targets, signals, reconciliation, or accounting. Null remains unavailable rather than zero. The only V5 arithmetic added is a presentation-only pixel scale for a risk bar using two already-resolved backend dollar values; displayed limits remain untouched authoritative strings.

## Baseline visual findings

- Strong data correctness, exact decimal handling, fail-closed empty states, and read-only semantics.
- Excessively small status/navigation type at 1440px.
- Large Live authorization hero and repeated prose consumed space without improving scan speed.
- Live was a portfolio subpage instead of the primary real-capital account surface.
- Paper lacked its own first-class route and account-specific shell context.
- Mobile behavior was structurally sound in late V4, but its route priorities did not put Live and Paper side by side.

Baseline screenshots were captured for 1440×900, 1920×1080, 834×1112, and 390×844 before V5 edits under `/Volumes/AUTOTRADER_QA/reports/terminal-v4/screenshots/terminal-v5-baseline/`.
