#!/usr/bin/env python3
"""Deterministic V6 Live/Paper visual QA with all API reads intercepted."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from playwright.async_api import ConsoleMessage, Route, async_playwright

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "dashboard" / "frontend"
OUT = Path(__file__).resolve().parent / "screenshots"
BASE = "http://127.0.0.1:33100"
BROWSER = Path("/Volumes/AUTOTRADER_QA/caches/playwright/chromium-1243/chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")

VIEWPORTS = {
    "desktop-1440x900": (1440, 900),
    "wide-1920x1080": (1920, 1080),
    "tablet-834x1112": (834, 1112),
    "mobile-390x844": (390, 844),
}


def live_fixtures() -> dict[str, object]:
    script = (
        f'import {{ TERMINAL_EDGE_FIXTURES }} from "{(FRONTEND / "lib/terminal-fixtures.ts").as_uri()}"; '
        'const x=TERMINAL_EDGE_FIXTURES.find(x=>x.key==="armed-participate-position-heavy-positive"); '
        'console.log(JSON.stringify({safety:x.fixture.safety,accounting:x.fixture.accounting,terminal:x.terminal,history:x.history}));'
    )
    return json.loads(subprocess.check_output(["node", "--experimental-strip-types", "--input-type=module", "-e", script], text=True))


def paper_fixtures() -> tuple[dict[str, object], dict[str, object]]:
    amount = lambda value: {"value": value, "available": True, "unavailable_reason": None}
    overview = {
        "generated_at": "2026-09-08T14:05:00+00:00",
        "environment": "PAPER",
        "metrics": {"equity": amount(100000.0), "cash": amount(91000.0), "daily_pnl": amount(122.41), "daily_pnl_fraction": 0.0012241, "daily_pnl_baseline": amount(99877.59), "daily_pnl_baseline_date": "2026-09-08", "exposure": amount(9000.0), "exposure_fraction": 0.09},
        "positions": {"source": "BROKER", "as_of": "2026-09-08T14:05:00+00:00", "flat_symbols": [], "unavailable_reason": None, "note": None, "rows": [
            {"symbol": "SPY", "asset_class": "EQUITY", "quantity": "12.000", "price": 651.24, "market_value": 7814.88, "average_entry_price": 648.10, "unrealized_pnl": 37.68, "unrealized_pnl_fraction": 0.00484, "updated_at": "2026-09-08T14:05:00+00:00", "source": "BROKER"},
            {"symbol": "QQQ", "asset_class": "EQUITY", "quantity": "2.000", "price": 592.56, "market_value": 1185.12, "average_entry_price": 588.20, "unrealized_pnl": 8.72, "unrealized_pnl_fraction": 0.00741, "updated_at": "2026-09-08T14:05:00+00:00", "source": "BROKER"},
        ]},
    }
    paper = {
        "generated_at": "2026-09-08T14:05:00+00:00", "mode": "PAPER", "read_only": True,
        "service": {"mode": "PAPER", "environment": "PAPER", "running": True, "stale": False, "stage": "C", "execution_universe": ["SPY", "QQQ"], "decision_universe": [], "sizing_policy": "PAPER_FRACTIONAL_90", "sizing_config_hash": "paperfixture", "started_at": "2026-09-08T13:30:00+00:00", "stopped_at": None, "last_cycle_at": "2026-09-08T14:00:00+00:00", "unresolved_intents": 0, "unavailable_reason": None},
        "regime": {"session_date": "2026-09-08", "participate": True, "reference_symbol": "SPY", "info_close": 651.24, "info_sma": 598.2, "info_drawdown": -0.012, "sessions_observed": 420, "spec": {}},
        "exposure": {"account_equity": 100000, "crypto_positions": [], "equity_positions": ["SPY", "QQQ"], "equity_positions_as_of": "2026-09-08T14:05:00+00:00", "equity_exposure_note": "simulated", "per_symbol_cap": "11%", "total_account_cap": "95%", "target_account_gross": "90%", "cash_reserve_target": "10%", "fractional_mode": True, "daily_loss_halt": "2%"},
        "targets": [{"symbol": "SPY", "in_execution_universe": True, "bar_timestamp": "2026-09-08T14:00:00Z", "participate": True, "eda1_signal": "LONG", "eda1_stance": 1, "v3_signal": None, "stances_agree": None, "reference_close": 651.24, "actual_quantity": "12.000", "last_risk_reason": "PASS", "stance_label": "LONG", "target_weight": .09, "target_source": "RUNTIME", "target_notional": 9000, "target_quantity": "13.819", "target_bar_timestamp": "2026-09-08T14:00:00Z", "target_decided_at": "2026-09-08T14:00:01Z", "target_external_exposure": None, "last_order_side": "BUY", "last_order_client_order_id": "paper-spy-001", "action": "HOLD"}],
        "orders": [{"client_order_id": "paper-spy-001", "symbol": "SPY", "side": "BUY", "requested_quantity": "12", "approved_quantity": "12", "status": "FILLED", "risk_reason_code": "PASS", "created_at": "2026-09-08T13:45:01+00:00", "broker_status": "filled", "filled_quantity": "12", "filled_average_price": 648.10}],
        "safety": {"account_safety": "SAFE", "account_safety_reason": None, "reconciliation_status": "CLEAN", "reconciliation_at": "2026-09-08T14:00:00+00:00", "reconciliation_unresolved": 0, "parity_mismatches": 0, "risk_blocked_recent": []},
        "policy": {"policy_id": "PAPER_FRACTIONAL_90", "config_hash": "paperfixture", "source": "RUNTIME", "authoritative": True, "target_gross": .9, "hard_gross_cap": .95, "hard_symbol_cap": .11, "cash_reserve_target": .1, "target_slot_weight": .09, "universe_size": 10, "fractional": True, "daily_loss_halt": .02, "note": "Runtime-authoritative Paper policy"},
    }
    return overview, paper


async def main() -> None:
    live = live_fixtures()
    overview, paper = paper_fixtures()
    OUT.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=str(BROWSER), headless=True)
        for viewport_name, (width, height) in VIEWPORTS.items():
            context = await browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark")
            page = await context.new_page()
            console_errors: list[str] = []

            def on_console(message: ConsoleMessage) -> None:
                if message.type == "error":
                    console_errors.append(message.text)

            page.on("console", on_console)

            async def api_route(route: Route) -> None:
                url = route.request.url
                payload: object = {}
                if "/api/live-safety/summary" in url: payload = live["safety"]
                elif "/api/live-accounting/summary" in url: payload = live["accounting"]
                elif "/api/live-safety/terminal" in url: payload = live["terminal"]
                elif "/api/live-accounting/history" in url: payload = live["history"]
                elif "/api/dashboard/overview" in url: payload = overview
                elif "/api/equity-paper/overview" in url: payload = paper
                elif "/api/equity-paper/services" in url: payload = {"units": []}
                await route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

            await page.route("**/api/**", api_route)
            for route_name in ("live", "paper"):
                console_errors.clear()
                response = await page.goto(f"{BASE}/{route_name}", wait_until="networkidle", timeout=45_000)
                await page.wait_for_timeout(300)
                metrics = await page.evaluate("""() => ({
                  scrollWidth: document.documentElement.scrollWidth,
                  clientWidth: document.documentElement.clientWidth,
                  h1: document.querySelectorAll('h1').length,
                  main: document.querySelectorAll('main').length,
                  text: document.body.innerText,
                  charts: document.querySelectorAll('svg, [role="img"]').length,
                  mutationButtons: [...document.querySelectorAll('button')].map(x => (x.textContent || '').trim()).filter(x => /^(ARM|DISARM|BUY|SELL|CANCEL|REPLACE|WITHDRAW|DEPOSIT)$/i.test(x)),
                })""")
                required = ["REAL CAPITAL TRADING", "Trading P&L", "Broker Equity", "Flow-Adjusted Equity", "External Cash Flow"] if route_name == "live" else ["SIMULATED TRADING", "NO REAL CAPITAL", "Paper Equity Curve", "PAPER EQUITY HISTORY NOT RECORDED", "Cash Performance / Cash Earned"]
                missing = [token for token in required if token.lower() not in metrics["text"].lower()]
                bad = [token for token in ("NaN", "undefined", "Infinity") if token in metrics["text"]]
                if not response or response.status != 200 or metrics["scrollWidth"] > metrics["clientWidth"] + 1 or metrics["h1"] != 1 or metrics["main"] != 1 or metrics["charts"] < 1 or metrics["mutationButtons"] or bad or missing or console_errors:
                    failures.append(f"{viewport_name}/{route_name}: width={metrics['scrollWidth']}/{metrics['clientWidth']} h1={metrics['h1']} main={metrics['main']} charts={metrics['charts']} mutation={metrics['mutationButtons']} bad={bad} missing={missing} console={console_errors}")
                await page.screenshot(path=str(OUT / f"{viewport_name}-{route_name}.png"), full_page=True)
            await context.close()
        await browser.close()
    print(json.dumps({"viewports": list(VIEWPORTS), "routes": ["live", "paper"], "failures": failures}, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
