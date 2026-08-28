"""The published dashboard, checked in a real browser engine.

Every check in `test_deployment_artifacts.py` reads a file, and every check the
first publish ran against the live host used curl. Both passed while the page
was blank: `default-src 'self'` blocked all seven of Next.js's inline bootstrap
scripts, hydration never started, and the server still returned HTTP 200 with a
full HTML body. **A 200 is not evidence that a page renders**, and nothing that
does not execute JavaScript can tell the difference.

So this module drives Chromium. It is opt-in twice over - it needs Playwright
installed and it needs the address and credentials of a real deployment - and
it is skipped, not failed, when either is absent, because the offline suite
must stay runnable on a laptop with no network and no browser.

    pip install '.[browser]' && playwright install chromium
    export AUTOTRADER_DASHBOARD_URL=https://dashboard.example.com
    export AUTOTRADER_DASHBOARD_USER=...
    export AUTOTRADER_DASHBOARD_PASSWORD=...      # from a 0600 file, not argv
    pytest -q tests/test_web_publish_browser.py

The password is read from the environment and never written to a log, an
assertion message, or a command line. Playwright puts it in an `Authorization`
header through `http_credentials`; it never appears in a URL.
"""

from __future__ import annotations

import os
import re
import socket
from urllib.parse import urlsplit

import pytest

pytest.importorskip("playwright.sync_api", reason="pip install '.[browser]'")

from playwright.sync_api import sync_playwright  # noqa: E402

URL = os.environ.get("AUTOTRADER_DASHBOARD_URL", "").rstrip("/")
USER = os.environ.get("AUTOTRADER_DASHBOARD_USER", "")
PASSWORD = os.environ.get("AUTOTRADER_DASHBOARD_PASSWORD", "")

pytestmark = pytest.mark.skipif(
    not (URL and USER and PASSWORD),
    reason="set AUTOTRADER_DASHBOARD_URL / _USER / _PASSWORD to test a live deployment",
)

#: Every route the dashboard API declares. All six are GETs.
API_ROUTES = ("health", "overview", "positions", "orders", "risk", "system")

#: Nothing the dashboard serves may be reachable by any of these.
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

#: Ports that must never answer from outside the host.
PRIVATE_PORTS = (3000, 8000, 2019)

#: Long enough for the client-side poll to complete a real request.
SETTLE_MS = 4000

#: Shapes that must never reach the browser: broker keys are 20+ alphanumerics,
#: and no credential of any kind belongs in a page or an API body.
SECRET_SHAPES = re.compile(
    r"(ALPACA_[A-Z_]*KEY|SECRET_KEY|api[_-]?secret|password\"?\s*[:=]|bearer\s+[A-Za-z0-9._-]{16,})",
    re.IGNORECASE,
)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def loaded(browser):
    """The dashboard, loaded once, with everything the browser complained about.

    Module-scoped because a page load is seconds and every assertion below is a
    different question about the same load. Collecting the console, the page
    errors and the CSP violations from one navigation also means they describe
    the same moment, which matters when one causes another.
    """
    context = browser.new_context(http_credentials={"username": USER, "password": PASSWORD})
    page = context.new_page()

    console: list[tuple[str, str]] = []
    errors: list[str] = []
    failed: list[str] = []
    page.on("console", lambda message: console.append((message.type, message.text)))
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "requestfailed",
        lambda request: failed.append(f"{request.url} {request.failure}"),
    )

    # `securitypolicyviolation` is the only report that survives a policy which
    # blocks the script that would otherwise have reported it.
    page.add_init_script(
        """
        document.addEventListener('securitypolicyviolation', event => {
          (window.__cspViolations = window.__cspViolations || []).push({
            directive: event.effectiveDirective,
            blocked: event.blockedURI || 'inline',
          });
        });
        """
    )

    page.goto(URL, wait_until="networkidle", timeout=45_000)
    page.wait_for_timeout(SETTLE_MS)

    state = {
        "page": page,
        "console": console,
        "errors": errors,
        "failed": failed,
        "violations": page.evaluate("window.__cspViolations || []"),
        "text": page.evaluate("document.body.innerText || ''"),
    }
    yield state
    context.close()


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


def test_the_page_is_refused_without_credentials(browser):
    """No credentials in the context at all - not wrong ones, none."""
    context = browser.new_context()
    try:
        response = context.request.get(URL + "/")
        assert response.status == 401, response.status
    finally:
        context.close()


def test_a_static_asset_is_refused_without_credentials(browser):
    """The bundle is as sensitive as the page: it is the page."""
    context = browser.new_context()
    try:
        assert context.request.get(URL + "/_next/static/chunks/main-app.js").status == 401
    finally:
        context.close()


def test_the_page_loads_with_credentials(loaded):
    assert loaded["page"].url.startswith(URL)


# ---------------------------------------------------------------------------
# What the first publish could not see
# ---------------------------------------------------------------------------


def test_no_content_security_policy_violation(loaded):
    """The regression itself.

    Seven inline bootstrap scripts were blocked by `default-src 'self'` while
    the server returned 200 and a complete HTML body. Any violation here means
    something on the page did not run, whether or not it is visible yet.
    """
    violations = loaded["violations"]
    summary = sorted({f"{v['directive']} <- {v['blocked']}" for v in violations})
    assert not violations, f"{len(violations)} CSP violation(s): {summary}"


def test_no_uncaught_javascript_exception(loaded):
    """`Connection closed.` was the second symptom, from the same cause."""
    assert not loaded["errors"], loaded["errors"]


def test_no_console_errors(loaded):
    errors = [text for kind, text in loaded["console"] if kind == "error"]
    assert not errors, errors[:5]


