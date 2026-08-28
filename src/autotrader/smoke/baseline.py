"""The pre-smoke baseline snapshot: what "before" looked like, as local JSON.

Written by `preflight`, compared by `final-audit`. Its entire purpose is to
remove a manual step from the riskiest moment of the smoke - deciding, after a
BUY and a cleanup SELL, whether exposure is back where it started - by writing
the "before" numbers down while they are still uncontroversial.

**A snapshot is not trading state.** Nothing reads it to make a decision about
an order. If it is missing, stale, or deleted, the final audit still works
against the broker and the database, which are the authorities; the snapshot
only makes the comparison automatic instead of manual.

**It is built from an allowlist, then scanned.** `Baseline.to_payload` names
every field that may be written, one at a time - there is no "dump the object"
path, so a field added upstream cannot appear here by accident. On top of that
`assert_no_secrets` rejects credential-shaped keys and refuses to write a
document containing any value the Alpaca credential variables currently hold.
Broker exceptions are never serialized either: only typed, normalized numbers
reach this module, so a provider error message carrying a header cannot ride
along.

The file is local operational scratch. It belongs under a gitignored directory
or `/tmp`, and it is never committed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from autotrader.execution.models import format_quantity
from autotrader.smoke.health import credential_key_names
from autotrader.smoke.models import SmokeError, SmokeInputError

#: Bumped when the payload shape changes. `read_baseline` refuses a version it
#: does not know rather than guessing at a field that moved.
#:
#: 2 adds the shared account safety state, which Combined Integration made a
#: durable row: one paper account carries both books, so "may anything trade"
#: is part of what "before" looked like.
BASELINE_SCHEMA = 2

#: Default location. Gitignored, alongside the repository, so a snapshot sits
#: next to the smoke it describes and never reaches a commit.
DEFAULT_BASELINE_DIR = Path(".smoke")
DEFAULT_BASELINE_PATH = DEFAULT_BASELINE_DIR / "baseline.json"

#: Environment variables whose *values* must never appear in a snapshot. The
#: values are read only to check for their presence and are never stored,
#: printed, or included in an error message.
_SECRET_ENV_VARS = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")

#: Shortest secret worth scanning for. A one- or two-character credential is
#: not a credential, and matching on one would false-positive on every digit in
#: the document.
_MIN_SECRET_LENGTH = 8


class BaselineError(SmokeError):
    """A snapshot could not be written, read, or trusted."""


@dataclass(frozen=True)
class Baseline:
    """The "before" picture of one paper smoke.

    Quantities are stored as canonical decimal *text*, exactly as the
    operational database stores them, so a round trip through JSON cannot turn
    `0.000166632` into the nearest binary float. The comparison this file
    exists to support is an exact-equality one, and a float would quietly break
    it.
    """

    captured_at: datetime
    universe: tuple[str, ...]
    positions: Mapping[str, Decimal]
    account_equity: float | None = None
    account_cash: float | None = None
    account_status: str | None = None
    git_branch: str | None = None
    git_sha: str | None = None
    git_dirty: bool | None = None
    database_path: str | None = None
    schema_version: int | None = None
    open_order_client_ids: tuple[str, ...] = ()
    unknown_order_client_ids: tuple[str, ...] = ()
    reconciliation_run_id: int | None = None
    reconciliation_status: str | None = None
    reconciliation_safe_to_trade: bool | None = None
    account_safety_state: str | None = None
    account_safety_safe_to_trade: bool | None = None

    def to_payload(self) -> dict[str, object]:
        """The JSON document, field by named field.

        Deliberately not `dataclasses.asdict`. Every key below is written out
        by hand so that adding a field to this class - or to something it is
        built from - cannot silently start writing it to disk.
        """
        return {
            "baseline_schema": BASELINE_SCHEMA,
            "captured_at": self.captured_at.astimezone(UTC).isoformat(),
            "git": {
                "branch": self.git_branch,
                "sha": self.git_sha,
                "dirty": self.git_dirty,
            },
            "account": {
                "equity": self.account_equity,
                "cash": self.account_cash,
                "status": self.account_status,
            },
            "database": {
                "path": self.database_path,
                "schema_version": self.schema_version,
            },
            "universe": list(self.universe),
            "positions": {
                symbol: format_quantity(quantity) for symbol, quantity in self.positions.items()
            },
            "open_order_client_ids": list(self.open_order_client_ids),
            "unknown_order_client_ids": list(self.unknown_order_client_ids),
            "reconciliation": {
                "run_id": self.reconciliation_run_id,
                "status": self.reconciliation_status,
                "safe_to_trade": self.reconciliation_safe_to_trade,
            },
            # The shared halt, recorded separately from the pass above: a
            # per-pass conclusion and the account-wide gate answer different
            # questions, and a smoke needs the "before" answer to both.
            "account_safety": {
                "state": self.account_safety_state,
                "safe_to_trade": self.account_safety_safe_to_trade,
            },
        }

    def quantity_for(self, symbol: str) -> Decimal:
        """The recorded quantity for `symbol`, or zero when it was flat.

        Zero rather than `None`: a symbol absent from a broker's position list
        is flat, and the snapshot records exactly what the broker reported.
        """
        return self.positions.get(symbol.strip().upper(), Decimal(0))


def assert_no_secrets(payload: object) -> None:
    """Raise unless `payload` is free of credentials. Called before every write.

    Two independent checks, because either alone is defeatable. The first
    rejects any key whose *name* looks like a credential, which catches a field
    added upstream without thought. The second serializes the document and
    refuses it if it contains any value the Alpaca credential variables
    currently hold, which catches a secret that arrived under an innocent key.

    The offending value is never included in the exception - only the variable
    whose value was found. An error message that quoted the secret would leak
    it into a terminal, a log, and probably a bug report.
    """
    leaked_keys = credential_key_names(payload)
    if leaked_keys:
        raise BaselineError(
            "Refusing to write a baseline snapshot containing credential-shaped "
            f"field(s): {', '.join(leaked_keys)}. Nothing was written."
        )
    document = json.dumps(payload, sort_keys=True, default=str)
    for variable in _SECRET_ENV_VARS:
        value = os.environ.get(variable, "").strip()
        if len(value) >= _MIN_SECRET_LENGTH and value in document:
            raise BaselineError(
                f"Refusing to write a baseline snapshot: it contains the value of "
                f"{variable}. Nothing was written, and the value is not repeated here."
            )


def write_baseline(baseline: Baseline, path: Path | str = DEFAULT_BASELINE_PATH) -> Path:
    """Serialize `baseline` to `path`, after proving it holds no credentials.

    The scan runs before the file is opened, so a rejected snapshot leaves
    nothing on disk - not a truncated file, not an empty one.
    """
    payload = baseline.to_payload()
    assert_no_secrets(payload)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return destination


def read_baseline(path: Path | str) -> Baseline:
    """Load a snapshot written by `write_baseline`.

    A document from an unknown `baseline_schema` is refused rather than parsed
    optimistically: the audit that reads it compares exposure, and a field that
    moved between versions would produce a confident wrong comparison.
    """
    source = Path(path)
    try:
        payload = json.loads(source.read_text())
    except OSError as error:
        raise BaselineError(f"Could not read baseline snapshot {source}: {error}.") from None
    except json.JSONDecodeError as error:
        raise BaselineError(f"Baseline snapshot {source} is not valid JSON: {error}.") from None
    if not isinstance(payload, dict):
        raise BaselineError(f"Baseline snapshot {source} is not a JSON object.")

    version = payload.get("baseline_schema")
    if version != BASELINE_SCHEMA:
        raise BaselineError(
            f"Baseline snapshot {source} declares schema {version!r}, but this harness "
            f"writes and reads schema {BASELINE_SCHEMA}. Refusing to compare exposure "
            "against a document whose fields may have moved."
        )

    git = _section(payload, "git")
    account = _section(payload, "account")
    database = _section(payload, "database")
    reconciliation = _section(payload, "reconciliation")
    account_safety = _section(payload, "account_safety")
    return Baseline(
        captured_at=_parse_time(payload.get("captured_at"), source),
        universe=_string_tuple(payload.get("universe")),
        positions=_positions(payload.get("positions"), source),
        account_equity=_optional_float(account.get("equity")),
        account_cash=_optional_float(account.get("cash")),
        account_status=_optional_str(account.get("status")),
        git_branch=_optional_str(git.get("branch")),
        git_sha=_optional_str(git.get("sha")),
        git_dirty=git.get("dirty") if isinstance(git.get("dirty"), bool) else None,
        database_path=_optional_str(database.get("path")),
        schema_version=(
            int(database["schema_version"])
            if isinstance(database.get("schema_version"), int)
            else None
        ),
        open_order_client_ids=_string_tuple(payload.get("open_order_client_ids")),
        unknown_order_client_ids=_string_tuple(payload.get("unknown_order_client_ids")),
        reconciliation_run_id=(
            int(reconciliation["run_id"]) if isinstance(reconciliation.get("run_id"), int) else None
        ),
        reconciliation_status=_optional_str(reconciliation.get("status")),
        reconciliation_safe_to_trade=(
            reconciliation.get("safe_to_trade")
            if isinstance(reconciliation.get("safe_to_trade"), bool)
            else None
        ),
        account_safety_state=_optional_str(account_safety.get("state")),
        account_safety_safe_to_trade=(
            account_safety.get("safe_to_trade")
            if isinstance(account_safety.get("safe_to_trade"), bool)
            else None
        ),
    )


def _section(payload: Mapping[str, object], name: str) -> Mapping[str, object]:
    section = payload.get(name)
    return section if isinstance(section, Mapping) else {}


def _positions(raw: object, source: Path) -> dict[str, Decimal]:
    """Decode the quantity map, refusing anything that is not exact text."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise BaselineError(f"Baseline snapshot {source} has a malformed positions map.")
    decoded: dict[str, Decimal] = {}
    for symbol, quantity in raw.items():
        try:
            decoded[str(symbol).strip().upper()] = Decimal(str(quantity))
        except Exception:  # noqa: BLE001 - any unparsable quantity invalidates the file
            raise BaselineError(
                f"Baseline snapshot {source} holds an unreadable quantity for "
                f"{symbol!r}. Refusing to compare exposure against it."
            ) from None
    return decoded


