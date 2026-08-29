"""Smoke-harness tests: the read-only guarantees, and the arithmetic that matters.

**Nothing here touches the network.** The only thing faked is the Alpaca
boundary, the fakes return *real* alpaca-py models so normalization runs
against real response shapes, no real credential is read, and a test asserts
sockets stay shut.

The fake client's submitting methods do not merely record a call - they raise.
A harness that placed an order would fail loudly here rather than quietly pass
a count assertion someone later deleted.

The tests that matter most are the structural ones and the fee-adjustment one.
The structural tests assert what this package *cannot do*: no order-submission
surface, no process except `git`, no database connection that can write. The
fee-adjustment test pins the real observed case - a BUY of 0.00016705 BTC that
settled as a position of 0.000166632 BTC - and requires the cleanup planner to
size from the second number. That is a deterministic fixture. No order, real or
paper, is placed by this file.
"""

from __future__ import annotations

import ast
import inspect
import json
import socket
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import (
    AccountStatus,
    AssetClass,
    OrderClass,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.models import Order, Position, TradeAccount
from typer.testing import CliRunner

from autotrader import smoke
from autotrader.execution.models import TRADABLE_SYMBOLS
from autotrader.execution.paper import CryptoAssetSpec
from autotrader.smoke import audit as audit_module
from autotrader.smoke import baseline as baseline_module
from autotrader.smoke import broker as broker_module
from autotrader.smoke import cleanup as cleanup_module
from autotrader.smoke import gitinfo as gitinfo_module
from autotrader.smoke import health as health_module
from autotrader.smoke import inspector as inspector_module
from autotrader.smoke import preflight as preflight_module
from autotrader.smoke import readonly as readonly_module
from autotrader.smoke import tracking as tracking_module
from autotrader.smoke.baseline import Baseline, BaselineError, read_baseline, write_baseline
from autotrader.smoke.broker import LookupOutcome
from autotrader.smoke.cleanup import CleanupPlanError, plan_cleanup
from autotrader.smoke.cli import app
from autotrader.smoke.gitinfo import GitState
from autotrader.smoke.models import (
    BLOCKED,
    DO_NOT_RETRY_BANNER,
    EXPOSURE_NOT_RESTORED,
    EXPOSURE_RESTORED,
    ORDER_TRUTH_UNRESOLVED,
    READY_FOR_PAPER_SMOKE,
    SMOKE_COMPLETE,
    SMOKE_INCOMPLETE,
    CleanupVerdict,
    PositionSnapshot,
    SmokeVerdict,
    StateUnreadableError,
)
from autotrader.smoke.readonly import open_readonly
from autotrader.state.sqlite import (
    ACCOUNT_SAFETY_SAFE,
    ACCOUNT_SAFETY_UNSAFE_RECONCILIATION,
    ACCOUNT_SAFETY_UNSAFE_UNKNOWN,
    INTENT_STATUS_CONFIRMED_NOT_SUBMITTED,
    INTENT_STATUS_SUBMITTED,
    INTENT_STATUS_UNKNOWN,
    RECONCILIATION_STATUS_CLEAN,
    RECONCILIATION_STATUS_REPAIRED,
    RECONCILIATION_STATUS_UNRESOLVED,
    SCHEMA_VERSION,
    connect,
    initialize_database,
    record_order_intent,
    record_reconciliation_run,
    set_account_safety_state,
    upsert_broker_order,
    upsert_runtime_checkpoint,
)

T0 = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
BTC = "BTC/USD"
ETH = "ETH/USD"
SPY = "SPY"
BROKER_ORDER_UUID = "8f1c4d2e-0000-4000-8000-000000000001"

#: The real observed regression. A BUY of this many BTC settled as the position
#: below once Alpaca took its taker fee out of the base asset.
ORDERED_BTC = Decimal("0.00016705")
SETTLED_BTC = Decimal("0.000166632")

#: Alpaca's published BTC/USD constraints at the time of writing. Used as a
#: deterministic fixture; the harness itself reads them from the broker on
#: every call and remembers nothing.
BTC_INCREMENT = Decimal("0.000000001")
BTC_MIN_ORDER = Decimal("0.000000001")

runner = CliRunner()


# --------------------------------------------------------------------------
# Source-level helpers
# --------------------------------------------------------------------------


def code_without_prose(source: str) -> str:
    """`source` with every docstring removed.

    The guarantees below are about *executable code*. This package's own
    documentation explains at length what it must never do, so a naive
    substring scan would trip over the very sentences that state the rule.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def package_root() -> Path:
    return Path(smoke.__file__).resolve().parent


def package_code() -> dict[str, str]:
    """Every module in the smoke package, prose stripped."""
    root = package_root()
    return {
        str(path.relative_to(root)): code_without_prose(path.read_text())
        for path in sorted(root.rglob("*.py"))
    }


def package_source() -> dict[str, str]:
    """Every module in the smoke package, verbatim."""
    root = package_root()
    return {str(path.relative_to(root)): path.read_text() for path in sorted(root.rglob("*.py"))}


def imported_modules(source: str) -> set[str]:
    """Top-level package names imported by `source`."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


# --------------------------------------------------------------------------
# Alpaca test doubles. The models are real; only the transport is faked.
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeHTTPError:
    def __init__(self, status_code: int) -> None:
        self.response = _FakeResponse(status_code)


def api_error(status_code: int | None, message: str = "broker said no") -> APIError:
    """An `APIError` shaped like the SDK's, optionally without a readable status."""
    body = json.dumps({"code": 40010001, "message": message})
    if status_code is None:
        return APIError(body)
    return APIError(body, _FakeHTTPError(status_code))


def not_found() -> APIError:
    """The broker's definitive "no order under this key"."""
    return api_error(404, "order not found")


def timeout_error() -> APIError:
    """An ambiguous answer: the question could not be answered at all."""
    return api_error(504, "gateway timeout")


def make_account(
    *,
    equity: str = "100000",
    cash: str = "100000",
    status: AccountStatus = AccountStatus.ACTIVE,
    trading_blocked: bool = False,
    account_blocked: bool = False,
    trade_suspended_by_user: bool = False,
) -> TradeAccount:
    return TradeAccount(
        id=uuid4(),
        account_number="PA0000000000",
        status=status,
        equity=equity,
        cash=cash,
        trading_blocked=trading_blocked,
        account_blocked=account_blocked,
        trade_suspended_by_user=trade_suspended_by_user,
    )


def make_position(
    symbol: str = BTC,
    *,
    qty: str = "0.000166632",
    market_value: str = "16.66",
    avg_entry_price: str = "100000",
    side: PositionSide = PositionSide.LONG,
    asset_class: AssetClass = AssetClass.CRYPTO,
) -> Position:
    return Position(
        asset_id=uuid4(),
        symbol=symbol,
        exchange="CRYPTO" if asset_class is AssetClass.CRYPTO else "NASDAQ",
        asset_class=asset_class,
        avg_entry_price=avg_entry_price,
        qty=qty,
        side=side,
        cost_basis=str(Decimal(qty) * Decimal(avg_entry_price)),
        market_value=market_value,
    )


def make_order(
    *,
    client_order_id: str,
    symbol: str = BTC,
    qty: str | None = "0.00016705",
    filled_qty: str = "0",
    filled_avg_price: str | None = None,
    status: OrderStatus = OrderStatus.ACCEPTED,
    side: AlpacaOrderSide = AlpacaOrderSide.BUY,
    order_id: str = BROKER_ORDER_UUID,
    filled_at: datetime | None = None,
) -> Order:
    return Order(
        id=order_id,
        client_order_id=client_order_id,
        created_at=T0,
        updated_at=T0,
        submitted_at=T0,
        filled_at=filled_at,
        order_class=OrderClass.SIMPLE,
        time_in_force=TimeInForce.GTC,
        status=status,
        extended_hours=False,
        symbol=symbol,
        qty=qty,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        side=side,
        order_type=OrderType.MARKET,
        type=OrderType.MARKET,
    )


class FakeBrokerClient:
    """A broker that answers reads and refuses every write.

    `orders` maps an identifier - a `client_order_id` or a broker order id - to
    what the broker answers with: an `Order` or an exception to raise. A key
    that is absent answers with Alpaca's 404, the definitive "no such order".

    The submitting methods raise rather than record. If this harness ever grows
    a call to one, these tests fail with a message that says what happened,
    instead of a silent count assertion that a later edit could delete.
    """

    def __init__(
        self,
        *,
        account: TradeAccount | BaseException | None = None,
        positions: list[Position] | BaseException | None = None,
        orders: dict[str, object] | None = None,
    ) -> None:
        self._account = account if account is not None else make_account()
        self._positions = positions if positions is not None else []
        self._orders = dict(orders or {})
        self.lookup_calls: list[str] = []
        self.account_calls = 0
        self.position_calls = 0

    def get_account(self) -> TradeAccount:
        self.account_calls += 1
        if isinstance(self._account, BaseException):
            raise self._account
        return self._account

    def get_all_positions(self) -> list[Position]:
        self.position_calls += 1
        if isinstance(self._positions, BaseException):
            raise self._positions
        return list(self._positions)

    def _answer(self, identifier: str) -> Order:
        self.lookup_calls.append(identifier)
        answer = self._orders.get(identifier, not_found())
        if isinstance(answer, BaseException):
            raise answer
        return answer  # type: ignore[return-value]

    def get_order_by_client_id(self, client_id: str) -> Order:
        return self._answer(client_id)

    def get_order_by_id(self, order_id: str) -> Order:
        return self._answer(order_id)

    # Everything below must never be reached from this package.

    def submit_order(self, order_data: object) -> Order:
        raise AssertionError("the smoke harness must never submit an order")

    def cancel_order_by_id(self, order_id: str) -> None:
        raise AssertionError("the smoke harness must never cancel an order")

    def close_position(self, symbol: str, close_options: object = None) -> Order:
        raise AssertionError("the smoke harness must never close a position")

    def replace_order_by_id(self, order_id: str, order_data: object = None) -> Order:
        raise AssertionError("the smoke harness must never replace an order")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return initialize_database(tmp_path / "state.db")


@pytest.fixture
def writer(database_path: Path):
    """A writable connection, for *seeding* fixtures only.

    The harness never gets one of these. Every assertion below reads through
    `open_readonly`, so a test cannot accidentally prove the harness works by
    handing it a connection it would never be given in production.
    """
    with connect(database_path) as connection:
        yield connection


@pytest.fixture
def reader(database_path: Path):
    with open_readonly(database_path) as connection:
        yield connection


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-never-real")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret-never-real")


