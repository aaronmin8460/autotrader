"""M1: the offline machine-learning data foundation for a future decision model.

This package builds **datasets and contracts**. It does not trade, does not
decide, and is not wired into any runtime. Nothing here is imported by the
crypto runtime, the equity runtime, the risk engine, the execution boundary, or
reconciliation, and a test asserts that in both directions.

**What this milestone is.** A versioned feature-dataset schema, a historical
dataset builder over already-stored bars, a configurable label framework, a
temporal split with purging and an embargo, a probability-model interface, a
calibration interface, artifact metadata, and a filesystem model registry.
That is the whole of it.

**What this milestone is deliberately not.** There is no trained trading model,
no strategy, no signal, no activation switch, and no registry stage named
`PRODUCTION`. Choosing V4's model is an evidence-driven decision that has not
been made and cannot be made from here; this package exists so that the
evidence can be produced reproducibly when it is.

**Offline by construction.** Every input is a file that already exists on disk:
a canonical Parquet bar dataset written by `autotrader.data.historical` or
`autotrader.equity.data`, and - for equities - an explicit session calendar
exported once by an operator. No module in this package constructs a market
data client or a broker client, opens a socket, or reads a credential. A test
blocks `socket.socket` and builds a dataset anyway.

**Completed bars only, and no look-ahead anywhere.** A feature row is stamped
with the *start* of the last completed bar it reads, and carries the instant
that row could first have existed (`knowable_at`). A label is stamped with the
exact future interval it measures and the instant that interval finished. The
two are separate columns because they are separate facts, and the split module
refuses to put a training row whose label reaches into the validation window on
the training side of the boundary.

**Two asset classes, two different clocks.** Crypto is continuous: every
15-minute UTC boundary exists, a Sunday bar is an ordinary bar, and there is no
session to be inside or outside of. Equities run regular sessions read from a
broker calendar: 09:30-16:00 Eastern, early closes, holidays, and an overnight
gap between the last bar of one session and the first bar of the next. Those
are not the same thing and this package does not pretend they are - see
`autotrader.ml.grid`, which is the single place the difference is written down.

Module map, in dependency order::

    __init__      this vocabulary: errors, asset classes, symbol universes
    storage       the external-volume boundary; refuses to write a secret
    schema        the versioned column contract and its fingerprint
    grid          the asset-class bar clock: crypto boundaries vs sessions
    features      backward-only feature computation over a grid
    labels        the configurable forward-interval label framework
    dataset       the builder: bars + grid + labels -> versioned Parquet
    splits        temporal train/validation/test with purge and embargo
    calibration   probability calibration interfaces and reliability metrics
    model         the probability-prediction contract V4 will expose
    registry      immutable artifact metadata on the filesystem
    experiment    the reproducibility record for one training run
    cli           the `autotrader ml ...` sub-application
"""

from __future__ import annotations

from enum import Enum

from autotrader.data.historical import SUPPORTED_SYMBOLS as CRYPTO_SYMBOLS
from autotrader.equity import EQUITY_SYMBOLS

#: The only bar timeframe this foundation supports, matching both products.
#:
#: Deliberately not a generic timeframe framework. Every horizon in this
#: package is counted in *bars*, and a bar is fifteen minutes in both books; a
#: second timeframe would change what "16 bars" means without changing any
#: name, which is the kind of silent redefinition a versioned dataset exists to
#: prevent.
SUPPORTED_TIMEFRAME = "15m"

#: A slash cannot appear in a flat filename. `BTC/USD` -> `BTC_USD`.
SYMBOL_SEPARATOR = "/"
SLUG_SEPARATOR = "_"


class MLError(Exception):
    """An expected, user-facing ML-foundation failure. Reported without a traceback.

    Every other error type in this package derives from it, so a caller that
    wants to catch "the ML foundation refused" can catch one thing, while a
    caller that cares *which* refusal it was still can.
    """


