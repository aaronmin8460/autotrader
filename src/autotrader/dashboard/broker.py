"""C10: the dashboard's read-only view of the paper broker.

The dashboard needs exactly two facts the local database cannot supply: what
the account is worth right now, and what the broker says is actually held. Both
are reads. This module gets them and stops there.

**It imports read helpers by name, and no others.** `create_paper_trading_client`,
`fetch_paper_account_state`, and `fetch_paper_positions` are the entire import
list from `autotrader.execution.paper`. The submission entry points in that
module are never imported, never referenced, and never called, and
`tests/test_dashboard.py` asserts that against this package's executable code
with prose stripped. Python cannot make a module unreachable; what it can do is
make its absence checkable, and that is what is checked.

**It cannot even name the type that submits.** The client is typed as
`ReadableBroker` - a structural protocol carrying exactly the two read methods
this package uses - rather than as the concrete Alpaca client class, which also
carries a submission method. That keeps the repository-wide rule pinning the
broker vocabulary to `execution/` intact (`tests/test_backtest.py`), and it
says something true: what this module needs is a thing that can be read from.

**Failure is a value, not an exception that escapes.** `read_broker()` always
returns a `BrokerRead`. A missing credential, an unreachable broker, an account
in a shape the execution boundary refuses to normalize, or a short position
the system will not reason about all come back as `ok=False` with a machine
reason code - never as a traceback, and never as a partial account with some
fields guessed. A dashboard that crashed because the broker was down would be
worse than useless at exactly the moment it is needed.

**No credential ever leaves here.** Keys are read inside the execution
boundary, are never returned from it, and are never placed on a `BrokerRead`.
The failure path deliberately discards the underlying exception text rather
than forwarding it: an authentication error's message is the single most
likely place for a key fragment or an account identifier to appear, and this
module's whole output is bound for a browser.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from autotrader.dashboard.models import (
    UNAVAILABLE_BROKER_NOT_CONFIGURED,
    UNAVAILABLE_BROKER_UNREADABLE,
)
from autotrader.execution.paper import (
    PaperAccountState,
    PaperPosition,
    create_paper_trading_client,
    credentials_configured,
    fetch_paper_account_state,
    fetch_paper_positions,
)


@runtime_checkable
class ReadableBroker(Protocol):
    """The two broker capabilities this dashboard needs, and no others.

    Deliberately structural. The concrete client the paper factory returns has
    a submission method on it; this type does not, so nothing typed as a
    `ReadableBroker` can be *asked* to submit without the name of the method
    appearing in this package - which is the thing the audit looks for.
    """

    def get_account(self) -> object: ...

    def get_all_positions(self) -> list[object]: ...


@dataclass(frozen=True)
class BrokerRead:
    """One attempt to read the paper account, successful or not.

    `ok` is the whole contract, exactly as in `Amount`: when it is False both
    `account` and `positions` are None and `reason` names which read failed.
    There is no partially populated success.
    """

    ok: bool
    account: PaperAccountState | None = None
    positions: dict[str, PaperPosition] | None = None
    reason: str | None = None

    @property
    def tradable(self) -> bool | None:
        """Whether the broker considers the account able to trade at all."""
        return None if self.account is None else self.account.tradable


def read_broker(client: ReadableBroker | None = None) -> BrokerRead:
    """Read the paper account and its open positions, or say why not.

    `client` exists so tests can supply a fake and so a caller that already
    holds a client does not build a second one. When it is None a paper client
    is constructed here - `create_paper_trading_client` hardcodes `paper=True`
    and there is no parameter anywhere in this repository that changes that.

    Every exception is caught deliberately, including ones this module cannot
    enumerate: the broker SDK, its transport, and its retry layer can raise
    types this package does not import, and a dashboard whose whole job is to
    report on a degraded system must not be the thing that falls over first.
    """
    if client is None and not credentials_configured():
        return BrokerRead(ok=False, reason=UNAVAILABLE_BROKER_NOT_CONFIGURED)
    try:
        broker = create_paper_trading_client() if client is None else client
        account = fetch_paper_account_state(broker)
        positions = fetch_paper_positions(broker)
    except Exception:  # noqa: BLE001 - see the docstring; the text is discarded on purpose
        return BrokerRead(ok=False, reason=UNAVAILABLE_BROKER_UNREADABLE)
    return BrokerRead(ok=True, account=account, positions=positions)


class SharedBrokerReader:
    """One paper client, built once, read by one caller at a time.

    A dashboard polling every five seconds with two tabs open would otherwise
    build a fresh HTTP session per poll per tab and read the same account
    twice, against an account the trading runtime also depends on. One client
    and one in-flight read is the whole mechanism: a page that watches the
    system must not become a reason the system is slower.

    A client is cached only once it has answered. A read that fails drops it,
    so a broker that comes back does not have to wait for a process restart -
    and a client that could not be built is never remembered as one that could.
    """

    def __init__(self, client: ReadableBroker | None = None) -> None:
        self._lock = threading.Lock()
        self._client = client

    def read(self) -> BrokerRead:
        """Read the account, reusing the shared client when there is one."""
        with self._lock:
            if self._client is None:
                if not credentials_configured():
                    return BrokerRead(ok=False, reason=UNAVAILABLE_BROKER_NOT_CONFIGURED)
                try:
                    self._client = create_paper_trading_client()
                except Exception:  # noqa: BLE001 - the text is discarded on purpose
                    return BrokerRead(ok=False, reason=UNAVAILABLE_BROKER_UNREADABLE)
            result = read_broker(self._client)
            if not result.ok:
                self._client = None
            return result


__all__ = ["BrokerRead", "ReadableBroker", "SharedBrokerReader", "read_broker"]