@pytest.fixture(autouse=True)
def no_inherited_universe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own `AUTOTRADER_SMOKE_UNIVERSE` must not steer the suite."""
    monkeypatch.delenv(readonly_module.UNIVERSE_ENV, raising=False)


CLEAN_GIT = GitState(branch="main", sha="a" * 40, dirty=False, detail="clean")
DIRTY_GIT = GitState(branch="main", sha="a" * 40, dirty=True, detail="dirty")


def crypto_asset(
    symbol: str = BTC,
    *,
    min_order_size: Decimal = BTC_MIN_ORDER,
    min_trade_increment: Decimal = BTC_INCREMENT,
) -> CryptoAssetSpec:
    return CryptoAssetSpec(
        symbol=symbol,
        asset_class="crypto",
        status="active",
        tradable=True,
        fractionable=True,
        min_order_size=min_order_size,
        min_trade_increment=min_trade_increment,
    )


def seed_account_safety(
    connection: sqlite3.Connection,
    *,
    safe: bool = True,
    client_order_id: str | None = None,
    now: datetime | None = None,
) -> None:
    """Set the shared account halt the way the system itself would."""
    set_account_safety_state(
        connection,
        account_state=ACCOUNT_SAFETY_SAFE if safe else ACCOUNT_SAFETY_UNSAFE_UNKNOWN,
        reason="seeded by the test suite",
        source="reconciliation" if safe else "crypto-runtime",
        client_order_id=client_order_id,
        updated_at=now or datetime.now(UTC),
    )


def seed_reconciliation(
    connection: sqlite3.Connection,
    *,
    status: str = RECONCILIATION_STATUS_CLEAN,
    safe: bool = True,
    completed_at: datetime | None = None,
    account_safe: bool | None = None,
) -> int:
    """Record a finished pass, and move the shared halt the way it would.

    The two are coupled here because they are coupled in the system:
    `account.safety.apply_reconciliation_result` is the one place a completed
    full-universe pass moves the account-wide gate. A fixture that recorded a
    green pass while leaving the account halted would be describing a state the
    runtime cannot actually produce, and every preflight built on it would be
    testing against fiction.

    `account_safe` decouples them for the tests that need the pass and the gate
    to disagree - which is a real state: a green pass narrower than the tracked
    universe reports honestly and leaves an existing halt exactly where it was.
    """
    moment = completed_at or datetime.now(UTC)
    run_id = record_reconciliation_run(
        connection,
        started_at=moment - timedelta(seconds=2),
        completed_at=moment,
        status=status,
        safe_to_trade=safe,
        orders_checked=1,
        positions_checked=2,
        issues_count=0 if safe else 1,
        unresolved_count=0 if safe else 1,
    )
    seed_account_safety(connection, safe=safe if account_safe is None else account_safe, now=moment)
    return run_id


def seed_intent(
    connection: sqlite3.Connection,
    *,
    client_order_id: str = "autotrader-buy-1",
    symbol: str = BTC,
    side: str = "BUY",
    status: str = INTENT_STATUS_SUBMITTED,
    quantity: Decimal = ORDERED_BTC,
    created_at: datetime | None = None,
) -> int:
    return record_order_intent(
        connection,
        client_order_id=client_order_id,
        created_at=created_at or T0,
        symbol=symbol,
        side=side,
        requested_quantity=quantity,
        approved_quantity=quantity,
        reference_price=100000.0,
        risk_reason_code="APPROVED",
        status=status,
    )


def seed_broker_order(
    connection: sqlite3.Connection,
    intent_id: int,
    *,
    client_order_id: str = "autotrader-buy-1",
    broker_order_id: str = BROKER_ORDER_UUID,
    symbol: str = BTC,
    side: str = "BUY",
    quantity: Decimal = ORDERED_BTC,
    filled_quantity: Decimal = ORDERED_BTC,
    status: str = "filled",
) -> int:
    return upsert_broker_order(
        connection,
        order_intent_id=intent_id,
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        filled_quantity=filled_quantity,
        status=status,
        updated_at=T0,
        submitted_at=T0,
    )


# --------------------------------------------------------------------------
# Safety: the harness has no order-submission surface
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    [
        "submit_order",
        "submit_order_intent",
        "execute_paper_order",
        "build_market_order_request",
        "MarketOrderRequest",
        "OrderRequest",
        "cancel_order",
        "cancel_order_by_id",
        "close_position",
        "close_all_positions",
        "replace_order",
        "liquidate",
        "paper=False",
        "require_confirmation",
        "require_paper_trading_enabled",
    ],
)
def test_smoke_harness_has_no_order_submission_surface(forbidden: str) -> None:
    """The whole promise of this package, asserted against executable code.

    Docstrings are stripped first, so this means the construct is absent from
    the code rather than merely unmentioned in the prose.
    """
    for name, code in package_code().items():
        assert forbidden not in code, f"{forbidden} found in smoke/{name}"


def test_smoke_harness_imports_no_broker_sdk() -> None:
    """No module here imports Alpaca. The broker is reached through one seam.

    `autotrader.execution.paper` is the only file in the repository that
    constructs a client or submits an order. This package imports the reading
    half of it and nothing else, which is why "this package cannot trade" is
    answerable by reading its import list.
    """
    for name, source in package_source().items():
        assert not any(module.startswith("alpaca") for module in imported_modules(source)), (
            f"smoke/{name} imports the Alpaca SDK"
        )


def test_only_gitinfo_may_start_a_process() -> None:
    """The command-generating modules cannot execute anything.

    `cleanup` renders a `paper-submit` line for a human to type. This is what
    makes "generating it is not running it" a structural fact: the module that
    produces the string has no way to start a process, and the one module that
    can start a process takes no command line.
    """
    process_modules = {"subprocess", "multiprocessing", "pty", "os2emxpath", "commands"}
    process_calls = ("os.system", "os.popen", "os.exec", "os.spawn", "os.fork", "os.posix_spawn")
    for name, source in package_source().items():
        if name == "gitinfo.py":
            continue
        leaked = imported_modules(source) & process_modules
        assert not leaked, f"smoke/{name} imports {leaked}"
        code = code_without_prose(source)
        for token in process_calls:
            assert token not in code, f"{token} found in smoke/{name}"


def test_gitinfo_runs_only_git_and_never_a_shell() -> None:
    """The one module that spawns a process may spawn exactly one program.

    Every call is checked against the parsed source rather than the text: the
    first argument must be a list literal whose first element is the constant
    `git`, and `shell=True` must not appear anywhere.
    """
    source = Path(gitinfo_module.__file__).read_text()
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert calls, "gitinfo must actually invoke subprocess, or this test proves nothing"
    for call in calls:
        argv = call.args[0]
        assert isinstance(argv, ast.List), "the argument list must be a literal, never a string"
        first = argv.elts[0]
        assert isinstance(first, ast.Constant) and first.value == "git", ast.dump(first)
        for keyword in call.keywords:
            assert keyword.arg != "shell", "gitinfo must never run through a shell"


def test_gitinfo_never_names_an_autotrader_command() -> None:
    """The module that can run a program has no knowledge of one to run."""
    source = Path(gitinfo_module.__file__).read_text()
    assert "paper-submit" not in source
    assert "autotrader" not in code_without_prose(source)


def test_the_generated_command_is_data_and_never_reaches_a_process() -> None:
    """`paper-submit` appears in exactly the modules that print it.

    It is a string built for a human. The modules that build or display it are
    also the modules proven above to have no way to start a process, so there
    is nowhere for it to go except the terminal.
    """
    producing = {name for name, source in package_source().items() if "paper-submit" in source}
    assert producing <= {"cleanup.py", "cli.py", "__init__.py"}, producing
    for name in producing:
        assert "subprocess" not in package_code()[name]


def declared_cli_options() -> set[str]:
    """Every `--flag` this CLI actually declares, read off the parsed source.

    Taken from the first string argument of each `typer.Option(...)` call, so
    an option added under an innocent Python name is still caught by the flag
    it exposes.
    """
    tree = ast.parse(package_root().joinpath("cli.py").read_text())
    flags: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Option"
        ):
            flags.update(
                argument.value
                for argument in node.args
                if isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and argument.value.startswith("--")
            )
    return flags


def test_the_cli_declares_no_execution_option() -> None:
    """No `--execute`, no `--yes`, no `--auto-cleanup`. Absent, not refused.

    Asserted against the options the CLI *declares* rather than against its
    text, so the help that explains which gates an operator must open for
    themselves does not read as an option this program accepts.
    """
    declared = declared_cli_options()
    assert declared, "the CLI must declare options, or this test proves nothing"
    for flag in ("--execute", "--yes", "--auto-cleanup", "--confirm-paper", "--force", "--submit"):
        assert flag not in declared, f"{flag} must not exist in the harness CLI"


def test_no_cli_command_takes_a_confirmation_parameter() -> None:
    """The same guarantee from the other side: the callbacks' own signatures."""
    forbidden = {"execute", "yes", "auto_cleanup", "confirm_paper", "confirm", "force", "submit"}
    for command in app.registered_commands:
        assert command.callback is not None
        parameters = set(inspect.signature(command.callback).parameters)
        assert not (parameters & forbidden), f"{command.callback.__name__}: {parameters}"


def test_the_harness_sets_no_environment_variable() -> None:
    """No hidden environment variable can enable execution, because none is set.

    The gate variable is *read* in one place, to report whether it is open.
    Nothing here assigns to `os.environ`, calls `setenv`, or calls `putenv`.
    """
    for name, code in package_code().items():
        assert "os.environ[" not in code, f"smoke/{name} assigns into os.environ"
        assert "putenv" not in code, f"smoke/{name} calls putenv"
        assert "setenv" not in code, f"smoke/{name} calls setenv"


