"""M1: the versioned feature-dataset column contract.

A stored dataset is only reusable if what its columns *mean* is written down
next to it, so this module describes every column a built dataset can carry:
its name, its dtype, its role, what it is, and - the part that matters - how
far backwards and how far forwards in time it reads.

**`lookback_bars` and `forward_bars` are the anti-look-ahead contract.** Every
column declares both. A column with `forward_bars > 0` reads bars that had not
happened yet at `feature_timestamp`, and `ColumnSpec` refuses to let one exist
in any role except a label or a label's own metadata. That makes "no feature
sees the future" a structural property of the schema rather than a habit, and
`autotrader.ml.features` is tested against it column by column.

**The version and the fingerprint answer different questions.**
`FEATURE_SCHEMA_VERSION` is the operator-facing number that changes when the
contract changes on purpose. The fingerprint is a SHA-256 over the full column
specification, and it changes whenever *anything* about a column changes -
including a lookback quietly edited from 16 to 32 while the version stayed
put. A test asserts the two move together, so a redefinition without a version
bump fails rather than silently invalidating every dataset already on disk.

**Column order is part of the contract.** Keys, then provenance, then features,
then label metadata, then the label. A frame built today and a frame built next
year line up positionally, which is what lets two datasets be concatenated or
compared without a join.

The schema does not know which features exist - `autotrader.ml.features` owns
that list - and does not know what a label means - `autotrader.ml.labels` owns
that. `build_schema` composes the three, so each of them has exactly one owner.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

import pandas as pd

from autotrader.ml import MLError
from autotrader.ml.storage import sha256_of_record

#: The operator-facing version of the column contract below.
#:
#: Bumped by hand whenever a column is added, removed, renamed, retyped, or
#: redefined - including a change to a feature's lookback. A dataset records
#: the version it was built under, so a model trained on 1.0.0 can refuse a
#: 2.0.0 dataset instead of silently consuming differently-defined columns.
FEATURE_SCHEMA_VERSION = "1.0.0"

#: The longest trailing window any feature reads, in completed bars.
#:
#: Declared here rather than derived from the feature list, because
#: `bars_present_in_window` is a fixed provenance column that has to be sized
#: against it and this module deliberately does not import the feature list. A
#: test asserts the two agree, so growing a feature's window without widening
#: this constant fails rather than producing a completeness count that quietly
#: covers less than the features it describes.
#:
#: It is 50 because the slowest feature is the EMA 50 spread, and 50 is the
#: strategy layer's own `SLOW_PERIOD`.
FEATURE_WINDOW_BARS = 50


class SchemaError(MLError):
    """A column contract that cannot be satisfied, or a frame that violates one."""


class ColumnRole(Enum):
    """What a column is for, which decides what may be done with it.

    `FEATURE` is the only role a model may read as input. `LABEL` is the only
    role it may fit against. The rest are identity and provenance: needed to
    know *which* row this is and *when* it could have existed, and never to be
    handed to a model - `session_id` is a date string, and a model that learns
    from it has learned the calendar rather than the market.
    """

    KEY = "key"
    PROVENANCE = "provenance"
    FEATURE = "feature"
    LABEL_META = "label_meta"
    LABEL = "label"


#: The roles that are permitted to read bars later than `feature_timestamp`.
FORWARD_LOOKING_ROLES: frozenset[ColumnRole] = frozenset({ColumnRole.LABEL, ColumnRole.LABEL_META})


@dataclass(frozen=True)
class ColumnSpec:
    """One column of a built dataset, and the time window it reads.

    `lookback_bars` counts completed bars *at or before* `feature_timestamp`
    that the value depends on; a column computed from this bar alone declares
    `1`. `forward_bars` counts bars strictly after it, and is zero for
    everything that is not a label.
    """

    name: str
    dtype: str
    role: ColumnRole
    description: str
    lookback_bars: int = 1
    forward_bars: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise SchemaError(f"A column name must be a non-empty string, got {self.name!r}.")
        if not isinstance(self.role, ColumnRole):
            raise SchemaError(f"{self.name}: role must be a ColumnRole, got {self.role!r}.")
        if not isinstance(self.description, str) or not self.description.strip():
            raise SchemaError(f"{self.name}: a column must describe what it holds.")
        for field, value in (
            ("lookback_bars", self.lookback_bars),
            ("forward_bars", self.forward_bars),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SchemaError(
                    f"{self.name}: {field} must be a non-negative int, got {value!r}."
                )
        if self.forward_bars > 0 and self.role not in FORWARD_LOOKING_ROLES:
            raise SchemaError(
                f"{self.name}: a {self.role.value} column declares forward_bars="
                f"{self.forward_bars}, which means it reads bars that had not happened "
                "yet at feature_timestamp. Only a label or its metadata may do that."
            )

    def to_record(self) -> dict[str, object]:
        """The fingerprinted form. Every field participates, by design."""
        return {
            "name": self.name,
            "dtype": self.dtype,
            "role": self.role.value,
            "description": self.description,
            "lookback_bars": self.lookback_bars,
            "forward_bars": self.forward_bars,
        }


# --------------------------------------------------------------------------
# The fixed columns: identity, and provenance
# --------------------------------------------------------------------------

#: Which row this is. Present in every dataset regardless of features or label.
KEY_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec(
        name="symbol",
        dtype="string",
        role=ColumnRole.KEY,
        description="The canonical symbol, slash included for a crypto pair.",
    ),
    ColumnSpec(
        name="feature_timestamp",
        dtype="datetime64[ns, UTC]",
        role=ColumnRole.KEY,
        description=(
            "Start of the newest completed bar every feature on this row reads. "
            "An interval start, not an execution time: the bar covers "
            "[feature_timestamp, feature_timestamp + 15m)."
        ),
    ),
    ColumnSpec(
        name="knowable_at",
        dtype="datetime64[ns, UTC]",
        role=ColumnRole.KEY,
        description=(
            "feature_timestamp + 15m: the first instant this feature row could "
            "have existed, because that is when its bar finished. A live system "
            "additionally waits a provider-lag safety delay past this instant."
        ),
    ),
)

#: How this row came to exist. Never model input.
PROVENANCE_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec(
        name="asset_class",
        dtype="string",
        role=ColumnRole.PROVENANCE,
        description="'crypto' or 'equity': which bar clock this row was built on.",
    ),
    ColumnSpec(
        name="grid_index",
        dtype="int64",
        role=ColumnRole.PROVENANCE,
        description=(
            "Position of this bar in the dataset's bar grid. Consecutive indices "
            "are consecutive tradable bars, so an embargo can be counted in bars "
            "without re-deriving the calendar."
        ),
    ),
    ColumnSpec(
        name="session_id",
        dtype="string",
        role=ColumnRole.PROVENANCE,
        description=(
            "The session this bar belongs to: the exchange session date for "
            "equities, the UTC calendar date for crypto. Split boundaries snap "
            "to it so one session is never divided between train and test."
        ),
    ),
    ColumnSpec(
        name="session_bar_count",
        dtype="int64",
        role=ColumnRole.PROVENANCE,
        description=(
            "How many bars this row's session contains: 26 on a full equity "
            "session, 14 on a 13:00 early close, 96 on a crypto UTC day."
        ),
    ),
    ColumnSpec(
        name="bars_present_in_window",
        dtype="int64",
        role=ColumnRole.PROVENANCE,
        description=(
            "How many of the trailing FEATURE_WINDOW_BARS bars the provider "
            "actually published, this bar included. Below the window length "
            "means the provider left holes; no price was invented to fill them."
        ),
        lookback_bars=FEATURE_WINDOW_BARS,
    ),
)


# --------------------------------------------------------------------------
# The composed schema
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureSchema:
    """The full, ordered column contract of one built dataset."""

    version: str
    columns: tuple[ColumnSpec, ...]

    def __post_init__(self) -> None:
        if not self.columns:
            raise SchemaError("A schema needs at least one column.")
        names = [column.name for column in self.columns]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise SchemaError(f"Duplicate column name(s) in schema: {', '.join(duplicates)}.")
        labels = [column for column in self.columns if column.role is ColumnRole.LABEL]
        if len(labels) != 1:
            raise SchemaError(
                f"A dataset carries exactly one label column, found {len(labels)}. "
                "A second target belongs in a second dataset with its own label spec."
            )

    @property
    def names(self) -> tuple[str, ...]:
        """Every column name, in contract order."""
        return tuple(column.name for column in self.columns)

    def names_for(self, role: ColumnRole) -> tuple[str, ...]:
        """Every column name with `role`, in contract order."""
        return tuple(column.name for column in self.columns if column.role is role)

    @property
    def feature_names(self) -> tuple[str, ...]:
        """The columns a model may read as input. Nothing else is model input."""
        return self.names_for(ColumnRole.FEATURE)

    @property
    def label_name(self) -> str:
        """The single column a model fits against."""
        return self.names_for(ColumnRole.LABEL)[0]

    @property
    def dtypes(self) -> dict[str, str]:
        """The declared pandas dtype of every column."""
        return {column.name: column.dtype for column in self.columns}

    @property
    def max_lookback_bars(self) -> int:
        """The longest trailing window any column reads.

        The number of leading bars a dataset cannot produce a complete row for:
        a 32-bar feature has no value until 32 bars exist.
        """
        return max(column.lookback_bars for column in self.columns)

    @property
    def max_forward_bars(self) -> int:
        """The furthest into the future any column reads. Labels only."""
        return max(column.forward_bars for column in self.columns)

    def column(self, name: str) -> ColumnSpec:
        """The specification of one column, by name."""
        for candidate in self.columns:
            if candidate.name == name:
                return candidate
        raise SchemaError(f"No column named {name!r} in schema {self.version}.")

    def to_record(self) -> dict[str, object]:
        """The serializable form stored in a dataset's metadata sidecar."""
        return {
            "feature_schema_version": self.version,
            "columns": [column.to_record() for column in self.columns],
        }

    @property
    def fingerprint(self) -> str:
        """SHA-256 over the full column specification.

        Changes when any column's name, dtype, role, description, lookback, or
        horizon changes - which is the point. The version says what an operator
        intended; this says what the code actually produced.
        """
        return sha256_of_record(self.to_record())

    def validate_frame(self, frame: pd.DataFrame) -> None:
        """Refuse a frame that is not exactly this contract.

        Exact column set *and* exact order, because a dataset whose columns
        merely overlap the schema is a different dataset. Dtypes are compared
        as strings against the declaration, so an `int64` column that arrived
        as `float64` because a NaN slipped in is a failure rather than a
        surprise three stages later.
        """
        if not isinstance(frame, pd.DataFrame):
            raise SchemaError(f"Expected a DataFrame, got {type(frame).__name__}.")
        actual = tuple(str(name) for name in frame.columns)
        if actual != self.names:
            missing = [name for name in self.names if name not in actual]
            unexpected = [name for name in actual if name not in self.names]
            detail = []
            if missing:
                detail.append(f"missing {', '.join(missing)}")
            if unexpected:
                detail.append(f"unexpected {', '.join(unexpected)}")
            if not detail:
                detail.append("columns are in the wrong order")
            raise SchemaError(
                f"Frame does not match feature schema {self.version}: {'; '.join(detail)}."
            )
        for column in self.columns:
            actual_dtype = str(frame[column.name].dtype)
            if actual_dtype != column.dtype:
                raise SchemaError(
                    f"Column {column.name!r} must have dtype {column.dtype!r}, "
                    f"got {actual_dtype!r}."
                )


