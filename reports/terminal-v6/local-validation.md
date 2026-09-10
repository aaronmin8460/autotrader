# AutoTrader Terminal V6 — local validation

## Candidate scope

- Base: `df27f6b32ef70c0d549300ae3f4350bb81085979`.
- Allowed changes: `dashboard/frontend/**` and `reports/terminal-v6/**` only.
- Protected-path diff: empty for `src/`, `tests/`, `deploy/`, and `live_accounting_contract.json`.
- New backend/read adapter: none.

## Functional gates

- TypeScript (`npm run typecheck`): PASS.
- ESLint (`npm run lint`): PASS.
- Frontend unit/contract suite (`npm test`): PASS, 167/167.
- Optimized production build (`npm run build`): PASS, 27 static outputs.
- Existing GET-only/no-form/no-mutation tests: PASS.
- V6-specific authority and unavailable-state tests: PASS, 4/4.
- `git diff --check`: PASS.
- Targeted credential-pattern scan of the frontend and V6 reports: PASS.

`npm audit --omit=dev` reports two inherited advisories inside Next's bundled PostCSS dependency (one moderate, one high). The only automated remediation offered is a breaking Next 16 upgrade; package and lock files are unchanged by V6.

## Visual QA

Deterministic browser QA used intercepted GET-only authoritative fixtures.

- Viewports: 1920×1080, 1440×900, 834×1112, 390×844.
- Routes: `/live`, `/paper`.
- Checks: 8 route/viewport combinations, zero failures.
- Page-level overflow: none.
- Console errors: none.
- Missing H1/main landmarks: none.
- Missing chart surfaces: none.
- Mutation controls: none.
- Invalid numeric tokens (`NaN`, `undefined`, `Infinity`): none.

Screenshots are in `reports/terminal-v6/screenshots/`.

## Production gate state

The public production boundary at `https://135.148.138.60.sslip.io/` responds with the expected unauthenticated `401`. Current privileged read-only production state could not be verified because the available encrypted SSH keys are not unlocked in the current agent session and the SSH agent holds no identities. No deployment was attempted while the required runtime PID/start-time, ARM state, frontend SHA, and frontend-only restart scope remained unverified.