def test_the_harness_needs_no_credentials_to_read_state(
    reader: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Database and planning work with the credential variables removed."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    assert tracking_module.open_intents(reader) == ()
    assert broker_module.credentials_present() is False
    plan = plan_cleanup(
        position=PositionSnapshot(symbol=BTC, quantity=SETTLED_BTC, market_value=16.66),
        asset=crypto_asset(),
        quoted_price=100000.0,
    )
    assert plan.verdict is CleanupVerdict.REQUIRED


def test_the_harness_opens_no_socket(
    reader: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything below the broker boundary runs with sockets blocked."""

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the smoke harness must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

    client = FakeBrokerClient(positions=[make_position()])
    report = preflight_module.run_preflight(
        client=client,
        connection=reader,
        database_path=Path("state.db"),
        git=CLEAN_GIT,
        universe=(BTC, ETH),
        universe_origin="test",
    )
    assert report.positions[BTC].quantity == SETTLED_BTC


# --------------------------------------------------------------------------
# Read-only state access
# --------------------------------------------------------------------------


def test_the_audit_connection_cannot_write(reader: sqlite3.Connection) -> None:
    """Both guards, checked: `query_only` is on and a write actually fails."""
    assert readonly_module.is_query_only(reader) is True
    with pytest.raises(sqlite3.OperationalError):
        reader.execute("UPDATE schema_metadata SET schema_version = 99 WHERE id = 1")


def test_opening_a_missing_database_creates_nothing(tmp_path: Path) -> None:
    """A mistyped path must not become a fresh, serene, meaningless CLEAN."""
    missing = tmp_path / "not-there.db"
    with pytest.raises(StateUnreadableError), open_readonly(missing):
        pass
    assert not missing.exists()


def test_reading_state_does_not_migrate_or_change_journal_mode(
    database_path: Path, reader: sqlite3.Connection
) -> None:
    """An audit must not alter the database it audits."""
    before = database_path.read_bytes()
    assert readonly_module.journal_mode(reader) == "wal"
    assert readonly_module.schema_version(reader) == SCHEMA_VERSION
    assert len(readonly_module.table_names(reader)) >= 12
    assert database_path.read_bytes() == before


def test_reading_a_wal_database_leaves_its_rows_byte_identical(
    database_path: Path, writer: sqlite3.Connection
) -> None:
    """The database is untouched; only SQLite's own sidecars may appear.

    Observed during a real read-only run against the live paper database: a
    `-shm` and an empty `-wal` appeared beside it. That is what any WAL reader
    does and it writes no rows - but "read-only" is a safety claim, so the part
    that matters is asserted directly rather than assumed.
    """
    seed_reconciliation(writer)
    seed_intent(writer)
    write_ahead_log = Path(f"{database_path}-wal")
    before = database_path.read_bytes()
    wal_before = write_ahead_log.stat().st_size if write_ahead_log.exists() else 0

    with open_readonly(database_path) as reader:
        assert readonly_module.schema_version(reader) == SCHEMA_VERSION
        assert len(tracking_module.intents_for_symbol(reader, BTC)) == 1
        assert tracking_module.latest_reconciliation(reader) is not None

    assert database_path.read_bytes() == before
    wal_after = write_ahead_log.stat().st_size if write_ahead_log.exists() else 0
    assert wal_after == wal_before, "a reader must not append to the write-ahead log"


def test_the_harness_never_calls_the_writing_connection_helper() -> None:
    """`state.connect` sets `journal_mode = WAL`, which is a write.

    `initialize_database` additionally applies migrations. Neither belongs in a
    read-only audit, and neither is reachable from this package.
    """
    for name, code in package_code().items():
        assert "initialize_database" not in code, f"smoke/{name}"
        assert "state.connect" not in code, f"smoke/{name}"


# --------------------------------------------------------------------------
# Universe resolution
# --------------------------------------------------------------------------


def test_the_universe_is_the_combined_twelve_symbol_book() -> None:
    """Discovered from the integrated build, never copied into this package.

    Combined Integration publishes the union of both books as
    `execution.models.TRADABLE_SYMBOLS` - the same tuple an `OrderIntent` is
    validated against, and the same one a full-universe reconciliation has to
    cover before it may clear the shared account halt. The harness finds it
    through the documented probe list, so widening or narrowing the traded
    universe moves every preflight, audit and checkpoint report with it and
    needs no edit here.
    """
    assert readonly_module.resolve_universe() == TRADABLE_SYMBOLS
    assert len(TRADABLE_SYMBOLS) == 12
    assert set(TRADABLE_SYMBOLS) >= {BTC, ETH, SPY}
    assert readonly_module.universe_source() == "autotrader.execution.models.TRADABLE_SYMBOLS"


def test_an_explicit_universe_wins_over_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(readonly_module.UNIVERSE_ENV, "ETH/USD")
    assert readonly_module.resolve_universe(["spy", "QQQ", "SPY"]) == (SPY, "QQQ")
    assert readonly_module.universe_source(["SPY"]) == "supplied on the command line"


def test_the_environment_universe_is_used_when_nothing_is_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(readonly_module.UNIVERSE_ENV, "SPY, QQQ ,SPY")
    assert readonly_module.resolve_universe() == (SPY, "QQQ")


def test_a_future_integration_universe_is_imported_not_reinvented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Combined Integration publishes a universe; the harness widens with it.

    Simulated by pointing the documented source list at a module that exists in
    this build. The point is that discovery happens by import rather than by a
    second hardcoded list.
    """
    monkeypatch.setattr(
        readonly_module, "UNIVERSE_SOURCES", (("autotrader.smoke.readonly", "_FAKE_UNIVERSE"),)
    )
    monkeypatch.setattr(readonly_module, "_FAKE_UNIVERSE", ["SPY", "QQQ", BTC], raising=False)

    assert readonly_module.resolve_universe() == (SPY, "QQQ", BTC)
    assert "_FAKE_UNIVERSE" in readonly_module.universe_source()


def test_a_malformed_published_universe_is_ignored_rather_than_guessed_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        readonly_module, "UNIVERSE_SOURCES", (("autotrader.smoke.readonly", "_FAKE_UNIVERSE"),)
    )
    monkeypatch.setattr(readonly_module, "_FAKE_UNIVERSE", ["SPY", 7, None], raising=False)

    assert readonly_module.resolve_universe() == (BTC, ETH)


def test_a_universe_file_can_supply_symbols_before_code_does(tmp_path: Path) -> None:
    path = tmp_path / "universe.json"
    path.write_text(json.dumps({"universe": ["SPY", "QQQ", BTC]}))
    assert readonly_module.load_universe_file(path) == (SPY, "QQQ", BTC)


def test_a_symbol_that_could_carry_a_shell_fragment_is_refused() -> None:
    """The cleanup command embeds the symbol as text an operator may paste."""
    for hostile in ("BTC/USD; rm -rf /", "SPY && echo", "$(whoami)", "", "SPY'"):
        with pytest.raises(smoke.SmokeInputError):
            readonly_module.normalize_smoke_symbol(hostile)


# --------------------------------------------------------------------------
# Cleanup planning
# --------------------------------------------------------------------------


def test_crypto_fee_adjusted_position_can_be_planned_correctly() -> None:
    """The real regression: plan from the settled position, not the fill.

    A BUY of 0.00016705 BTC settled as a position of 0.000166632 BTC once the
    taker fee came out of the base asset. A cleanup sized from the ordered or
    filled number would try to sell more than the account holds.
    """
    plan = plan_cleanup(
        position=PositionSnapshot(symbol=BTC, quantity=SETTLED_BTC, market_value=16.66),
        asset=crypto_asset(),
        quoted_price=100000.0,
    )

    assert plan.verdict is CleanupVerdict.REQUIRED
    assert plan.plan_quantity == SETTLED_BTC
    assert plan.plan_quantity != ORDERED_BTC
    assert plan.plan_quantity < ORDERED_BTC
    assert plan.full_cleanup_possible is True
    assert plan.residual_quantity == 0


def test_cleanup_uses_broker_position_not_ordered_quantity() -> None:
    """The planner is given both numbers and must use only one of them.

    `position_quantity` is what the broker reports. The ordered quantity is
    deliberately larger here, and nothing in the returned plan may equal it.
    """
    plan = plan_cleanup(
        position=PositionSnapshot(symbol=BTC, quantity=SETTLED_BTC, market_value=16.66),
        asset=crypto_asset(),
        quoted_price=100000.0,
    )

    assert plan.position_quantity == SETTLED_BTC
    assert plan.plan_quantity <= plan.position_quantity
    assert ORDERED_BTC not in (plan.plan_quantity, plan.position_quantity)


@pytest.mark.parametrize(
    "held",
    ["0.000166632", "0.00016705", "1.23456789", "0.0001", "12.5", "0.000000001"],
)
def test_cleanup_plan_never_exceeds_actual_position(held: str) -> None:
    """The invariant, across a spread of positions. Selling more is a short."""
    quantity = Decimal(held)
    plan = plan_cleanup(
        position=PositionSnapshot(
            symbol=BTC, quantity=quantity, market_value=float(quantity) * 1e5
        ),
        asset=crypto_asset(),
        quoted_price=100000.0,
    )
    assert plan.plan_quantity <= quantity
    assert plan.residual_quantity >= 0
    assert plan.plan_quantity + plan.residual_quantity == quantity


def test_a_plan_that_exceeded_the_position_would_be_refused() -> None:
    """The last gate raises rather than printing a quantity that is too large."""
    with pytest.raises(CleanupPlanError, match="exceeds the broker position"):
        cleanup_module._verified(  # noqa: SLF001 - asserting the guard itself
            smoke.CleanupPlan(
                symbol=BTC,
                verdict=CleanupVerdict.REQUIRED,
                position_quantity=SETTLED_BTC,
                plan_quantity=ORDERED_BTC,
                reference_price=100000.0,
                estimated_value=None,
                min_order_size=BTC_MIN_ORDER,
                min_trade_increment=BTC_INCREMENT,
                minimum_notional_quantity=None,
                full_cleanup_possible=False,
                reason="constructed by hand for this test",
            )
        )


def test_a_coarse_increment_rounds_down_and_leaves_a_residue() -> None:
    """Rounding is one-directional, and what cannot be sold is reported."""
    plan = plan_cleanup(
        position=PositionSnapshot(symbol=BTC, quantity=Decimal("0.000166632"), market_value=16.66),
        asset=crypto_asset(
            min_trade_increment=Decimal("0.000001"), min_order_size=Decimal("0.000001")
        ),
        quoted_price=100000.0,
    )

    assert plan.plan_quantity == Decimal("0.000166")
    assert plan.residual_quantity == Decimal("0.000000632")
    assert plan.full_cleanup_possible is False
    assert plan.plan_quantity < plan.position_quantity


def test_a_position_below_the_brokers_minimum_order_value_cannot_be_closed() -> None:
    """Reported plainly rather than papered over by rounding up to the floor."""
    plan = plan_cleanup(
        position=PositionSnapshot(symbol=BTC, quantity=Decimal("0.00002"), market_value=2.0),
        asset=crypto_asset(),
        quoted_price=100000.0,
    )

    assert plan.verdict is CleanupVerdict.NOT_POSSIBLE
    assert plan.plan_quantity == 0
    assert plan.command is None
    assert "minimum order value" in plan.reason
    assert "do NOT top the position up" in plan.reason


def test_a_flat_position_needs_no_cleanup_and_generates_no_command() -> None:
    plan = plan_cleanup(
        position=PositionSnapshot(symbol=BTC, quantity=Decimal(0), market_value=0.0),
        asset=crypto_asset(),
        quoted_price=100000.0,
    )

    assert plan.verdict is CleanupVerdict.NONE_REQUIRED
    assert plan.plan_quantity == 0
    assert plan.command is None


def test_unreadable_crypto_metadata_plans_nothing_rather_than_guessing() -> None:
    """A guessed trade increment produces a wrong size. Fail closed instead."""
    plan = plan_cleanup(
        position=PositionSnapshot(symbol=BTC, quantity=SETTLED_BTC, market_value=16.66),
        asset=None,
        quoted_price=100000.0,
    )

    assert plan.verdict is CleanupVerdict.NOT_POSSIBLE
    assert plan.plan_quantity == 0
    assert "precision metadata" in plan.reason


def test_equity_whole_share_cleanup_is_planned_correctly() -> None:
    """Equities round down to whole shares, and carry no USD order minimum.

    No equity asset metadata exists on this build, so the policy is this
    system's own whole-share rule - and the plan says so, rather than implying
    the broker was consulted.
    """
    plan = plan_cleanup(
        position=PositionSnapshot(symbol=SPY, quantity=Decimal("3.4"), market_value=1700.0),
        asset=None,
        quoted_price=None,
    )

    assert plan.verdict is CleanupVerdict.REQUIRED
    assert plan.plan_quantity == Decimal(3)
    assert plan.residual_quantity == Decimal("0.4")
    assert plan.full_cleanup_possible is False
    assert plan.minimum_notional_quantity == Decimal(1)
    assert "whole-share policy" in plan.reason
    assert plan.command is not None and "--qty 3" in plan.command


def test_a_whole_equity_position_is_fully_closable() -> None:
    plan = plan_cleanup(
        position=PositionSnapshot(symbol=SPY, quantity=Decimal(2), market_value=1000.0),
        asset=None,
        quoted_price=None,
    )

    assert plan.plan_quantity == Decimal(2)
    assert plan.full_cleanup_possible is True
    assert plan.reference_price == pytest.approx(500.0)


def test_a_sub_share_equity_position_cannot_be_closed_under_whole_share_policy() -> None:
    plan = plan_cleanup(
        position=PositionSnapshot(symbol=SPY, quantity=Decimal("0.4"), market_value=200.0),
        asset=None,
        quoted_price=None,
    )

    assert plan.verdict is CleanupVerdict.NOT_POSSIBLE
    assert plan.plan_quantity == 0
    assert "minimum order size" in plan.reason


def test_the_generated_command_names_the_position_quantity_and_sells() -> None:
    plan = plan_cleanup(
        position=PositionSnapshot(symbol=BTC, quantity=SETTLED_BTC, market_value=16.66),
        asset=crypto_asset(),
        quoted_price=100000.0,
        database=Path("data/autotrader.db"),
    )

    assert plan.command == (
        "autotrader paper-submit --symbol BTC/USD --side SELL --qty 0.000166632 "
        "--confirm-paper PAPER --db data/autotrader.db"
    )
    assert "0.00016705" not in plan.command


def test_the_minimum_entry_is_a_floor_and_comes_with_a_dry_run() -> None:
    """Sizing the BUY stays the operator's decision, checked by a dry run."""
    minimum, note, command = cleanup_module.plan_minimum_entry(
        symbol=BTC, asset=crypto_asset(), quoted_price=100000.0
    )

    assert minimum == Decimal("0.0001")
    assert "not a recommended size" in note
    assert command is not None and command.endswith("--dry-run")
    assert "--confirm-paper" not in command


# --------------------------------------------------------------------------
# Baseline snapshot
# --------------------------------------------------------------------------


def sample_baseline() -> Baseline:
    return Baseline(
        captured_at=T0,
        universe=(BTC, ETH),
        positions={BTC: SETTLED_BTC, ETH: Decimal(0)},
        account_equity=100000.0,
        account_cash=99983.34,
        account_status="ACTIVE",
        git_branch="main",
        git_sha="a" * 40,
        git_dirty=False,
        database_path="data/autotrader.db",
        schema_version=5,
        open_order_client_ids=("autotrader-buy-1",),
        reconciliation_run_id=7,
        reconciliation_status=RECONCILIATION_STATUS_CLEAN,
        reconciliation_safe_to_trade=True,
        account_safety_state=ACCOUNT_SAFETY_SAFE,
        account_safety_safe_to_trade=True,
    )


def test_baseline_snapshot_contains_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing credential-shaped by key, and no credential value by content."""
    monkeypatch.setenv("ALPACA_API_KEY", "PKTESTKEYNEVERREAL0000")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SKTESTSECRETNEVERREAL0")

    path = write_baseline(sample_baseline(), tmp_path / "baseline.json")
    document = path.read_text()

    assert "PKTESTKEYNEVERREAL0000" not in document
    assert "SKTESTSECRETNEVERREAL0" not in document
    for token in ("api_key", "apiKey", "secret", "token", "password", "authorization", ".env"):
        assert token.lower() not in document.lower(), token
    assert health_module.credential_key_names(json.loads(document)) == ()


def test_a_snapshot_holding_a_credential_value_is_refused_before_it_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan runs first, so a rejected snapshot leaves nothing on disk.

    The secret is smuggled in through an innocent-looking field, which is
    exactly the case a key-name check alone would miss.
    """
    monkeypatch.setenv("ALPACA_API_KEY", "PKTESTKEYNEVERREAL0000")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SKTESTSECRETNEVERREAL0")
    leaky = Baseline(
        captured_at=T0,
        universe=(BTC,),
        positions={BTC: Decimal(0)},
        account_status="PKTESTKEYNEVERREAL0000",
    )
    destination = tmp_path / "baseline.json"

    with pytest.raises(BaselineError) as error:
        write_baseline(leaky, destination)

    assert "PKTESTKEYNEVERREAL0000" not in str(error.value)
    assert "ALPACA_API_KEY" in str(error.value)
    assert not destination.exists()


def test_a_snapshot_with_a_credential_shaped_key_is_refused() -> None:
    with pytest.raises(BaselineError, match="credential-shaped"):
        baseline_module.assert_no_secrets({"account": {"api_key": "anything"}})


def test_a_snapshot_round_trips_quantities_exactly(tmp_path: Path) -> None:
    """Exact decimal text, never a float: the comparison is exact equality."""
    path = write_baseline(sample_baseline(), tmp_path / "baseline.json")
    loaded = read_baseline(path)

    assert loaded.positions[BTC] == SETTLED_BTC
    assert loaded.quantity_for("btc/usd") == SETTLED_BTC
    assert loaded.quantity_for(SPY) == Decimal(0)
    assert json.loads(path.read_text())["positions"][BTC] == "0.000166632"


def test_a_snapshot_from_an_unknown_schema_is_refused(tmp_path: Path) -> None:
    """A field that moved between versions would produce a wrong comparison."""
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"baseline_schema": 99, "captured_at": T0.isoformat()}))

    with pytest.raises(BaselineError, match="schema"):
        read_baseline(path)