def build_schema(
    feature_columns: Sequence[ColumnSpec],
    label_columns: Sequence[ColumnSpec],
    *,
    version: str = FEATURE_SCHEMA_VERSION,
) -> FeatureSchema:
    """Compose the fixed columns, the features, and one label's columns.

    The single place the contract order is decided: keys, provenance, features,
    label metadata, label. Callers supply the two halves that vary and cannot
    reorder the result.
    """
    features = tuple(feature_columns)
    labels = tuple(label_columns)
    for column in features:
        if column.role is not ColumnRole.FEATURE:
            raise SchemaError(
                f"{column.name!r} was supplied as a feature but declares role "
                f"{column.role.value!r}."
            )
    for column in labels:
        if column.role not in FORWARD_LOOKING_ROLES:
            raise SchemaError(
                f"{column.name!r} was supplied as a label column but declares role "
                f"{column.role.value!r}."
            )
    meta = tuple(column for column in labels if column.role is ColumnRole.LABEL_META)
    target = tuple(column for column in labels if column.role is ColumnRole.LABEL)
    return FeatureSchema(
        version=version,
        columns=(*KEY_COLUMNS, *PROVENANCE_COLUMNS, *features, *meta, *target),
    )


__all__ = [
    "FEATURE_SCHEMA_VERSION",
    "FEATURE_WINDOW_BARS",
    "FORWARD_LOOKING_ROLES",
    "KEY_COLUMNS",
    "PROVENANCE_COLUMNS",
    "ColumnRole",
    "ColumnSpec",
    "FeatureSchema",
    "SchemaError",
    "build_schema",
]
