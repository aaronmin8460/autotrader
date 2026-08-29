"""Trade and event accounting: turning fills into round trips.

A fill is a cash event; a trade is a round trip. Almost every metric worth
reporting - win rate, average trade, profit factor, holding period - is defined
over round trips, so the pairing has to happen somewhere explicit rather than
inside a metrics function where nobody can see the rule that produced it.

**The rule.** A BUY opens a position and the matching SELL closes it. This is
long-only with at most one open position, so pairing is unambiguous: fills
alternate, and each closing fill settles the whole open quantity. There is no
partial exit, no pyramiding and no FIFO/LIFO question to answer, because the
replay simulator never creates one.

**An open position at the end is not a trade.** It is reported separately as an
`OpenPosition`, and its profit is unrealized. Folding it into the trade list -
"closing" it at the final bar's mark - would inflate the trade count, invent a
win or a loss the strategy never took, and make a result depend on where the
dataset happens to end. Realized and unrealized are kept apart for exactly that
reason.

Fees are charged to the trade that incurred them: a round trip's net PnL is
gross PnL less both sides' fees, so a strategy cannot look profitable by
ignoring what it paid to get in.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

import pandas as pd

_ZERO = Decimal(0)


class TradeAccountingError(Exception):
    """A fill sequence could not be paired into round trips.

    Every case that raises this is a defect in whatever produced the fills, not
    bad input from a user: fills that do not alternate, or a SELL with no
    matching BUY, mean the simulator broke its own invariant.
    """


class FillSide(Enum):
    """The market side of one simulated fill."""

    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Fill:
    """One simulated execution, with its costs broken out.

    `reference_price` is the bar price the fill was derived from and
    `fill_price` is what it actually transacted at once adverse slippage was
    applied; keeping both means a study can always say how much of its cost was
    an assumption. `fee` and `slippage_cost` are both positive on both sides.

    `signal_timestamp` is the bar whose close produced the proposal and
    `timestamp` is the strictly later bar it filled on - the two are separate
    fields rather than one so that no-look-ahead is visible in the data a
    result carries, not only in the code that produced it.
    """

    signal_timestamp: pd.Timestamp
    timestamp: pd.Timestamp
    bar_index: int
    symbol: str
    side: FillSide
    quantity: Decimal
    reference_price: Decimal
    fill_price: Decimal
    fee: Decimal
    slippage_cost: Decimal
    cash_after: Decimal
    reason: str

    @property
    def notional(self) -> Decimal:
        """Traded value at the fill price, before fees."""
        return self.quantity * self.fill_price

    @property
    def total_cost(self) -> Decimal:
        """Everything this side paid beyond the reference-price notional."""
        return self.fee + self.slippage_cost


@dataclass(frozen=True)
class Trade:
    """One completed round trip: a BUY and the SELL that closed it."""

    symbol: str
    entry_timestamp: pd.Timestamp
    exit_timestamp: pd.Timestamp
    entry_bar_index: int
    exit_bar_index: int
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    slippage_cost: Decimal
    entry_reason: str
    exit_reason: str

    @property
    def gross_pnl(self) -> Decimal:
        """Price move times size, before any cost."""
        return (self.exit_price - self.entry_price) * self.quantity

    @property
    def fees(self) -> Decimal:
        """Both sides' venue fees."""
        return self.entry_fee + self.exit_fee

    @property
    def net_pnl(self) -> Decimal:
        """What the round trip actually made or lost, fees included.

        Slippage is already inside `entry_price` and `exit_price` - the fill
        happened there - so it is not subtracted again here. `slippage_cost`
        records what that adjustment cost for reporting, and adding it a second
        time would double-charge every trade.
        """
        return self.gross_pnl - self.fees

    @property
    def cost_basis(self) -> Decimal:
        """Cash the entry consumed, its fee included."""
        return self.quantity * self.entry_price + self.entry_fee

    @property
    def return_fraction(self) -> Decimal:
        """Net PnL over cost basis. Zero when the basis is zero."""
        basis = self.cost_basis
        return self.net_pnl / basis if basis > 0 else _ZERO

    @property
    def bars_held(self) -> int:
        """How many bars the position was open for."""
        return self.exit_bar_index - self.entry_bar_index

    @property
    def is_win(self) -> bool:
        """True when the round trip made money after costs.

        Strictly positive: a scratch trade is not a win. Win rate is easy to
        flatter by counting break-even as a victory, so it is not counted here.
        """
        return self.net_pnl > 0

    def to_json_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "entry_timestamp": str(self.entry_timestamp),
            "exit_timestamp": str(self.exit_timestamp),
            "bars_held": self.bars_held,
            "quantity": str(self.quantity),
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "gross_pnl": str(self.gross_pnl),
            "fees": str(self.fees),
            "net_pnl": str(self.net_pnl),
            "return_fraction": str(self.return_fraction),
            "entry_reason": self.entry_reason,
            "exit_reason": self.exit_reason,
        }


