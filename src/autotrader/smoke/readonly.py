"""Read-only access to the operational database, and the tracked universe.

**The connection this module hands out cannot write.** It is opened through a
`file:...?mode=ro` URI - SQLite itself refuses writes on that handle - and
`PRAGMA query_only = ON` is set on top, so a stray `UPDATE` fails twice rather
than once. Neither `initialize_database` nor `connect` from
`autotrader.state` is used here: the first applies pending migrations and the
second sets `journal_mode = WAL`, and both are writes. An audit that can
migrate the database it is auditing is not an audit.

Everything above the connection reuses `autotrader.state.sqlite`'s own row
readers. They are plain `SELECT`s, they already know the decimal-text and
UTC-text conventions, and duplicating them here would be a second decoder that
could disagree with the one the runtime uses.

**The universe is resolved, never frozen.** Combined Integration publishes the
twelve tracked symbols - both crypto pairs and the ten equities - as
`execution.models.TRADABLE_SYMBOLS`, and that is what this module now finds.
It is discovered through the same documented probe list as any other location
rather than copied here, so the harness widens and narrows with the system it
audits; the crypto-only `SUPPORTED_SYMBOLS` remains the last-resort fallback,
so an older build still resolves to what it actually knows rather than to a
second, conflicting list.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path

from autotrader.execution.models import SUPPORTED_SYMBOLS
from autotrader.smoke.models import SmokeInputError, StateUnreadableError

#: Comma-separated override, for an operator who knows the universe before the
#: code does. Read for reporting only; it selects what to *look at*, never what
#: to do about it.
UNIVERSE_ENV = "AUTOTRADER_SMOKE_UNIVERSE"

#: Where a future integration may publish its tracked universe, in the order
#: they are tried. `(module, attribute)`. Adding a location here is the single
#: edit Combined Integration should need; see the adaptation notes in
#: `docs/SMOKE_HARNESS.md`.
UNIVERSE_SOURCES: tuple[tuple[str, str], ...] = (
    ("autotrader.universe", "TRACKED_UNIVERSE"),
    ("autotrader.universe", "SUPPORTED_SYMBOLS"),
    ("autotrader.config", "TRACKED_UNIVERSE"),
    # Where Combined Integration actually publishes it: the union of both
    # books, which is the same tuple an `OrderIntent` is validated against and
    # the same one a full-universe reconciliation must cover to clear the
    # shared account halt. Probed rather than copied, so the harness cannot
    # hold a stale twelfth symbol the system has stopped trading.
    ("autotrader.execution.models", "TRADABLE_SYMBOLS"),
)

#: Characters a symbol may contain. Permissive on purpose: `BTC/USD` today,
#: `SPY` and `BRK.B` later. `execution.models.normalize_symbol` is not used
#: because it refuses anything outside the frozen crypto pair list, and this
#: harness must be able to *look at* a symbol it cannot trade.
_SYMBOL_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/.-")

_PRAGMA_QUERY_ONLY = "PRAGMA query_only = ON"
_SELECT_QUERY_ONLY = "PRAGMA query_only"
_SELECT_JOURNAL_MODE = "PRAGMA journal_mode"
_SELECT_TABLE_NAMES = "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
_SELECT_SCHEMA_VERSION = "SELECT schema_version FROM schema_metadata WHERE id = 1"


def normalize_smoke_symbol(symbol: str, field_name: str = "symbol") -> str:
    """Uppercase and validate a symbol this harness may *inspect*.

    Inspection is not trading, so this accepts symbols the execution layer
    would refuse - a future equity ticker, a pair this build does not know.
    What it does not accept is something that is not a symbol at all: an empty
    string, a path, a shell fragment. The generated cleanup command embeds this
    value as text, and a symbol that could carry a quote or a semicolon into a
    line an operator is invited to paste is not acceptable input.
    """
    if not isinstance(symbol, str):
        raise SmokeInputError(f"{field_name} must be a string, got {type(symbol).__name__}.")
    normalized = symbol.strip().upper()
    if not normalized:
        raise SmokeInputError(f"{field_name} must not be empty.")
    illegal = sorted(set(normalized) - _SYMBOL_CHARACTERS)
    if illegal:
        raise SmokeInputError(
            f"{field_name} {symbol!r} contains characters that are not part of a "
            f"symbol: {''.join(illegal)!r}."
        )
    return normalized


def is_crypto_symbol(symbol: str) -> bool:
    """Whether `symbol` names a crypto pair rather than an equity.

    The slash is the discriminator, because it is what the broker's own symbol
    uses: `BTC/USD` is a pair, `SPY` is a ticker. Nothing here consults a
    remembered list of crypto names, so a pair this build has never heard of
    still classifies correctly.
    """
    return "/" in normalize_smoke_symbol(symbol)


def resolve_universe(explicit: Sequence[str] | None = None) -> tuple[str, ...]:
    """The symbols this harness should look at, most specific source first.

    Order: an explicit argument (a `--symbol`/`--universe` option), then
    `AUTOTRADER_SMOKE_UNIVERSE`, then whatever a future integration publishes
    at one of `UNIVERSE_SOURCES`, then `execution.models.SUPPORTED_SYMBOLS`.

    The fallback is an *import*, not a copy. When Combined Integration widens
    the traded universe, the harness widens with it without an edit here, and
    until then it degrades to the two pairs `main` actually knows.

    Duplicates are collapsed and order is preserved, so a report reads in the
    order the operator asked for.
    """
    for candidate in (explicit, _universe_from_env(), _universe_from_integration()):
        if candidate:
            return _dedupe(normalize_smoke_symbol(symbol) for symbol in candidate)
    return _dedupe(normalize_smoke_symbol(symbol) for symbol in SUPPORTED_SYMBOLS)


def universe_source(explicit: Sequence[str] | None = None) -> str:
    """A human-readable note about *where* the universe came from.

    Printed in every report. An operator reading `SPY` in a preflight needs to
    know whether the harness discovered it or was told it, because those two
    situations mean different things about how ready the integration is.
    """
    if explicit:
        return "supplied on the command line"
    if _universe_from_env():
        return f"{UNIVERSE_ENV} environment variable"
    for module_name, attribute in UNIVERSE_SOURCES:
        if _read_attribute(module_name, attribute):
            return f"{module_name}.{attribute}"
    return "autotrader.execution.models.SUPPORTED_SYMBOLS (this build's frozen pairs)"


def load_universe_file(path: Path) -> tuple[str, ...]:
    """Read a universe from a JSON file: a list, or `{"universe": [...]}`.

    Provided so Combined Integration can hand the harness a universe as data
    before it publishes one as code.
    """
    try:
        payload = json.loads(Path(path).read_text())
    except OSError as error:
        raise SmokeInputError(f"Could not read universe file {path}: {error}.") from None
    except json.JSONDecodeError as error:
        raise SmokeInputError(f"Universe file {path} is not valid JSON: {error}.") from None
    if isinstance(payload, dict):
        payload = payload.get("universe", payload.get("symbols"))
    if not isinstance(payload, list) or not payload:
        raise SmokeInputError(
            f"Universe file {path} must hold a non-empty JSON list of symbols, or an "
            'object with a "universe" or "symbols" list.'
        )
    return _dedupe(normalize_smoke_symbol(str(symbol)) for symbol in payload)


def _dedupe(symbols: Iterator[str]) -> tuple[str, ...]:
    """Collapse duplicates, keep first-seen order."""
    return tuple(dict.fromkeys(symbols))


def _universe_from_env() -> tuple[str, ...]:
    raw = os.environ.get(UNIVERSE_ENV, "").strip()
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _universe_from_integration() -> tuple[str, ...]:
    """The first published universe found, or `()` when none exists yet."""
    for module_name, attribute in UNIVERSE_SOURCES:
        found = _read_attribute(module_name, attribute)
        if found:
            return found
    return ()


def _read_attribute(module_name: str, attribute: str) -> tuple[str, ...]:
    """One universe source, or `()`. Never raises for a module that is absent.

    A missing module is the expected case on `main` and is not an error. A
    module that exists but whose attribute is the wrong shape is also ignored
    rather than guessed at - the fallback is a correct universe, and a mangled
    one would not be.
    """
    try:
        module = import_module(module_name)
    except ImportError:
        return ()
    value = getattr(module, attribute, None)
    if not isinstance(value, (list, tuple)) or not value:
        return ()
    if not all(isinstance(symbol, str) and symbol.strip() for symbol in value):
        return ()
    return tuple(str(symbol) for symbol in value)


@contextmanager
def open_readonly(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open `path` read-only, or raise. Never creates, never migrates.

    Two independent guards, because one of them is easy to lose in a later
    edit. The `mode=ro` URI makes SQLite itself reject a write on this handle,
    and `PRAGMA query_only = ON` rejects it a layer earlier. A missing file is
    an error rather than a fresh empty database, which is what plain
    `sqlite3.connect` would silently produce and what would make an audit of a
    mistyped path report a serene, meaningless CLEAN.

    A database left with an unreadable WAL sidecar raises `StateUnreadableError`
    rather than being reopened read-write. Reopening would work, and would also
    apply schema migrations to a file the operator is mid-smoke on.

    One honest caveat about "read-only". Reading a WAL database correctly
    requires the `-shm` coordination file, and SQLite will create `-shm` and an
    empty `-wal` next to the database if they are absent - a reader does this,
    not this code, and it is how every WAL reader behaves. Those two files
    carry no rows: the database itself is not modified, which a test asserts by
    comparing its bytes before and after. The alternative, `immutable=1`, would
    avoid the sidecars by telling SQLite the file cannot change and to ignore
    the WAL entirely - which would silently return stale data whenever a
    runtime had uncommitted pages in flight. Stale numbers in an audit are far
    worse than two empty coordination files, so this opens the database
    properly.
    """
    database = Path(path)
    if not database.exists():
        raise StateUnreadableError(
            f"No operational database at {database}. Nothing was created: this harness "
            "only ever reads, and an empty database would report a meaningless CLEAN."
        )
    uri = f"file:{database.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.OperationalError as error:
        raise StateUnreadableError(
            f"Could not open {database} read-only ({error}). This most often means a "
            "write-ahead log sidecar (-wal/-shm) is present but not readable. The "
            "harness will not reopen it read-write, because that would apply pending "
            "schema migrations to a database an operator is mid-smoke on. Stop the "
            "runtime cleanly, or copy the database and its sidecars aside, then retry."
        ) from None
    try:
        connection.row_factory = sqlite3.Row
        connection.execute(_PRAGMA_QUERY_ONLY)
        yield connection
    finally:
        connection.close()


