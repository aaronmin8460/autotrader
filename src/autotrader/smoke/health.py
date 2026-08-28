"""Liveness reads: runtime checkpoints, and the optional dashboard endpoint.

Two things that report whether a component is alive, and neither of them can
start, stop, or nudge one. `runtime_health` reads the durable per-symbol
checkpoint rows the runtime commits; `dashboard_health` performs a single
HTTP `GET`.

**The dashboard is never a gate.** It is a view of state, not the state. If it
is unreachable, the broker still answers and the database still answers, and
those are what a cleanup decision is made from - so an unavailable dashboard is
reported and nothing is blocked on it. Only a dashboard that answers and leaks
a credential field is a finding worth acting on, and that is reported as one.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from autotrader.smoke.models import DashboardHealth, Freshness, RuntimeHealth
from autotrader.smoke.readonly import normalize_smoke_symbol
from autotrader.state import sqlite as state

#: How old a checkpoint may be before it is called stale. Three 15-minute bars:
#: long enough that one slow provider cycle is not an alarm, short enough that a
#: runtime which died an hour ago is not called healthy.
DEFAULT_STALE_AFTER = timedelta(minutes=45)

#: Only `http` and `https` are fetched. A `file://` URL handed to `urlopen`
#: would read the local disk, which is not a health check.
_ALLOWED_SCHEMES = ("http://", "https://")

#: Key names that must never appear in a health payload. Matched on the key,
#: case-insensitively, anywhere in it - `alpaca_api_key` and `Authorization`
#: both trip it.
_CREDENTIAL_KEY_PATTERN = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|credential|authorization|bearer|private[_-]?key)",
    re.IGNORECASE,
)

_DEFAULT_TIMEOUT_SECONDS = 5.0


def runtime_health(
    connection: sqlite3.Connection,
    universe: Sequence[str],
    *,
    now: datetime | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> tuple[RuntimeHealth, ...]:
    """Freshness of each symbol's durable checkpoint. Reads; starts nothing.

    Every symbol in `universe` gets a row, including one the runtime has never
    written - reported as `NOT_RECORDED` rather than omitted, because a symbol
    silently missing from a health report reads as fine.

    Age is measured from `updated_at`, the moment the row was written, not from
    the bar it names. A runtime processing an old bar is a data problem; a
    runtime that has not written for an hour is a liveness problem, and this
    function answers the second.

    Equity symbols go stale outside market hours by design. The age is reported
    rather than interpreted; the caller says so in its output instead of
    encoding a session calendar this build does not have.
    """
    moment = now or datetime.now(UTC)
    stored = {
        checkpoint.symbol: checkpoint for checkpoint in state.list_runtime_checkpoints(connection)
    }
    results: list[RuntimeHealth] = []
    for symbol in universe:
        ticker = normalize_smoke_symbol(symbol)
        checkpoint = stored.get(ticker)
        if checkpoint is None:
            results.append(
                RuntimeHealth(
                    symbol=ticker,
                    freshness=Freshness.NOT_RECORDED,
                    last_processed_bar=None,
                    updated_at=None,
                    age_seconds=None,
                )
            )
            continue
        age = (moment - checkpoint.updated_at).total_seconds()
        results.append(
            RuntimeHealth(
                symbol=ticker,
                freshness=(
                    Freshness.FRESH if age <= stale_after.total_seconds() else Freshness.STALE
                ),
                last_processed_bar=checkpoint.last_processed_bar_timestamp,
                updated_at=checkpoint.updated_at,
                age_seconds=age,
            )
        )
    return tuple(results)


def dashboard_health(
    url: str | None, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS
) -> DashboardHealth:
    """One `GET` against a dashboard health endpoint. Optional, never a gate.

    Reports availability, the HTTP status, whether the body parsed as JSON, the
    top-level keys it contained, and any key whose *name* looks like a
    credential. The values behind such keys are never read, never printed, and
    never returned - naming the field is enough for an operator to go and fix
    it, and echoing the value would copy a leak into the terminal and into this
    harness's own output.

    A `None` URL means the check was not requested and is reported as such.
    """
    if not url:
        return DashboardHealth(
            available=False,
            url=None,
            status_code=None,
            detail="No dashboard URL was supplied, so no dashboard check was performed.",
        )
    if not url.startswith(_ALLOWED_SCHEMES):
        return DashboardHealth(
            available=False,
            url=url,
            status_code=None,
            detail=(
                "A dashboard URL must start with http:// or https://. Nothing was "
                "fetched: other schemes would read the local filesystem rather than "
                "check a service."
            ),
        )
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - scheme checked
            status = int(getattr(response, "status", 0) or 0)
            body = response.read()
    except urllib.error.HTTPError as error:
        return DashboardHealth(
            available=False,
            url=url,
            status_code=int(error.code),
            detail=(
                f"The dashboard answered HTTP {error.code}. This does not block broker "
                "verification: the broker and the database are the authorities on "
                "positions and orders."
            ),
        )
    except Exception as error:  # noqa: BLE001 - any transport failure is "unavailable"
        return DashboardHealth(
            available=False,
            url=url,
            status_code=None,
            detail=(
                f"The dashboard could not be reached ({type(error).__name__}). This does "
                "not block broker verification."
            ),
        )

    if status < 200 or status >= 300:
        return DashboardHealth(
            available=False,
            url=url,
            status_code=status,
            detail=f"The dashboard answered HTTP {status}, which is not a success.",
        )
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return DashboardHealth(
            available=False,
            url=url,
            status_code=status,
            detail=(
                f"The dashboard answered HTTP {status} but the body was not valid JSON. "
                "Treating a malformed payload as healthy would hide a broken endpoint."
            ),
        )

    keys = tuple(str(key) for key in payload) if isinstance(payload, dict) else ()
    leaked = credential_key_names(payload)
    detail = f"HTTP {status}, JSON payload with {len(keys)} top-level field(s)."
    if leaked:
        detail += (
            " CREDENTIAL-SHAPED FIELD(S) PRESENT: "
            + ", ".join(leaked)
            + ". The values were not read or printed. Fix the endpoint before exposing it."
        )
    return DashboardHealth(
        available=True,
        url=url,
        status_code=status,
        detail=detail,
        payload_keys=keys,
        credential_fields=leaked,
    )


def credential_key_names(payload: object, *, prefix: str = "") -> tuple[str, ...]:
    """Every key path in `payload` whose *name* looks like a credential.

    Walks nested dictionaries and lists, because a secret one level down is
    still a secret. Only key names are collected; values are never inspected,
    so nothing sensitive can reach the return value even by accident.
    """
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if _CREDENTIAL_KEY_PATTERN.search(str(key)):
                found.append(path)
            found.extend(credential_key_names(value, prefix=path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(credential_key_names(value, prefix=f"{prefix}[{index}]"))
    return tuple(found)


__all__ = [
    "DEFAULT_STALE_AFTER",
    "credential_key_names",
    "dashboard_health",
    "runtime_health",
]