@dataclass(frozen=True)
class OpenPosition:
    """A position still held when the dataset ran out.

    Deliberately not a `Trade`. Its profit is unrealized and depends entirely
    on where the sample ends, so it is reported under its own name and never
    counted in win rate, average trade or profit factor.
    """

    symbol: str
    entry_timestamp: pd.Timestamp
    entry_bar_index: int
    quantity: Decimal
    entry_price: Decimal
    entry_fee: Decimal
    mark_price: Decimal
    entry_reason: str

    @property
    def market_value(self) -> Decimal:
        return self.quantity * self.mark_price

    @property
    def cost_basis(self) -> Decimal:
        return self.quantity * self.entry_price + self.entry_fee

    @property
    def unrealized_pnl(self) -> Decimal:
        """Mark less basis. Never counted as realized, and never a trade."""
        return self.market_value - self.cost_basis

    def to_json_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "entry_timestamp": str(self.entry_timestamp),
            "quantity": str(self.quantity),
            "entry_price": str(self.entry_price),
            "mark_price": str(self.mark_price),
            "market_value": str(self.market_value),
            "unrealized_pnl": str(self.unrealized_pnl),
            "entry_reason": self.entry_reason,
        }


def build_trades(
    fills: Sequence[Fill],
    *,
    final_mark_price: Decimal | None = None,
) -> tuple[tuple[Trade, ...], OpenPosition | None]:
    """Pair `fills` into round trips, plus whatever position stayed open.

    `fills` must be in execution order and must alternate BUY, SELL, BUY, ...
    Anything else is a simulator invariant violation and raises rather than
    being patched over, because a quietly repaired pairing produces a trade
    list that no longer describes what happened.

    `final_mark_price` is the price a trailing open position is marked at. When
    a position is open and no mark is supplied, its entry price is used - which
    reports zero unrealized profit rather than inventing a number.
    """
    trades: list[Trade] = []
    open_fill: Fill | None = None

    for fill in fills:
        if fill.side is FillSide.BUY:
            if open_fill is not None:
                raise TradeAccountingError(
                    f"A BUY at {fill.timestamp} arrived while a position opened at "
                    f"{open_fill.timestamp} was still open. The replay simulator holds at "
                    "most one position and never pyramids."
                )
            open_fill = fill
            continue

        if open_fill is None:
            raise TradeAccountingError(
                f"A SELL at {fill.timestamp} has no matching BUY. The replay simulator "
                "never sells short and never exits while flat."
            )
        if fill.quantity != open_fill.quantity:
            raise TradeAccountingError(
                f"The SELL at {fill.timestamp} closed {fill.quantity} against an open "
                f"{open_fill.quantity}. Exits are always for the whole position."
            )
        trades.append(
            Trade(
                symbol=open_fill.symbol,
                entry_timestamp=open_fill.timestamp,
                exit_timestamp=fill.timestamp,
                entry_bar_index=open_fill.bar_index,
                exit_bar_index=fill.bar_index,
                quantity=open_fill.quantity,
                entry_price=open_fill.fill_price,
                exit_price=fill.fill_price,
                entry_fee=open_fill.fee,
                exit_fee=fill.fee,
                slippage_cost=open_fill.slippage_cost + fill.slippage_cost,
                entry_reason=open_fill.reason,
                exit_reason=fill.reason,
            )
        )
        open_fill = None

    if open_fill is None:
        return tuple(trades), None

    mark = open_fill.fill_price if final_mark_price is None else final_mark_price
    return tuple(trades), OpenPosition(
        symbol=open_fill.symbol,
        entry_timestamp=open_fill.timestamp,
        entry_bar_index=open_fill.bar_index,
        quantity=open_fill.quantity,
        entry_price=open_fill.fill_price,
        entry_fee=open_fill.fee,
        mark_price=mark,
        entry_reason=open_fill.reason,
    )


def realized_pnl(trades: Sequence[Trade]) -> Decimal:
    """Net profit over every closed round trip. Unrealized is never included."""
    return sum((trade.net_pnl for trade in trades), _ZERO)


def traded_notional(fills: Sequence[Fill]) -> Decimal:
    """Total value transacted across every side. The basis for turnover."""
    return sum((fill.notional for fill in fills), _ZERO)


__all__ = [
    "Fill",
    "FillSide",
    "OpenPosition",
    "Trade",
    "TradeAccountingError",
    "build_trades",
    "realized_pnl",
    "traded_notional",
]