def _parse_time(raw: object, source: Path) -> datetime:
    if not isinstance(raw, str):
        raise BaselineError(f"Baseline snapshot {source} has no captured_at timestamp.")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        raise BaselineError(
            f"Baseline snapshot {source} has an unreadable captured_at timestamp."
        ) from None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _string_tuple(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(str(item) for item in raw)


def _optional_str(raw: object) -> str | None:
    return str(raw) if isinstance(raw, str) and raw.strip() else None


def _optional_float(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def default_baseline_path(repo: Path | str | None = None) -> Path:
    """Where a snapshot goes when the operator names no path."""
    if repo is None:
        return DEFAULT_BASELINE_PATH
    return Path(repo) / DEFAULT_BASELINE_PATH


def require_existing(path: Path | str) -> Path:
    """A baseline path that exists, or a message saying which one did not."""
    candidate = Path(path)
    if not candidate.exists():
        raise SmokeInputError(
            f"No baseline snapshot at {candidate}. Run the preflight with "
            "--write-baseline before the smoke, or pass --no-baseline to audit "
            "without a comparison."
        )
    return candidate


__all__ = [
    "BASELINE_SCHEMA",
    "DEFAULT_BASELINE_DIR",
    "DEFAULT_BASELINE_PATH",
    "Baseline",
    "BaselineError",
    "assert_no_secrets",
    "default_baseline_path",
    "read_baseline",
    "require_existing",
    "write_baseline",
]
