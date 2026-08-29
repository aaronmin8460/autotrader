"""Read-only broker access for the harness. Reads only; there is no write here.

Every call in this module is a `GET`. There is no submission, no cancellation,
no replacement, and no position close - not behind a flag, not behind an
environment variable, and not behind a confirmation token, because none of
those exist here to open.

**No Alpaca import.** The harness reaches the broker only through
`autotrader.execution.paper`, which is the single file in this repository that
constructs a trading client and the single file that can submit an order. This
module imports the *reading* half of that boundary and nothing else, so
`autotrader.smoke` contains no broker SDK import at all - a property a test
asserts, and one that makes "this package cannot trade" checkable by reading
its import list.

**A failed read is not an empty result.** Every function here keeps the two
apart. `read_order` returns `NOT_FOUND` only when the broker positively said
so, and `UNRESOLVED` for a timeout, a `5xx`, or an unreadable answer; a caller
that collapses the two would report an order as absent when it may be live,
which is exactly how a smoke becomes two orders instead of one.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum

from autotrader.execution import paper
from autotrader.execution.models import TRADABLE_SYMBOLS, ExecutionError
from autotrader.execution.paper import broker_symbol_key
from autotrader.smoke.models import (
    BrokerReadClient,
    BrokerUnreadableError,
    PositionSnapshot,
    SmokeInputError,
)
from autotrader.smoke.readonly import is_crypto_symbol, normalize_smoke_symbol


class LookupOutcome(Enum):
    """The three answers an order lookup can give. Never two of them at once.

    Mirrors `reconciliation.engine._LookupOutcome` on purpose - the same
    distinction, drawn the same way, so an operator reading a harness report
    and a reconciliation report is reading one vocabulary. It is redefined
    rather than imported because that one is private to its module.
    """

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    UNRESOLVED = "UNRESOLVED"


def credentials_present() -> bool:
    """Whether both Alpaca credential variables hold a value.

    Reports presence only. The values are never read into this package, never
    printed, and never written to a snapshot.
    """
    return paper.credentials_configured()


def paper_gate_open() -> bool:
    """Whether the paper-submission environment gate is currently open.

    Read purely so the preflight can *report* it. Nothing here acts on the
    answer, and nothing here can set it: the harness has no submission path
    for an open gate to enable.
    """
    return paper.paper_trading_enabled()


def open_paper_client() -> BrokerReadClient:
    """Build the paper trading client, and prove it is the paper one.

    Delegates to the single hardcoded-`paper=True` factory and then re-checks
    the constructed object through `verify_paper_environment`, exactly as
    reconciliation does. This process is about to print numbers an operator
    will act on; confirming which broker they came from costs one attribute
    read and no network call.
    """
    try:
        client = paper.create_paper_trading_client()
        paper.verify_paper_environment(client)
    except ExecutionError as error:
        raise BrokerUnreadableError(str(error)) from None
    return client


def read_account(client: BrokerReadClient) -> paper.PaperAccountState:
    """The paper account, normalized. Raises rather than guessing."""
    try:
        return paper.fetch_paper_account_state(client)
    except ExecutionError as error:
        raise BrokerUnreadableError(f"Could not read the paper account: {error}") from None
    except Exception as error:  # noqa: BLE001 - an unreadable broker must fail closed
        raise BrokerUnreadableError(
            f"Could not read the paper account ({type(error).__name__})."
        ) from None


#: Canonical spelling for every market this build trades, keyed by market.
_CANONICAL_BY_MARKET = {broker_symbol_key(symbol): symbol for symbol in TRADABLE_SYMBOLS}


def display_symbol(symbol: str) -> str:
    """Render a broker symbol the way the rest of the system spells it.

    The broker returns `BTCUSD` for a position and `BTC/USD` for the order that
    created it. Reports should read in one vocabulary, and the cleanup command
    this harness prints has to use the spelling the execution layer will
    actually accept: the submit path takes only the canonical pair form and
    refuses `BTCUSD` outright, so a plan rendered in the broker's spelling
    hands the operator a line that cannot run.

    A market this build does not track keeps the broker's own spelling. There
    is nothing to map it to, and inventing a pair form would be a guess printed
    as fact.
    """
    return _CANONICAL_BY_MARKET.get(broker_symbol_key(symbol), normalize_smoke_symbol(symbol))


def read_positions(client: BrokerReadClient) -> dict[str, PositionSnapshot]:
    """Every open broker position, keyed by `broker_symbol_key`.

    **This is the authoritative quantity** for everything downstream. A short
    position makes the underlying reader raise, and that is surfaced as an
    unreadable broker rather than swallowed: this system is long only, and a
    harness that quietly ignored a short would plan a cleanup around exposure
    it had not accounted for.

    The key is slash-insensitive because the broker is not consistent with
    itself: the same market is `BTC/USD` on an order and `BTCUSD` on the
    position that order creates. Keying by the literal spelling means a
    position looked up by its canonical pair name is never found - and
    `position_for` reports "not mentioned" as a confident flat zero, so the
    miss reads as an authoritative "you hold nothing" rather than as an error.
    """
    try:
        positions = paper.fetch_paper_positions(client)
    except ExecutionError as error:
        raise BrokerUnreadableError(f"Could not read broker positions: {error}") from None
    except Exception as error:  # noqa: BLE001 - an unreadable broker must fail closed
        raise BrokerUnreadableError(
            f"Could not read broker positions ({type(error).__name__})."
        ) from None
    return {
        broker_symbol_key(position.symbol): PositionSnapshot(
            symbol=display_symbol(position.symbol),
            quantity=position.quantity,
            market_value=position.market_value,
            average_entry_price=position.average_entry_price,
        )
        for position in positions.values()
    }


def position_for(positions: dict[str, PositionSnapshot], symbol: str) -> PositionSnapshot:
    """The broker's position in `symbol`, or an explicit zero.

    A symbol the broker did not mention is flat, and a flat position is a real
    answer rather than a missing one - so it is returned as a quantity of zero
    instead of `None`. Callers then have one shape to reason about, and no
    caller can accidentally read "absent" as "unknown".

    That guarantee is exactly why the lookup is slash-insensitive. Alpaca
    names the same market two ways - `BTC/USD` on the order, `BTCUSD` on the
    resulting position - so matching on the literal spelling turns a real
    position into a confident zero. Downstream that is not a cosmetic miss:
    the cleanup planner would report NO_CLEANUP_REQUIRED against an open
    position, and the final audit would call the exposure restored while the
    smoke's own BUY was still held.

    The returned snapshot keeps the **caller's** spelling when the broker is
    flat, so a report reads in the vocabulary it was asked in.
    """
    ticker = normalize_smoke_symbol(symbol)
    found = positions.get(broker_symbol_key(ticker))
    if found is not None:
        return found
    return PositionSnapshot(symbol=ticker, quantity=Decimal(0), market_value=0.0)


def read_order(
    client: BrokerReadClient,
    *,
    client_order_id: str | None = None,
    broker_order_id: str | None = None,
) -> tuple[LookupOutcome, paper.BrokerOrderSnapshot | None, str]:
    """Look one order up by exactly one identifier. Read-only, always.

    Returns `(outcome, snapshot, detail)`. Exactly one identifier must be
    supplied: accepting both would invite a caller to pass a mismatched pair
    and get an answer about whichever one this function happened to try first.

    An `UNRESOLVED` outcome means the broker could not be asked, not that the
    order is missing. The caller prints `ORDER_TRUTH_UNRESOLVED` for it and
    must not act.
    """
    supplied = [value for value in (client_order_id, broker_order_id) if value]
    if len(supplied) != 1:
        raise SmokeInputError(
            "Supply exactly one of --client-order-id or --broker-order-id. Two "
            "identifiers could disagree, and an order inspector that quietly picked "
            "one would answer a question that was not asked."
        )
    try:
        if client_order_id:
            snapshot = paper.find_broker_order_by_client_id(client, client_order_id.strip())
        else:
            snapshot = paper.find_broker_order_by_broker_id(client, str(broker_order_id).strip())
    except ExecutionError as error:
        return LookupOutcome.UNRESOLVED, None, str(error)
    except Exception as error:  # noqa: BLE001 - any unexpected failure is unresolved
        return LookupOutcome.UNRESOLVED, None, f"{type(error).__name__} during order lookup"
    if snapshot is None:
        return LookupOutcome.NOT_FOUND, None, "the broker reports no order under this identifier"
    return LookupOutcome.FOUND, snapshot, f"broker status {snapshot.status}"


def read_asset_spec(client: BrokerReadClient, symbol: str) -> paper.CryptoAssetSpec | None:
    """Live broker precision metadata for a crypto pair, or None.

    None for an equity, and None for a crypto pair this build's execution layer
    does not recognise - both are the graceful-degradation path, not an error.
    The caller then plans from the policy that fits the asset class rather than
    refusing to plan at all.

    Nothing is remembered: `min_order_size` and `min_trade_increment` come from
    the broker on every call, because a stale constant produces a plan the
    broker will not accept.
    """
    ticker = normalize_smoke_symbol(symbol)
    if not is_crypto_symbol(ticker):
        return None
    try:
        return paper.fetch_crypto_asset(client, ticker)
    except ExecutionError:
        return None
    except Exception:  # noqa: BLE001 - metadata is an optimization, not a gate
        return None


def read_reference_price(symbol: str) -> float | None:
    """The current mark for `symbol`, or None when this build cannot fetch one.

    Crypto goes through the existing market-data boundary. An equity returns
    None here: `main` has no equity price path, and inventing one would be the
    speculative integration internal this harness is meant to avoid. The
    cleanup planner falls back to the broker's own reported `market_value` for
    the position, which needs no second data source and is the broker's number
    rather than a guess.
    """
    ticker = normalize_smoke_symbol(symbol)
    if not is_crypto_symbol(ticker):
        return None
    try:
        data_client = paper.create_market_data_client()
        return paper.fetch_reference_price(data_client, ticker)
    except ExecutionError:
        return None
    except Exception:  # noqa: BLE001 - a missing price degrades, it does not crash
        return None


__all__ = [
    "LookupOutcome",
    "credentials_present",
    "open_paper_client",
    "paper_gate_open",
    "display_symbol",
    "position_for",
    "read_account",
    "read_asset_spec",
    "read_order",
    "read_positions",
    "read_reference_price",
]
