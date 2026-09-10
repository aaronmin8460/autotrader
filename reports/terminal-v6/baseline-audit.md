# AutoTrader Terminal V6 — baseline audit

Baseline commit: `df27f6b32ef70c0d549300ae3f4350bb81085979` (`origin/main` and `origin/feat/autotrader-terminal-v5` at branch creation).

Isolated worktree: `/Volumes/AUTOTRADER_QA/worktrees/terminal-v6`.

## Current production-aligned frontend

- Next.js 15 / React 19, using hand-authored read-only contracts and GET polling only.
- `/live` is a dedicated real-capital route; `/paper` is a dedicated simulated route.
- Live uses the safety, terminal, frozen accounting summary, and accounting-history read models.
- Paper uses the account overview and Equity Paper overview read models.
- The V5 frontend tests already prohibit forms and mutating request methods.

## Visual baseline

The current V5 Live and Paper pages were previously captured at the production-aligned SHA for all required viewports. The exact current screenshots are preserved in:

`/Volumes/AUTOTRADER_QA/worktrees/terminal-v5/reports/terminal-v5/screenshots/pass-4-orders-fix/`

That set contains `/live` and `/paper` at 1440×900, 1920×1080, 834×1112, and 390×844.

## Findings carried into V6

- The Live chart identifies broker and flow-adjusted equity by a small legend, but current values, range movement, scale context, and hover inspection are not immediately visible.
- External cash flows appear as markers but lack a strong series/value identity.
- Authoritative cash history exists in Live checkpoints, but it is not charted on the flagship page.
- Paper has no equity, exposure, or cash history in its current authoritative read models and therefore has no chart surface.
- Trading P&L exists for Live, but the "money made" interpretation competes with account-summary detail.
- Paper exposes daily P&L but no authoritative realized P&L, TWR, or cash-earned metric; the UI must state those boundaries directly.
- Tables are accurate but default to more visible columns than needed for fast scanning.

## Safety boundary

V6 will modify only `dashboard/frontend/**` and `reports/terminal-v6/**`. It will not add endpoints, change trading/risk/reconciliation code, alter deployment units, or introduce browser mutations.
