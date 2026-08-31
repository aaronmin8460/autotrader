"""Read-only tick-window acquisition from the venue's historical data host.

Every request is an unauthenticated GET against the venue's *market-data*
host — the same feed family the project's C1 module already reads. The
trading host is never contacted; a test asserts this module names no
mutation endpoint. Windows are cached on the external QA volume with a
provenance sidecar (request parameters, retrieval time, row counts,
SHA-256 of the cached payload), and a cached window is never re-fetched.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

DATA_HOST = "https://data.alpaca.markets"
ALLOWED_PATHS = ("/v1beta3/crypto/us/quotes", "/v1beta3/crypto/us/trades")

PAGE_LIMIT = 10_000
REQUEST_TIMEOUT_S = 30
MAX_RETRIES = 5
RETRY_BACKOFF_S = 3.0
POLITE_PAUSE_S = 0.35

#: Window bounds around the decision instant: 5 minutes of pre-decision
#: quotes, and the longest policy wait (4h, the P4 extension arm) plus a
#: 15 min markout horizon and 15 s slack after it.
PRE_DECISION = timedelta(minutes=5)
POST_DECISION = timedelta(hours=4, minutes=15, seconds=15)


def cache_root() -> Path:
    qa = os.environ.get("AUTOTRADER_QA", "/Volumes/AUTOTRADER_QA")
    return Path(qa) / "datasets" / "crypto-maker-execution" / "ticks-4h"


@dataclass(frozen=True)
class TickWindow:
    symbol: str
    decision_ts: pd.Timestamp
    quotes: pd.DataFrame  # t, bid_price, bid_size, ask_price, ask_size
    trades: pd.DataFrame  # t, price, size, taker_side


def _fetch_paged(path: str, symbol: str, start: str, end: str) -> list[dict]:
    if path not in ALLOWED_PATHS:
        raise ValueError(f"path not allowlisted: {path}")
    kind = path.rsplit("/", 1)[-1]
    rows: list[dict] = []
    token: str | None = None
    while True:
        params = {
            "symbols": symbol,
            "start": start,
            "end": end,
            "limit": str(PAGE_LIMIT),
        }
        if token:
            params["page_token"] = token
        payload = _get_with_retry(DATA_HOST + path, params)
        rows.extend(payload.get(kind, {}).get(symbol, []))
        token = payload.get("next_page_token")
        if not token:
            return rows
        time.sleep(POLITE_PAUSE_S)


def _get_with_retry(url: str, params: dict) -> dict:
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_S)
            if response.status_code == 200:
                return response.json()
            if response.status_code in (429, 500, 502, 503, 504):
                time.sleep(RETRY_BACKOFF_S * (attempt + 1))
                continue
            response.raise_for_status()
        except requests.RequestException as error:  # noqa: PERF203
            last_error = error
            time.sleep(RETRY_BACKOFF_S * (attempt + 1))
    raise RuntimeError(f"gave up after {MAX_RETRIES} attempts: {url} ({last_error})")


def _window_paths(symbol: str, decision_ts: datetime) -> tuple[Path, Path]:
    slug = symbol.replace("/", "_")
    day = decision_ts.strftime("%Y-%m-%dT%H%M")
    directory = cache_root() / slug
    return directory / f"{day}.json.gz", directory / f"{day}.provenance.json"


def _prevailing_quote_raw(symbol: str, decision_ts: datetime) -> list[dict]:
    """The latest quote update at or before the decision, up to 24h back.

    The historical quote record prints *updates*; between updates the
    standing book persists, so the prevailing book at a quiet decision
    instant is the most recent earlier update, however old. Cached beside
    the tick window.
    """
    payload_path, _ = _window_paths(symbol, decision_ts)
    cache = payload_path.with_name(payload_path.name.replace(".json.gz", ".prevailing.json"))
    if cache.exists():
        return json.loads(cache.read_text())
    start = (decision_ts - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = decision_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "symbols": symbol,
        "start": start,
        "end": end,
        "limit": "1",
        "sort": "desc",
    }
    payload = _get_with_retry(DATA_HOST + "/v1beta3/crypto/us/quotes", params)
    rows = payload.get("quotes", {}).get(symbol, [])
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows))
    os.replace(tmp, cache)
    return rows


def fetch_window(symbol: str, decision_ts: datetime) -> TickWindow:
    """Return the cached tick window for an event, fetching it once if absent."""
    payload_path, sidecar_path = _window_paths(symbol, decision_ts)
    if payload_path.exists() and sidecar_path.exists():
        raw = json.loads(gzip.decompress(payload_path.read_bytes()))
        recorded = json.loads(sidecar_path.read_text())
        digest = hashlib.sha256(gzip.decompress(payload_path.read_bytes())).hexdigest()
        if digest != recorded["sha256"]:
            raise RuntimeError(f"cache digest mismatch for {payload_path}")
        raw["prevailing"] = _prevailing_quote_raw(symbol, decision_ts)
        return _to_window(symbol, decision_ts, raw)

    start = (decision_ts - PRE_DECISION).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (decision_ts + POST_DECISION).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = {
        "quotes": _fetch_paged("/v1beta3/crypto/us/quotes", symbol, start, end),
        "trades": _fetch_paged("/v1beta3/crypto/us/trades", symbol, start, end),
    }
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(raw, separators=(",", ":")).encode()
    tmp = payload_path.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(body))
    os.replace(tmp, payload_path)
    sidecar = {
        "symbol": symbol,
        "window_start": start,
        "window_end": end,
        "retrieved_at_utc": datetime.utcnow().isoformat() + "Z",
        "quote_rows": len(raw["quotes"]),
        "trade_rows": len(raw["trades"]),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    tmp_side = sidecar_path.with_suffix(".tmp")
    tmp_side.write_text(json.dumps(sidecar, indent=1))
    os.replace(tmp_side, sidecar_path)
    raw["prevailing"] = _prevailing_quote_raw(symbol, decision_ts)
    return _to_window(symbol, decision_ts, raw)


def _to_window(symbol: str, decision_ts: datetime, raw: dict) -> TickWindow:
    quote_rows = list(raw.get("prevailing", [])) + list(raw["quotes"])
    seen = set()
    deduped = []
    for row in quote_rows:
        if row["t"] not in seen:
            seen.add(row["t"])
            deduped.append(row)
    quotes = pd.DataFrame(deduped)
    if len(quotes):
        quotes = quotes.rename(
            columns={"bp": "bid_price", "bs": "bid_size", "ap": "ask_price", "as": "ask_size"}
        )
        quotes["t"] = pd.to_datetime(quotes["t"], utc=True, format="ISO8601")
        quotes = quotes.sort_values("t", kind="stable").reset_index(drop=True)
    trades = pd.DataFrame(raw["trades"])
    if len(trades):
        trades = trades.rename(columns={"p": "price", "s": "size", "tks": "taker_side"})
        trades["t"] = pd.to_datetime(trades["t"], utc=True, format="ISO8601")
        trades = trades.sort_values("t", kind="stable").reset_index(drop=True)
    return TickWindow(
        symbol=symbol,
        decision_ts=pd.Timestamp(decision_ts),
        quotes=quotes,
        trades=trades,
    )