def test_the_snapshot_payload_is_an_allowlist_not_an_object_dump() -> None:
    """A field added upstream cannot start writing itself to disk."""
    payload = sample_baseline().to_payload()
    assert set(payload) == {
        "account",
        "account_safety",
        "baseline_schema",
        "captured_at",
        "database",
        "git",
        "open_order_client_ids",
        "positions",
        "reconciliation",
        "universe",
        "unknown_order_client_ids",
    }
    assert "asdict" not in package_code()["baseline.py"]


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------


def run_preflight(
    connection: sqlite3.Connection,
    *,
    client: object | None = None,
    git: GitState = CLEAN_GIT,
    universe: tuple[str, ...] = (BTC, ETH),
    **kwargs: object,
) -> preflight_module.PreflightReport:
    return preflight_module.run_preflight(
        client=client if client is not None else FakeBrokerClient(),
        connection=connection,
        database_path=Path("state.db"),
        git=git,
        universe=universe,
        universe_origin="test",
        **kwargs,  # type: ignore[arg-type]
    )


def check_named(report: object, name: str):
    gate = report.gate if hasattr(report, "gate") else report.report.gate
    found = [check for check in gate.checks if check.name == name]
    assert found, f"no check named {name} in {[c.name for c in gate.checks]}"
    return found[0]


def test_preflight_blocks_on_a_shared_account_halt(
    writer: sqlite3.Connection, database_path: Path, credentials: None
) -> None:
    """One account, both books: a halt from either side stops the smoke.

    The halt is what the execution boundary itself checks, so a preflight that
    ignored it would clear a smoke the very next step refuses - and would do so
    while an order of ours may be live at the broker under the recorded key.
    """
    seed_reconciliation(writer)
    seed_account_safety(writer, safe=False, client_order_id="autotrader-unknown-1")

    with open_readonly(database_path) as reader:
        report = run_preflight(reader)

    safety = check_named(report, "account.safety")
    assert safety.verdict is SmokeVerdict.FAIL
    assert ACCOUNT_SAFETY_UNSAFE_UNKNOWN in safety.detail
    assert "autotrader-unknown-1" in safety.detail
    assert report.ready is False


def test_a_green_pass_does_not_override_a_standing_account_halt(
    writer: sqlite3.Connection, database_path: Path, credentials: None
) -> None:
    """The two checks answer different questions, and both must be asked.

    A reconciliation pass narrower than the tracked universe can be entirely
    CLEAN and still leave a halt standing, because it has not established that
    the rest of the account is understood. Reading only the pass would call
    that ready.
    """
    seed_reconciliation(writer, status=RECONCILIATION_STATUS_CLEAN, account_safe=False)

    with open_readonly(database_path) as reader:
        report = run_preflight(reader)

    assert check_named(report, "reconciliation").verdict is SmokeVerdict.PASS
    assert check_named(report, "account.safety").verdict is SmokeVerdict.FAIL
    assert report.ready is False


def test_preflight_blocks_when_account_safety_was_never_established(
    writer: sqlite3.Connection, database_path: Path, credentials: None
) -> None:
    """ "Nobody has ever checked" is not "checked and fine"."""
    seed_reconciliation(writer, account_safe=None)
    writer.execute("DELETE FROM account_safety_state")
    writer.commit()

    with open_readonly(database_path) as reader:
        report = run_preflight(reader)

    safety = check_named(report, "account.safety")
    assert safety.verdict is SmokeVerdict.FAIL
    assert "has ever established" in safety.detail
    assert ACCOUNT_SAFETY_UNSAFE_RECONCILIATION in safety.detail
    assert report.ready is False


def test_the_preflight_reports_the_account_safety_state_it_read(
    writer: sqlite3.Connection, database_path: Path, credentials: None
) -> None:
    """It is carried on the report, so the baseline records the same answer."""
    seed_reconciliation(writer)

    with open_readonly(database_path) as reader:
        report = run_preflight(reader)

    assert report.account_safety is not None
    assert report.account_safety.state == ACCOUNT_SAFETY_SAFE
    assert report.account_safety.safe_to_trade is True

    baseline = report.to_baseline(database_path="state.db", schema=None)
    assert baseline.account_safety_state == ACCOUNT_SAFETY_SAFE
    assert baseline.account_safety_safe_to_trade is True


def test_the_baseline_round_trips_the_account_safety_state(tmp_path: Path) -> None:
    """It survives JSON, so "before" and "after" are compared on one answer."""
    before = sample_baseline()
    path = write_baseline(before, tmp_path / "baseline.json")
    after = read_baseline(path)
    assert after.account_safety_state == before.account_safety_state
    assert after.account_safety_safe_to_trade == before.account_safety_safe_to_trade


def test_a_clean_system_is_ready_for_a_paper_smoke(
    writer: sqlite3.Connection, database_path: Path, credentials: None
) -> None:
    seed_reconciliation(writer)

    with open_readonly(database_path) as reader:
        report = run_preflight(reader)

    assert report.ready is True
    assert report.verdict_text() == READY_FOR_PAPER_SMOKE
    assert report.gate.failures == ()


def test_preflight_blocks_on_open_unknown_order(
    writer: sqlite3.Connection, database_path: Path, credentials: None
) -> None:
    """The hardest block: an order may already exist under that key.

    Both the UNKNOWN gate and the open-order gate must fail, and the message
    must tell the operator to reconcile rather than re-send.
    """
    seed_reconciliation(writer)
    seed_intent(writer, client_order_id="autotrader-unknown-1", status=INTENT_STATUS_UNKNOWN)

    with open_readonly(database_path) as reader:
        report = run_preflight(reader)

    assert report.ready is False
    assert report.verdict_text() == BLOCKED
    unknown = check_named(report, "orders.unknown")
    assert unknown.verdict is SmokeVerdict.FAIL
    assert "autotrader-unknown-1" in unknown.detail
    assert "DO NOT RETRY THE ORIGINAL ORDER" in unknown.detail
    assert "reconcile" in unknown.detail
    assert check_named(report, "orders.open").verdict is SmokeVerdict.FAIL