class AssetClass(Enum):
    """Which clock a symbol's bars run on.

    This is the only asset-class switch in the package, and it exists because
    the two clocks genuinely differ: `CRYPTO` bars occupy every 15-minute UTC
    boundary forever, and `EQUITY` bars occupy only the regular-session
    boundaries a broker calendar reports. Everything downstream that needs the
    distinction asks `autotrader.ml.grid` rather than branching again.
    """

    CRYPTO = "crypto"
    EQUITY = "equity"


def symbols_for(asset_class: AssetClass) -> tuple[str, ...]:
    """The frozen symbol universe of `asset_class`.

    Both universes are closed lists owned elsewhere - the crypto pairs by C1,
    the ten equity symbols by Equity V0.2 - and are re-exported rather than
    restated, so a universe change lands in one place.
    """
    if not isinstance(asset_class, AssetClass):
        raise MLError(f"asset_class must be an AssetClass, got {type(asset_class).__name__}.")
    return CRYPTO_SYMBOLS if asset_class is AssetClass.CRYPTO else EQUITY_SYMBOLS


def asset_class_for_symbol(symbol: str) -> AssetClass:
    """Which universe `symbol` belongs to, refusing anything in neither.

    Resolved from the universes rather than from the presence of a slash: the
    spelling of a symbol is a provider convention, and inferring an asset class
    from punctuation would quietly accept `FOO/BAR` as crypto.
    """
    if not isinstance(symbol, str):
        raise MLError(f"symbol must be a string, got {type(symbol).__name__}.")
    candidate = symbol.strip().upper()
    for asset_class in AssetClass:
        if candidate in symbols_for(asset_class):
            return asset_class
    known = ", ".join([*CRYPTO_SYMBOLS, *EQUITY_SYMBOLS])
    raise MLError(f"Unknown symbol: {symbol!r}. Known symbols are: {known}.")


def normalize_symbol(symbol: str, asset_class: AssetClass | None = None) -> str:
    """Uppercase `symbol` and confirm it is in the expected universe.

    With `asset_class` supplied the symbol must be in *that* universe, which is
    what a dataset builder wants: a dataset declared as equity that names
    `BTC/USD` is a mislabelled dataset, not an asset-class discovery.
    """
    resolved = asset_class_for_symbol(symbol)
    if asset_class is not None and resolved is not asset_class:
        raise MLError(
            f"{symbol.strip().upper()!r} is a {resolved.value} symbol, but the "
            f"{asset_class.value} universe was expected."
        )
    return symbol.strip().upper()


def filesystem_slug(symbol: str) -> str:
    """The filesystem-safe form of a symbol, for filenames only.

    C1 has a slug function of its own, and it refuses an equity ticker because
    it validates against the crypto pairs first. This package spans both
    universes, so it validates against both and then applies the same one
    substitution. The stored data always keeps the canonical spelling.
    """
    return normalize_symbol(symbol).replace(SYMBOL_SEPARATOR, SLUG_SEPARATOR)


def normalize_timeframe(timeframe: str) -> str:
    """Confirm `timeframe` is the single timeframe this foundation supports."""
    if not isinstance(timeframe, str):
        raise MLError(f"timeframe must be a string, got {type(timeframe).__name__}.")
    normalized = timeframe.strip().lower()
    if normalized != SUPPORTED_TIMEFRAME:
        raise MLError(
            f"Unsupported timeframe: {timeframe!r}. Only {SUPPORTED_TIMEFRAME!r} is supported."
        )
    return normalized


__all__ = [
    "CRYPTO_SYMBOLS",
    "EQUITY_SYMBOLS",
    "SLUG_SEPARATOR",
    "SUPPORTED_TIMEFRAME",
    "SYMBOL_SEPARATOR",
    "AssetClass",
    "MLError",
    "asset_class_for_symbol",
    "filesystem_slug",
    "normalize_symbol",
    "normalize_timeframe",
    "symbols_for",
]