def is_query_only(connection: sqlite3.Connection) -> bool:
    """Whether `connection` currently refuses writes. Asserted by a test."""
    row = connection.execute(_SELECT_QUERY_ONLY).fetchone()
    return bool(row[0]) if row is not None else False


def journal_mode(connection: sqlite3.Connection) -> str:
    """The journal mode already in force. Reads it; never sets it.

    `PRAGMA journal_mode` with no `=` is a query. The WAL-setting form lives in
    `state.connect`, which this module deliberately does not call.
    """
    row = connection.execute(_SELECT_JOURNAL_MODE).fetchone()
    return str(row[0]).lower() if row is not None else "unknown"


def table_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    """Every table present, sorted. Used to report an unexpected schema."""
    return tuple(str(row["name"]) for row in connection.execute(_SELECT_TABLE_NAMES))


def schema_version(connection: sqlite3.Connection) -> int | None:
    """The recorded schema version, or None when the database has no metadata.

    None rather than an exception: "this file is not an autotrader database"
    is a finding the preflight should print as a blocked check, not a
    traceback. `state.get_schema_version` is not used because it assumes the
    table exists.
    """
    try:
        row = connection.execute(_SELECT_SCHEMA_VERSION).fetchone()
    except sqlite3.DatabaseError:
        return None
    return int(row[0]) if row is not None else None


__all__ = [
    "UNIVERSE_ENV",
    "UNIVERSE_SOURCES",
    "is_crypto_symbol",
    "is_query_only",
    "journal_mode",
    "load_universe_file",
    "normalize_smoke_symbol",
    "open_readonly",
    "resolve_universe",
    "schema_version",
    "table_names",
    "universe_source",
]