def test_preflight_blocks_on_an_open_but_known_order(
    writer: sqlite3.Connection, database_path: Path, credentials: None
) -> None:
    """A working order would make the smoke's own order unidentifiable."""
    seed_reconciliation(writer)
    intent = seed_intent(writer, status=INTENT_STATUS_SUBMITTED)
    seed_broker_order(writer, intent, status="partially_filled", filled_quantity=Decimal("0.00001"))

    with open_readonly(database_path) as reader:
        report = run_preflight(reader)

    assert report.ready is False
    assert check_named(report, "orders.open").verdict is SmokeVerdict.FAIL
    assert check_named(report, "orders.unknown").verdict is SmokeVerdict.PASS


def test_a_settled_order_does_not_block(
    writer: sqlite3.Connection, database_path: Path, credentials: None
) -> None:
    """A filled order in a terminal broker status is finished, not open."""
    seed_reconciliation(writer)
    intent = seed_intent(writer, status=INTENT_STATUS_SUBMITTED)
    seed_broker_order(writer, intent, status="filled")

    with open_readonly(database_path) as reader:
        report = run_preflight(reader)

    assert check_named(report, "orders.open").verdict is SmokeVerdict.PASS
    assert report.ready is True


def test_preflight_blocks_when_reconciliation_was_never_run(
    reader: sqlite3.Connection, credentials: None
) -> None:
    report = run_preflight(reader)

    reconciliation = check_named(report, "reconciliation")
    assert reconciliation.verdict is SmokeVerdict.FAIL
    assert "autotrader reconcile" in reconciliation.detail
    assert report.ready is False


def test_preflight_blocks_on_a_stale_green_reconciliation(
    writer: sqlite3.Connection, database_path: Path, credentials: None
) -> None:
    """Positions and orders move. Yesterday's green says nothing about now."""
    seed_reconciliation(writer, completed_at=datetime.now(UTC) - timedelta(hours=9))

    with open_readonly(database_path) as reader:
        report = run_preflight(reader)

    reconciliation = check_named(report, "reconciliation")
    assert reconciliation.verdict is SmokeVerdict.FAIL
    assert "stale green result is not" in reconciliation.detail


@pytest.mark.parametrize(
    ("status", "safe"),
    [(RECONCILIATION_STATUS_UNRESOLVED, False), (RECONCILIATION_STATUS_CLEAN, False)],
)
def test_preflight_blocks_on_a_reconciliation_that_is_not_safe(
    writer: sqlite3.Connection, database_path: Path, credentials: None, status: str, safe: bool
) -> None:
    seed_reconciliation(writer, status=status, safe=safe)

    with open_readonly(database_path) as reader:
        report = run_preflight(reader)

    assert check_named(report, "reconciliation").verdict is SmokeVerdict.FAIL


def test_preflight_never_starts_a_reconciliation_pass() -> None:
    """Read and repair stay apart: the pass writes, so the operator runs it."""
    for name, code in package_code().items():
        assert "reconcile_paper_state" not in code, f"smoke/{name}"


def test_preflight_blocks_without_credentials(
    writer: sqlite3.Connection, database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    seed_reconciliation(writer)

    with open_readonly(database_path) as reader:
        report = run_preflight(reader)

    credentials_check = check_named(report, "credentials")
    assert credentials_check.verdict is SmokeVerdict.FAIL
    assert "NOT SET" in credentials_check.detail


def test_preflight_reports_credential_presence_without_reading_values(
    reader: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "PKTESTKEYNEVERREAL0000")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SKTESTSECRETNEVERREAL0")

    report = run_preflight(reader)

    for check in report.gate.checks:
        assert "PKTESTKEYNEVERREAL0000" not in check.detail
        assert "SKTESTSECRETNEVERREAL0" not in check.detail


def test_preflight_blocks_on_a_dirty_working_tree_unless_told_otherwise(
    writer: sqlite3.Connection, database_path: Path, credentials: None
) -> None:
    seed_reconciliation(writer)

    with open_readonly(database_path) as reader:
        blocked = run_preflight(reader, git=DIRTY_GIT)
        allowed = run_preflight(reader, git=DIRTY_GIT, allow_dirty=True)

    assert check_named(blocked, "git").verdict is SmokeVerdict.FAIL
    assert check_named(allowed, "git").verdict is SmokeVerdict.PASS
    assert "DIRTY" in check_named(allowed, "git").detail


def test_preflight_blocks_when_the_account_cannot_trade(
    writer: sqlite3.Connection, database_path: Path, credentials: None
) -> None:
    seed_reconciliation(writer)
    client = FakeBrokerClient(account=make_account(trading_blocked=True))

    with open_readonly(database_path) as reader:
        report = run_preflight(reader, client=client)

    assert check_named(report, "broker.account").verdict is SmokeVerdict.FAIL
    assert report.ready is False


def test_preflight_reports_the_whole_picture_when_the_broker_is_unreachable(
    writer: sqlite3.Connection, database_path: Path, credentials: None
) -> None:
    """One line marked FAIL beats a traceback about the first failure."""
    seed_reconciliation(writer)

    with open_readonly(database_path) as reader:
        report = preflight_module.run_preflight(
            client=None,
            connection=reader,
            database_path=Path("state.db"),
            git=CLEAN_GIT,
            universe=(BTC, ETH),
            universe_origin="test",
            broker_error="credentials are not configured",
        )

    assert report.ready is False
    assert check_named(report, "broker.account").verdict is SmokeVerdict.FAIL
    assert check_named(report, "sqlite.schema").verdict is SmokeVerdict.PASS
    assert check_named(report, "reconciliation").verdict is SmokeVerdict.PASS


def test_preflight_reports_a_short_position_as_an_unreadable_broker(
    writer: sqlite3.Connection, database_path: Path, credentials: None
) -> None:
    """This system is long only; a short must not be quietly ignored."""
    seed_reconciliation(writer)
    client = FakeBrokerClient(positions=[make_position(side=PositionSide.SHORT)])

    with open_readonly(database_path) as reader:
        report = run_preflight(reader, client=client)

    positions = check_named(report, "broker.positions")
    assert positions.verdict is SmokeVerdict.FAIL
    assert "SHORT" in positions.detail


def test_preflight_reports_untracked_positions_without_blocking(
    writer: sqlite3.Connection, database_path: Path, credentials: None
) -> None:
    seed_reconciliation(writer)
    client = FakeBrokerClient(
        positions=[make_position(), make_position("LTC/USD", qty="1", market_value="70")]
    )

    with open_readonly(database_path) as reader:
        report = run_preflight(reader, client=client)

    positions = check_named(report, "broker.positions")
    assert positions.verdict is SmokeVerdict.PASS
    assert "LTC/USD" in positions.detail
    assert report.ready is True


def test_runtime_checkpoints_are_reported_but_never_gate(
    writer: sqlite3.Connection, database_path: Path, credentials: None
) -> None:
    """Stopping the runtime before a manual smoke is the recommended way."""
    seed_reconciliation(writer)
    upsert_runtime_checkpoint(
        writer,
        symbol=BTC,
        last_processed_bar_timestamp=T0,
        updated_at=datetime.now(UTC) - timedelta(days=2),
    )

    with open_readonly(database_path) as reader:
        report = run_preflight(reader)

    checkpoints = check_named(report, "runtime.checkpoints")
    assert checkpoints.verdict is SmokeVerdict.PASS
    assert "STALE" in checkpoints.detail
    assert "NOT_RECORDED" in checkpoints.detail
    assert report.ready is True


def test_the_preflight_reports_the_smallest_valid_order_for_the_smoke_symbol(
    writer: sqlite3.Connection,
    database_path: Path,
    credentials: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_reconciliation(writer)
    monkeypatch.setattr(broker_module, "read_asset_spec", lambda client, symbol: crypto_asset())
    monkeypatch.setattr(broker_module, "read_reference_price", lambda symbol: 100000.0)

    with open_readonly(database_path) as reader:
        report = run_preflight(reader, smoke_symbol=BTC)

    assert report.entry_minimum == Decimal("0.0001")
    assert "--dry-run" in report.entry_dry_run_command
    assert "--confirm-paper" not in report.entry_dry_run_command


def test_the_baseline_is_built_from_the_numbers_the_preflight_already_read(
    writer: sqlite3.Connection, database_path: Path, credentials: None
) -> None:
    seed_reconciliation(writer)
    client = FakeBrokerClient(positions=[make_position()])

    with open_readonly(database_path) as reader:
        report = run_preflight(reader, client=client)

    recorded = report.to_baseline(database_path="state.db", schema=5)
    assert recorded.positions[BTC] == SETTLED_BTC
    assert recorded.positions[ETH] == Decimal(0)
    assert recorded.git_sha == CLEAN_GIT.sha
    assert recorded.reconciliation_status == RECONCILIATION_STATUS_CLEAN


# --------------------------------------------------------------------------
# Order inspector
# --------------------------------------------------------------------------


def test_order_inspector_never_treats_submitted_as_filled() -> None:
    """An accepted order is accepted. The filled quantity is a separate number."""
    client = FakeBrokerClient(
        orders={
            "autotrader-buy-1": make_order(
                client_order_id="autotrader-buy-1",
                status=OrderStatus.ACCEPTED,
                qty="0.00016705",
                filled_qty="0",
            )
        },
        positions=[],
    )

    result = inspector_module.inspect_order(client, client_order_id="autotrader-buy-1")

    assert result.outcome is LookupOutcome.FOUND
    report = result.report
    assert report is not None
    assert report.status == "accepted"
    assert report.filled_quantity == 0
    assert report.filled_average_price is None
    assert report.open_remainder == ORDERED_BTC
    assert report.is_open is True
    assert report.broker_position is not None
    assert report.broker_position.quantity == 0


def test_the_inspector_reports_the_position_as_authoritative_over_the_fill() -> None:
    """Filled 0.00016705, settled 0.000166632. Both printed; the second wins."""
    client = FakeBrokerClient(
        orders={
            "autotrader-buy-1": make_order(
                client_order_id="autotrader-buy-1",
                status=OrderStatus.FILLED,
                qty="0.00016705",
                filled_qty="0.00016705",
                filled_avg_price="99850.5",
                filled_at=T0,
            )
        },
        positions=[make_position()],
    )

    result = inspector_module.inspect_order(client, client_order_id="autotrader-buy-1")
    report = result.report
    assert report is not None

    assert report.filled_quantity == ORDERED_BTC
    assert report.broker_position.quantity == SETTLED_BTC
    assert report.open_remainder == 0
    note = inspector_module.fill_versus_position_note(report)
    assert note is not None
    assert "taker fee" in note
    assert "Size any cleanup from the POSITION" in note
    assert "0.000000418" in note, "dust must not be rendered in scientific notation"


def note_for(
    *, side: AlpacaOrderSide, filled: str, held: str | None, symbol: str = BTC
) -> str | None:
    """The fill-versus-position line for one order, with no I/O."""
    client = FakeBrokerClient(
        orders={
            "autotrader-1": make_order(
                client_order_id="autotrader-1",
                side=side,
                status=OrderStatus.FILLED,
                qty=filled,
                filled_qty=filled,
                symbol=symbol,
            )
        },
        positions=[] if held is None else [make_position(symbol, qty=held, market_value="1")],
    )
    result = inspector_module.inspect_order(client, client_order_id="autotrader-1")
    assert result.report is not None
    return inspector_module.fill_versus_position_note(result.report)


def test_a_buy_whose_position_is_zero_is_not_blamed_on_the_taker_fee() -> None:
    """A closed position and a fee-shaved one are different situations.

    Observed against the real paper account: inspecting a historic BUY whose
    exposure a later SELL had already closed produced a fee explanation for a
    position of zero, which reads as though the whole fill had evaporated.
    """
    note = note_for(side=AlpacaOrderSide.BUY, filled="0.00016705", held=None)

    assert note is not None
    assert "taker fee" not in note
    assert "already closed it" in note
    assert "there is nothing to close" in note


def test_a_sell_is_described_by_what_it_left_behind_not_by_a_fee() -> None:
    """Comparing a SELL's fill against the remaining position is meaningless."""
    closed = note_for(side=AlpacaOrderSide.SELL, filled="0.000166632", held=None)
    residual = note_for(side=AlpacaOrderSide.SELL, filled="0.0001", held="0.000000418")

    assert closed is not None
    assert "Exposure in this symbol is closed" in closed
    assert "taker fee" not in closed

    assert residual is not None
    assert "residual exposure" in residual
    assert "0.000000418" in residual
    assert "taker fee" not in residual


def test_a_buy_matching_its_position_exactly_says_nothing() -> None:
    """No note is better than a note that states the obvious."""
    assert note_for(side=AlpacaOrderSide.BUY, filled="0.0001", held="0.0001") is None


def test_an_unresolvable_lookup_reports_unresolved_and_forbids_a_retry() -> None:
    """The one case where acting would duplicate an order."""
    client = FakeBrokerClient(orders={"autotrader-buy-1": timeout_error()})

    result = inspector_module.inspect_order(client, client_order_id="autotrader-buy-1")

    assert result.outcome is LookupOutcome.UNRESOLVED
    assert result.unresolved is True
    assert result.verdict_text == ORDER_TRUTH_UNRESOLVED
    assert result.banners() == (DO_NOT_RETRY_BANNER,)
    assert result.report is None
    assert "not evidence that the order is absent" in result.detail
    assert "reconcile" in result.detail
    assert "resend" not in result.detail.lower().replace("-", "")


def test_a_definitive_not_found_is_kept_apart_from_an_unresolved_lookup() -> None:
    client = FakeBrokerClient(orders={})

    result = inspector_module.inspect_order(client, client_order_id="autotrader-missing")

    assert result.outcome is LookupOutcome.NOT_FOUND
    assert result.unresolved is False
    assert result.banners() == ()
    assert "definitive answer, not a failed lookup" in result.detail


def test_an_order_can_be_inspected_by_the_brokers_own_id() -> None:
    client = FakeBrokerClient(
        orders={
            BROKER_ORDER_UUID: make_order(
                client_order_id="autotrader-buy-1",
                status=OrderStatus.FILLED,
                filled_qty="0.00016705",
            )
        }
    )

    result = inspector_module.inspect_order(client, broker_order_id=BROKER_ORDER_UUID)

    assert result.outcome is LookupOutcome.FOUND
    assert result.report is not None
    assert result.report.client_order_id == "autotrader-buy-1"


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"client_order_id": "a", "broker_order_id": "b"}],
)
def test_exactly_one_identifier_is_required(kwargs: dict[str, str]) -> None:
    """Two identifiers could disagree; answering one of them silently is worse."""
    with pytest.raises(smoke.SmokeInputError, match="exactly one"):
        inspector_module.inspect_order(FakeBrokerClient(), **kwargs)