def test_the_dashboard_actually_renders_its_content(loaded):
    """Not "the body is non-empty" - the specific things this dashboard says.

    A blank page and a rendered page both return 200 and both have a `<body>`.
    What separates them is whether the panels are on screen.
    """
    text = loaded["text"]
    assert len(text.strip()) > 500, f"only {len(text.strip())} characters rendered"
    for expected in ("AutoTrader", "PAPER"):
        assert expected in text, f"{expected!r} missing from the rendered page"


def test_react_has_hydrated(loaded):
    """Server-rendered HTML is inert until React attaches to it.

    The blank page still contained markup; what it did not contain was a React
    fiber, because the script that would have created one never executed.
    """
    hydrated = loaded["page"].evaluate(
        """() => {
            const node = document.body.querySelector('*');
            return !!node && Object.keys(node).some(key => key.startsWith('__react'));
        }"""
    )
    assert hydrated, "React never attached to the server-rendered markup"


def test_the_client_side_poll_reached_the_api(loaded):
    """Hydration that cannot fetch is only half the page working.

    The header timestamp is written by the browser's own poll of
    /api/dashboard/overview, so its presence proves the round trip that fills
    every panel - proxy, frontend rewrite, API and back - completed.
    """
    assert re.search(r"Last sync \d{2}:\d{2}:\d{2}", loaded["text"]), loaded["text"][:200]


def test_no_request_failed(loaded):
    """A blocked script is a failed request; so is a 404 bundle."""
    assert not loaded["failed"], loaded["failed"][:5]


def test_the_static_bundle_loaded(loaded):
    """Chromium is asked what it actually executed, not what the HTML asked for."""
    scripts = loaded["page"].evaluate(
        "() => [...document.querySelectorAll('script[src]')].map(s => s.getAttribute('src'))"
    )
    assert any("/_next/static/" in (src or "") for src in scripts), scripts
    ok = loaded["page"].evaluate(
        """async (sources) => {
            for (const src of sources) {
                const response = await fetch(src, {cache: 'no-store'});
                if (!response.ok) return `${src} -> ${response.status}`;
            }
            return 'ok';
        }""",
        [s for s in scripts if s and s.startswith("/_next/static/")],
    )
    assert ok == "ok", ok


# ---------------------------------------------------------------------------
# The API, through the published hostname
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", API_ROUTES)
def test_each_api_route_reads(loaded, route):
    response = loaded["page"].request.get(f"{URL}/api/dashboard/{route}")
    assert response.status == 200, f"{route} -> {response.status}"
    assert response.json() is not None


@pytest.mark.parametrize("route", API_ROUTES)
def test_each_api_route_is_refused_without_credentials(browser, route):
    context = browser.new_context()
    try:
        assert context.request.get(f"{URL}/api/dashboard/{route}").status == 401
    finally:
        context.close()


@pytest.mark.parametrize("method", WRITE_METHODS)
@pytest.mark.parametrize("path", ["/", "/api/dashboard/overview"])
def test_write_methods_are_rejected_even_with_credentials(loaded, method, path):
    """405 from the proxy, before either upstream is consulted."""
    response = loaded["page"].request.fetch(URL + path, method=method)
    assert response.status == 405, f"{method} {path} -> {response.status}"


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_write_methods_are_refused_without_credentials(browser, method):
    context = browser.new_context()
    try:
        assert context.request.fetch(URL + "/", method=method).status == 401
    finally:
        context.close()


# ---------------------------------------------------------------------------
# Nothing that should not be there
# ---------------------------------------------------------------------------


def test_no_secret_reaches_the_browser(loaded):
    html = loaded["page"].content()
    match = SECRET_SHAPES.search(html)
    assert match is None, f"page carries something credential-shaped: {match.group(0)[:24]!r}"


def test_no_secret_reaches_the_api_responses(loaded):
    for route in API_ROUTES:
        body = loaded["page"].request.get(f"{URL}/api/dashboard/{route}").text()
        match = SECRET_SHAPES.search(body)
        assert match is None, f"{route} carries {match.group(0)[:24]!r}"


@pytest.mark.parametrize("port", PRIVATE_PORTS)
def test_the_loopback_ports_are_not_reachable_from_here(port):
    """The proxy is the only way in; if these answer, it is not."""
    host = urlsplit(URL).hostname
    connection = socket.socket()
    connection.settimeout(5)
    try:
        reachable = connection.connect_ex((host, port)) == 0
    finally:
        connection.close()
    assert not reachable, f"{host}:{port} answered from outside the host"


# ---------------------------------------------------------------------------
# The system behind the page
# ---------------------------------------------------------------------------


def test_the_crypto_runtime_is_healthy(loaded):
    system = loaded["page"].request.get(f"{URL}/api/dashboard/system").json()
    crypto = next(row for row in system["health"] if row["key"] == "runtime_crypto")
    assert crypto["status"] == "RUNNING", crypto
    assert system["system_state"] == "HEALTHY", system["system_state"]
    assert system["reconciliation"]["status"] == "CLEAN", system["reconciliation"]


def test_the_equity_runtime_is_not_running(loaded):
    """The host masks it. A masked unit reports STOPPED and must stay that way."""
    system = loaded["page"].request.get(f"{URL}/api/dashboard/system").json()
    equity = next(row for row in system["health"] if row["key"] == "runtime_equity")
    assert equity["status"] == "STOPPED", equity


def test_the_environment_is_paper(loaded):
    system = loaded["page"].request.get(f"{URL}/api/dashboard/system").json()
    assert system["environment"] == "PAPER", system["environment"]