def test_the_inspector_shows_what_local_state_believes_alongside_the_broker(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    intent = seed_intent(writer, status=INTENT_STATUS_SUBMITTED)
    seed_broker_order(writer, intent, status="accepted", filled_quantity=Decimal(0))
    client = FakeBrokerClient(
        orders={
            "autotrader-buy-1": make_order(
                client_order_id="autotrader-buy-1",
                status=OrderStatus.FILLED,
                filled_qty="0.00016705",
            )
        },
        positions=[make_position()],
    )

    with open_readonly(database_path) as reader:
        result = inspector_module.inspect_order(
            client, client_order_id="autotrader-buy-1", connection=reader
        )

    assert result.local_intent_status == INTENT_STATUS_SUBMITTED
    assert result.local_snapshot_status == "accepted"
    assert result.report is not None and result.report.status == "filled"


def test_an_unreadable_position_does_not_invalidate_the_order_report() -> None:
    """The order still reads; the number a cleanup needs is what goes missing."""
    client = FakeBrokerClient(
        orders={"autotrader-buy-1": make_order(client_order_id="autotrader-buy-1")},
        positions=api_error(500, "upstream failure"),
    )

    result = inspector_module.inspect_order(client, client_order_id="autotrader-buy-1")

    assert result.outcome is LookupOutcome.FOUND
    assert result.report is not None
    assert result.report.broker_position is None
    assert "Do not size a cleanup from the filled quantity" in result.position_detail


# --------------------------------------------------------------------------
# Final audit
# --------------------------------------------------------------------------


def run_audit(
    connection: sqlite3.Connection,
    *,
    client: object | None = None,
    git: GitState = CLEAN_GIT,
    universe: tuple[str, ...] = (BTC, ETH),
    **kwargs: object,
) -> audit_module.AuditRunReport:
    return audit_module.run_audit(
        client=client if client is not None else FakeBrokerClient(),
        connection=connection,
        database_path=Path("state.db"),
        git=git,
        universe=universe,
        universe_origin="test",
        **kwargs,  # type: ignore[arg-type]
    )


def flat_baseline() -> Baseline:
    return Baseline(
        captured_at=T0,
        universe=(BTC, ETH),
        positions={BTC: Decimal(0), ETH: Decimal(0)},
    )


def test_final_audit_detects_a_shared_account_halt(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    """A smoke that left the account halted did not leave it restored.

    The dangerous shape this catches: a CLEAN pass recorded *before* an
    ambiguous submission, and the halt that submission raised still standing
    afterwards. The pass check alone would call that a finished smoke.
    """
    seed_reconciliation(writer, status=RECONCILIATION_STATUS_CLEAN, account_safe=False)

    with open_readonly(database_path) as reader:
        result = run_audit(reader, baseline=flat_baseline(), baseline_path="baseline.json")

    assert check_named(result, "reconciliation").verdict is SmokeVerdict.PASS
    safety = check_named(result, "account.safety")
    assert safety.verdict is SmokeVerdict.FAIL
    assert ACCOUNT_SAFETY_UNSAFE_UNKNOWN in safety.detail
    assert result.complete is False
    assert result.verdict_text() == SMOKE_INCOMPLETE


def test_a_finished_smoke_leaves_the_account_open_for_business(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    """The condition the final report has to be able to state as proven."""
    seed_reconciliation(writer, status=RECONCILIATION_STATUS_CLEAN)

    with open_readonly(database_path) as reader:
        result = run_audit(reader, baseline=flat_baseline(), baseline_path="baseline.json")

    safety = check_named(result, "account.safety")
    assert safety.verdict is SmokeVerdict.PASS
    assert ACCOUNT_SAFETY_SAFE in safety.detail
    assert result.complete is True


def test_a_finished_smoke_is_complete_and_exposure_is_restored(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    seed_reconciliation(writer, status=RECONCILIATION_STATUS_CLEAN)

    with open_readonly(database_path) as reader:
        result = run_audit(reader, baseline=flat_baseline(), baseline_path="baseline.json")

    assert result.complete is True
    assert result.verdict_text() == SMOKE_COMPLETE
    assert result.exposure_text() == EXPOSURE_RESTORED
    assert all(comparison.restored for comparison in result.report.comparisons)


def test_final_audit_detects_residual_position(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    """A dust remainder is residual exposure, not noise. Exact equality."""
    seed_reconciliation(writer)
    client = FakeBrokerClient(positions=[make_position(qty="0.000000418", market_value="0.04")])

    with open_readonly(database_path) as reader:
        result = run_audit(reader, client=client, baseline=flat_baseline())

    assert result.complete is False
    assert result.verdict_text() == SMOKE_INCOMPLETE
    assert result.exposure_text() == EXPOSURE_NOT_RESTORED
    positions = check_named(result, "positions")
    assert positions.verdict is SmokeVerdict.FAIL
    assert "0.000000418" in positions.detail
    comparison = next(c for c in result.report.comparisons if c.symbol == BTC)
    assert comparison.restored is False
    assert comparison.delta == Decimal("0.000000418")


def test_a_position_that_predates_the_smoke_is_restored_not_residual(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    """The baseline is the reference, not zero."""
    seed_reconciliation(writer)
    client = FakeBrokerClient(positions=[make_position()])
    baseline = Baseline(
        captured_at=T0, universe=(BTC, ETH), positions={BTC: SETTLED_BTC, ETH: Decimal(0)}
    )

    with open_readonly(database_path) as reader:
        result = run_audit(reader, client=client, baseline=baseline)

    assert result.complete is True
    assert result.exposure_text() == EXPOSURE_RESTORED


def test_without_a_baseline_any_open_position_is_treated_as_residual(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    """The audit cannot prove a position predates the smoke, so it does not."""
    seed_reconciliation(writer)
    client = FakeBrokerClient(positions=[make_position()])

    with open_readonly(database_path) as reader:
        result = run_audit(reader, client=client)

    assert result.complete is False
    assert result.exposure_text() is None
    assert "no baseline" in check_named(result, "positions").detail.lower()
    assert any("No baseline snapshot" in note for note in result.report.notes)


def test_final_audit_detects_open_smoke_order(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    seed_reconciliation(writer)
    intent = seed_intent(writer, status=INTENT_STATUS_SUBMITTED)
    seed_broker_order(writer, intent, status="accepted", filled_quantity=Decimal(0))

    with open_readonly(database_path) as reader:
        result = run_audit(reader, baseline=flat_baseline())

    assert result.complete is False
    open_check = check_named(result, "orders.open")
    assert open_check.verdict is SmokeVerdict.FAIL
    assert "autotrader-buy-1" in open_check.detail


def test_final_audit_detects_unknown_order(writer: sqlite3.Connection, database_path: Path) -> None:
    seed_reconciliation(writer)
    seed_intent(writer, client_order_id="autotrader-unknown-1", status=INTENT_STATUS_UNKNOWN)

    with open_readonly(database_path) as reader:
        result = run_audit(reader, baseline=flat_baseline())

    assert result.complete is False
    unknown = check_named(result, "orders.unknown")
    assert unknown.verdict is SmokeVerdict.FAIL
    assert ORDER_TRUTH_UNRESOLVED in unknown.detail
    assert DO_NOT_RETRY_BANNER in result.banners


def test_final_audit_requires_a_clean_reconciliation_not_merely_a_safe_one(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    """REPAIRED means the pass still had something to fix. Run it again."""
    seed_reconciliation(writer, status=RECONCILIATION_STATUS_REPAIRED, safe=True)

    with open_readonly(database_path) as reader:
        result = run_audit(reader, baseline=flat_baseline())

    reconciliation = check_named(result, "reconciliation")
    assert reconciliation.verdict is SmokeVerdict.FAIL
    assert "REPAIRED means the pass still had something to fix" in reconciliation.detail
    assert result.complete is False


def test_final_audit_blocks_on_a_dirty_tree(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    seed_reconciliation(writer)

    with open_readonly(database_path) as reader:
        result = run_audit(reader, git=DIRTY_GIT, baseline=flat_baseline())

    assert check_named(result, "git").verdict is SmokeVerdict.FAIL
    assert result.complete is False


def test_the_audit_confirms_one_buy_and_one_sell(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    seed_reconciliation(writer)
    buy = seed_intent(writer, client_order_id="autotrader-buy-1", side="BUY")
    seed_broker_order(writer, buy, client_order_id="autotrader-buy-1", status="filled")
    sell = seed_intent(
        writer, client_order_id="autotrader-sell-1", side="SELL", quantity=SETTLED_BTC
    )
    seed_broker_order(
        writer,
        sell,
        client_order_id="autotrader-sell-1",
        broker_order_id="8f1c4d2e-0000-4000-8000-000000000002",
        side="SELL",
        quantity=SETTLED_BTC,
        filled_quantity=SETTLED_BTC,
        status="filled",
    )
    client = FakeBrokerClient(
        orders={
            "autotrader-buy-1": make_order(
                client_order_id="autotrader-buy-1",
                status=OrderStatus.FILLED,
                filled_qty="0.00016705",
            ),
            "autotrader-sell-1": make_order(
                client_order_id="autotrader-sell-1",
                status=OrderStatus.FILLED,
                side=AlpacaOrderSide.SELL,
                qty="0.000166632",
                filled_qty="0.000166632",
                order_id="8f1c4d2e-0000-4000-8000-000000000002",
            ),
        }
    )

    with open_readonly(database_path) as reader:
        result = run_audit(
            reader,
            client=client,
            baseline=flat_baseline(),
            smoke_symbol=BTC,
            buy_client_order_id="autotrader-buy-1",
            sell_client_order_id="autotrader-sell-1",
        )

    assert check_named(result, "orders.buy").verdict is SmokeVerdict.PASS
    assert check_named(result, "orders.sell").verdict is SmokeVerdict.PASS
    assert check_named(result, "orders.unexpected").verdict is SmokeVerdict.PASS
    assert result.complete is True


def test_the_audit_detects_an_unexpected_third_order(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    """Local intents are the complete record of what this system attempted."""
    seed_reconciliation(writer)
    buy = seed_intent(writer, client_order_id="autotrader-buy-1")
    seed_broker_order(writer, buy, status="filled")
    stray = seed_intent(
        writer,
        client_order_id="autotrader-buy-2",
        status=INTENT_STATUS_CONFIRMED_NOT_SUBMITTED,
    )
    assert stray
    client = FakeBrokerClient(
        orders={
            "autotrader-buy-1": make_order(
                client_order_id="autotrader-buy-1",
                status=OrderStatus.FILLED,
                filled_qty="0.00016705",
            )
        }
    )

    with open_readonly(database_path) as reader:
        result = run_audit(
            reader,
            client=client,
            baseline=flat_baseline(),
            smoke_symbol=BTC,
            buy_client_order_id="autotrader-buy-1",
        )

    unexpected = check_named(result, "orders.unexpected")
    assert unexpected.verdict is SmokeVerdict.FAIL
    assert "autotrader-buy-2" in unexpected.detail
    assert result.complete is False


def test_the_audit_reports_an_unresolvable_smoke_order_without_suggesting_a_retry(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    seed_reconciliation(writer)
    seed_intent(writer, client_order_id="autotrader-buy-1")
    client = FakeBrokerClient(orders={"autotrader-buy-1": timeout_error()})

    with open_readonly(database_path) as reader:
        result = run_audit(
            reader,
            client=client,
            baseline=flat_baseline(),
            smoke_symbol=BTC,
            buy_client_order_id="autotrader-buy-1",
        )

    buy = check_named(result, "orders.buy")
    assert buy.verdict is SmokeVerdict.FAIL
    assert ORDER_TRUTH_UNRESOLVED in buy.detail
    assert DO_NOT_RETRY_BANNER in buy.detail
    assert DO_NOT_RETRY_BANNER in result.banners


def test_a_smoke_order_the_broker_has_never_heard_of_is_a_finding(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    seed_reconciliation(writer)
    seed_intent(writer, client_order_id="autotrader-buy-1")

    with open_readonly(database_path) as reader:
        result = run_audit(
            reader,
            baseline=flat_baseline(),
            smoke_symbol=BTC,
            buy_client_order_id="autotrader-buy-1",
        )

    buy = check_named(result, "orders.buy")
    assert buy.verdict is SmokeVerdict.FAIL
    assert "no order under the BUY id" in buy.detail


# --------------------------------------------------------------------------
# Runtime and dashboard health
# --------------------------------------------------------------------------


def test_runtime_freshness_distinguishes_stale_from_never_recorded(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    """A runtime that never wrote and one that stopped an hour ago differ."""
    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    upsert_runtime_checkpoint(
        writer, symbol=BTC, last_processed_bar_timestamp=T0, updated_at=now - timedelta(minutes=5)
    )
    upsert_runtime_checkpoint(
        writer, symbol=ETH, last_processed_bar_timestamp=T0, updated_at=now - timedelta(hours=3)
    )

    with open_readonly(database_path) as reader:
        results = {
            item.symbol: item
            for item in health_module.runtime_health(reader, (BTC, ETH, SPY), now=now)
        }

    assert results[BTC].freshness is smoke.Freshness.FRESH
    assert results[ETH].freshness is smoke.Freshness.STALE
    assert results[SPY].freshness is smoke.Freshness.NOT_RECORDED
    assert results[SPY].updated_at is None


class _FakeHTTPResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def fake_dashboard(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int = 200,
    body: object = None,
    error: Exception | None = None,
) -> None:
    def urlopen(url: str, timeout: float = 0.0) -> _FakeHTTPResponse:
        if error is not None:
            raise error
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        return _FakeHTTPResponse(status, payload)

    monkeypatch.setattr(health_module.urllib.request, "urlopen", urlopen)


def test_a_healthy_dashboard_is_reported_and_its_fields_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_dashboard(
        monkeypatch,
        body={"account": {"safe_to_trade": True}, "reconciliation": {"status": "CLEAN"}},
    )

    result = health_module.dashboard_health("https://dash.local/health")

    assert result.available is True
    assert result.status_code == 200
    assert set(result.payload_keys) == {"account", "reconciliation"}
    assert result.credential_fields == ()


def test_a_dashboard_exposing_a_credential_field_is_a_finding(
    monkeypatch: pytest.MonkeyPatch,
    writer: sqlite3.Connection,
    database_path: Path,
    credentials: None,
) -> None:
    """The field name is reported. Its value is never read or printed."""
    fake_dashboard(monkeypatch, body={"account": {"alpaca_api_key": "PKNEVERREAL"}})
    seed_reconciliation(writer)

    result = health_module.dashboard_health("https://dash.local/health")
    assert result.credential_fields == ("account.alpaca_api_key",)
    assert "PKNEVERREAL" not in result.detail

    with open_readonly(database_path) as reader:
        report = run_preflight(reader, dashboard_url="https://dash.local/health")
    assert check_named(report, "dashboard").verdict is SmokeVerdict.FAIL


@pytest.mark.parametrize(
    "kwargs",
    [
        {"error": OSError("connection refused")},
        {"status": 500},
        {"body": b"<html>not json</html>"},
    ],
)
def test_a_broken_dashboard_never_blocks_broker_verification(
    monkeypatch: pytest.MonkeyPatch,
    writer: sqlite3.Connection,
    database_path: Path,
    credentials: None,
    kwargs: dict[str, object],
) -> None:
    """The broker and the database are the authorities. A view is not one."""
    fake_dashboard(monkeypatch, **kwargs)  # type: ignore[arg-type]
    seed_reconciliation(writer)

    with open_readonly(database_path) as reader:
        report = run_preflight(reader, dashboard_url="https://dash.local/health")

    assert health_module.dashboard_health("https://dash.local/health").available is False
    assert check_named(report, "dashboard").verdict is SmokeVerdict.PASS
    assert report.ready is True


def test_a_non_http_dashboard_url_is_never_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    """`file://` handed to urlopen would read the local disk, not a service."""

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("a non-http URL must not be fetched")

    monkeypatch.setattr(health_module.urllib.request, "urlopen", blocked)

    result = health_module.dashboard_health("file:///etc/passwd")

    assert result.available is False
    assert "http:// or https://" in result.detail


def test_no_dashboard_url_means_no_check_rather_than_a_failure() -> None:
    result = health_module.dashboard_health(None)
    assert result.available is False
    assert "no dashboard check was performed" in result.detail


# --------------------------------------------------------------------------
# Git state
# --------------------------------------------------------------------------


def test_git_state_reports_unknown_for_a_directory_that_is_not_a_repository(
    tmp_path: Path,
) -> None:
    """Unknown, not clean. A tree reported clean but never checked is worse."""
    state = gitinfo_module.git_state(tmp_path)

    assert state.known is False
    assert state.sha is None
    assert state.dirty is None
    assert state.short_sha == "unknown"


def test_git_state_reads_the_repository_this_test_runs_in() -> None:
    state = gitinfo_module.git_state(Path(smoke.__file__).resolve().parents[3])

    assert state.known is True
    assert len(state.sha or "") == 40
    assert state.dirty in (True, False)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def use_fake_broker(monkeypatch: pytest.MonkeyPatch, client: object | None) -> None:
    """Point the CLI's one client factory at a fake. No network, no credentials."""

    def factory() -> object:
        if client is None:
            raise smoke.BrokerUnreadableError("no broker in this test")
        return client

    monkeypatch.setattr(broker_module, "open_paper_client", factory)


def test_the_cli_preflight_exits_zero_when_ready(
    monkeypatch: pytest.MonkeyPatch,
    writer: sqlite3.Connection,
    database_path: Path,
    credentials: None,
    tmp_path: Path,
) -> None:
    use_fake_broker(monkeypatch, FakeBrokerClient(positions=[make_position()]))
    seed_reconciliation(writer)
    snapshot = tmp_path / "baseline.json"

    result = runner.invoke(
        app,
        [
            "preflight",
            "--db",
            str(database_path),
            "--repo",
            str(Path(smoke.__file__).resolve().parents[3]),
            "--allow-dirty",
            "--write-baseline",
            "--baseline-path",
            str(snapshot),
        ],
    )

    assert result.exit_code == 0, result.output
    assert READY_FOR_PAPER_SMOKE in result.output
    assert snapshot.exists()
    assert read_baseline(snapshot).positions[BTC] == SETTLED_BTC


def test_the_cli_preflight_exits_one_when_blocked(
    monkeypatch: pytest.MonkeyPatch,
    writer: sqlite3.Connection,
    database_path: Path,
    credentials: None,
) -> None:
    use_fake_broker(monkeypatch, FakeBrokerClient())
    seed_intent(writer, client_order_id="autotrader-unknown-1", status=INTENT_STATUS_UNKNOWN)

    result = runner.invoke(app, ["preflight", "--db", str(database_path), "--allow-dirty"])

    assert result.exit_code == 1
    assert BLOCKED in result.output
    assert "DO NOT RETRY THE ORIGINAL ORDER" in result.output


def test_the_cli_inspect_order_exits_two_when_truth_is_unresolved(
    monkeypatch: pytest.MonkeyPatch, database_path: Path, credentials: None
) -> None:
    """Its own exit code, so a script can never read it as "no such order"."""
    use_fake_broker(monkeypatch, FakeBrokerClient(orders={"autotrader-buy-1": timeout_error()}))

    result = runner.invoke(
        app,
        ["inspect-order", "--client-order-id", "autotrader-buy-1", "--db", str(database_path)],
    )

    assert result.exit_code == 2
    assert ORDER_TRUTH_UNRESOLVED in result.output
    assert DO_NOT_RETRY_BANNER in result.output


def test_the_cli_cleanup_plan_prints_a_command_it_does_not_run(
    monkeypatch: pytest.MonkeyPatch, database_path: Path, credentials: None
) -> None:
    """The proof that generation is not submission: the fake would raise."""
    client = FakeBrokerClient(positions=[make_position()])
    use_fake_broker(monkeypatch, client)
    monkeypatch.setattr(broker_module, "read_asset_spec", lambda c, s: crypto_asset())
    monkeypatch.setattr(broker_module, "read_reference_price", lambda s: 100000.0)

    result = runner.invoke(app, ["cleanup-plan", "--symbol", BTC, "--db", str(database_path)])

    assert result.exit_code == 0, result.output
    assert "USER MUST EXECUTE EXACTLY ONCE" in result.output
    assert "--qty 0.000166632" in result.output
    assert "0.00016705" not in result.output
    assert "It did not run it and cannot." in result.output


def test_the_cli_cleanup_plan_exits_one_when_the_position_cannot_be_closed(
    monkeypatch: pytest.MonkeyPatch, database_path: Path, credentials: None
) -> None:
    use_fake_broker(
        monkeypatch, FakeBrokerClient(positions=[make_position(qty="0.00002", market_value="2")])
    )
    monkeypatch.setattr(broker_module, "read_asset_spec", lambda c, s: crypto_asset())
    monkeypatch.setattr(broker_module, "read_reference_price", lambda s: 100000.0)

    result = runner.invoke(app, ["cleanup-plan", "--symbol", BTC, "--db", str(database_path)])

    assert result.exit_code == 1
    assert CleanupVerdict.NOT_POSSIBLE.value in result.output
    assert "paper-submit" not in result.output


def test_the_cli_final_audit_exits_zero_on_a_finished_smoke(
    monkeypatch: pytest.MonkeyPatch,
    writer: sqlite3.Connection,
    database_path: Path,
    credentials: None,
    tmp_path: Path,
) -> None:
    use_fake_broker(monkeypatch, FakeBrokerClient())
    seed_reconciliation(writer)
    snapshot = write_baseline(flat_baseline(), tmp_path / "baseline.json")

    result = runner.invoke(
        app,
        [
            "final-audit",
            "--db",
            str(database_path),
            "--baseline",
            str(snapshot),
            "--repo",
            str(Path(smoke.__file__).resolve().parents[3]),
        ],
    )

    if result.exit_code != 0:
        # A dirty working tree is a legitimate finding, not a broken test.
        assert "working tree has uncommitted changes" in result.output
        assert SMOKE_INCOMPLETE in result.output
    else:
        assert EXPOSURE_RESTORED in result.output
        assert SMOKE_COMPLETE in result.output


def test_the_cli_final_audit_reports_residual_exposure(
    monkeypatch: pytest.MonkeyPatch,
    writer: sqlite3.Connection,
    database_path: Path,
    credentials: None,
    tmp_path: Path,
) -> None:
    use_fake_broker(
        monkeypatch,
        FakeBrokerClient(positions=[make_position(qty="0.000000418", market_value="0.04")]),
    )
    seed_reconciliation(writer)
    snapshot = write_baseline(flat_baseline(), tmp_path / "baseline.json")

    result = runner.invoke(
        app, ["final-audit", "--db", str(database_path), "--baseline", str(snapshot)]
    )

    assert result.exit_code == 1
    assert EXPOSURE_NOT_RESTORED in result.output
    assert SMOKE_INCOMPLETE in result.output


def test_the_cli_refuses_a_missing_database_rather_than_creating_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, credentials: None
) -> None:
    use_fake_broker(monkeypatch, FakeBrokerClient())
    missing = tmp_path / "absent.db"

    result = runner.invoke(app, ["preflight", "--db", str(missing), "--allow-dirty"])

    assert result.exit_code == 1
    assert "No operational database" in result.output
    assert not missing.exists()


def test_the_cli_sequence_marks_the_two_steps_that_place_orders() -> None:
    result = runner.invoke(app, ["sequence", "--symbol", BTC])

    assert result.exit_code == 0
    assert "YOU run ONE paper BUY" in result.output
    assert "YOU run ONE cleanup SELL" in result.output
    assert "This harness runs none of them." in result.output
    assert "reconcile" in result.output


def test_every_cli_command_runs_without_a_network(
    monkeypatch: pytest.MonkeyPatch, database_path: Path, credentials: None
) -> None:
    """The whole program, end to end, with sockets blocked."""

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the smoke harness must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    use_fake_broker(monkeypatch, FakeBrokerClient(positions=[make_position()]))
    monkeypatch.setattr(broker_module, "read_asset_spec", lambda c, s: crypto_asset())
    monkeypatch.setattr(broker_module, "read_reference_price", lambda s: 100000.0)

    for arguments in (
        ["preflight", "--db", str(database_path), "--allow-dirty"],
        ["inspect-order", "--client-order-id", "autotrader-buy-1", "--db", str(database_path)],
        ["cleanup-plan", "--symbol", BTC, "--db", str(database_path)],
        ["final-audit", "--db", str(database_path), "--no-baseline"],
        ["sequence"],
    ):
        result = runner.invoke(app, arguments)
        assert result.exit_code in (0, 1, 2), (arguments, result.output)
        assert not isinstance(result.exception, AssertionError), arguments


# ---------------------------------------------------------------------------
# The broker names one market two ways
#
# Alpaca returns `BTC/USD` on an order and `BTCUSD` on the position that order
# creates. Every test above builds its fake positions with the slashed
# spelling, which is exactly why this went unnoticed until a real smoke ran:
# matching on the literal string turned a live position into a confident zero,
# and a confident zero is what the cleanup planner and the final audit both
# treat as "nothing to do".
# ---------------------------------------------------------------------------

BROKER_BTC = "BTCUSD"


def test_a_position_the_broker_spells_without_a_slash_is_still_found() -> None:
    client = FakeBrokerClient(
        positions=[make_position(BROKER_BTC, qty="0.000322094", market_value="24.92")]
    )
    live = broker_module.read_positions(client)

    found = broker_module.position_for(live, BTC)
    assert found.quantity == Decimal("0.000322094"), (
        "the broker's own position spelling must not hide it from a canonical lookup"
    )


def test_a_flat_symbol_is_still_reported_flat() -> None:
    """The fix must not turn 'not held' into a false match."""
    client = FakeBrokerClient(
        positions=[make_position(BROKER_BTC, qty="0.000322094", market_value="24.92")]
    )
    live = broker_module.read_positions(client)

    assert broker_module.position_for(live, ETH).quantity == Decimal(0)
    assert broker_module.position_for(live, "SPY").quantity == Decimal(0)


def test_a_baseline_quantity_is_found_by_either_spelling() -> None:
    baseline = flat_baseline()
    assert baseline.quantity_for(BTC) == Decimal(0)
    assert baseline.quantity_for(BROKER_BTC) == Decimal(0)


def test_the_final_audit_sees_a_position_the_broker_spelled_without_a_slash(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    """The failure this pins is a FALSE GREEN, which is the worst kind here.

    A smoke BUY that is still held must never be reported as restored exposure.
    """
    seed_reconciliation(writer)
    client = FakeBrokerClient(
        positions=[make_position(BROKER_BTC, qty="0.000322094", market_value="24.92")]
    )

    with open_readonly(database_path) as reader:
        result = run_audit(reader, client=client, baseline=flat_baseline())

    assert result.exposure_text() == EXPOSURE_NOT_RESTORED
    assert result.complete is False


def test_the_audit_compares_one_row_per_market_not_one_per_spelling(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    """Two spellings of one holding must not become two comparisons."""
    seed_reconciliation(writer)
    client = FakeBrokerClient(
        positions=[make_position(BROKER_BTC, qty="0.000322094", market_value="24.92")]
    )

    with open_readonly(database_path) as reader:
        result = run_audit(reader, client=client, baseline=flat_baseline())

    symbols = [comparison.symbol for comparison in result.report.comparisons]
    assert len(symbols) == len(set(symbols)), symbols
    keys = [symbol.replace("/", "") for symbol in symbols]
    assert len(keys) == len(set(keys)), f"one market compared twice: {symbols}"


def test_preflight_does_not_call_a_tracked_market_untracked(
    writer: sqlite3.Connection, database_path: Path
) -> None:
    """`BTCUSD` is the BTC/USD the universe already tracks, not a surprise holding."""
    seed_reconciliation(writer)
    client = FakeBrokerClient(
        positions=[make_position(BROKER_BTC, qty="0.000322094", market_value="24.92")]
    )

    with open_readonly(database_path) as reader:
        report = run_preflight(reader, client=client)

    positions = check_named(report, "broker.positions")
    assert "0.000322094" in positions.detail, positions.detail
    assert "untracked" not in positions.detail, positions.detail


def test_a_tracked_market_is_reported_in_the_canonical_spelling() -> None:
    """The generated cleanup command has to be runnable.

    The execution layer accepts `BTC/USD` and refuses `BTCUSD`, so a plan
    rendered in the broker's own position spelling prints a line that fails.
    """
    client = FakeBrokerClient(
        positions=[make_position(BROKER_BTC, qty="0.000322094", market_value="24.92")]
    )
    live = broker_module.read_positions(client)

    snapshot = broker_module.position_for(live, BTC)
    assert snapshot.symbol == BTC, snapshot.symbol


def test_an_untracked_market_keeps_the_brokers_own_spelling() -> None:
    """There is nothing to map it to, and inventing a pair form is a guess."""
    client = FakeBrokerClient(positions=[make_position("DOGEUSD", qty="1", market_value="1")])
    live = broker_module.read_positions(client)

    assert [p.symbol for p in live.values()] == ["DOGEUSD"]


def test_the_cleanup_plan_names_a_symbol_the_submit_path_accepts() -> None:
    """End to end: broker spelling in, canonical spelling out."""
    client = FakeBrokerClient(
        positions=[make_position(BROKER_BTC, qty="0.000322094", market_value="24.92")]
    )
    live = broker_module.read_positions(client)

    plan = plan_cleanup(
        position=broker_module.position_for(live, BTC),
        asset=crypto_asset(),
        quoted_price=77432.9,
    )
    assert plan.verdict is CleanupVerdict.REQUIRED
    assert plan.symbol == BTC, plan.symbol
    assert plan.position_quantity == Decimal("0.000322094")
    assert plan.plan_quantity == Decimal("0.000322094")
    assert plan.residual_quantity == Decimal(0)
    # The command an operator is invited to paste must name the tradable form.
    assert plan.command is not None
    assert f"--symbol {BTC}" in plan.command, plan.command
