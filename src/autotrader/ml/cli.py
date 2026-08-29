"""M1: the `autotrader ml ...` sub-application.

Read-or-build commands and nothing else. Every one of them reads files that
already exist and writes to external storage; none of them fetches market data,
constructs a broker client, or activates anything. The `autotrader` application
it attaches to owns the commands that can reach a broker, and this
sub-application deliberately shares none of them.

`v4-compare` and `v4-train` do fit models, which the first five commands do not.
That is the only capability added, and it is bounded on both sides: the input is
a bar file already on disk, and the output is an artifact registered at the
`experimental` stage. There is no stage that makes a model trade and no command
here that could turn one on - activation is a deliberate change to a runtime,
made somewhere else, by someone who has read the evidence `v4-compare` prints.

Kept in its own module rather than added to `autotrader.cli` so that the ML
foundation owns its own surface: the commands here change when the ML contracts
change, and the trading CLI is untouched by that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from autotrader.ml import MLError, asset_class_for_symbol
from autotrader.ml.dataset import (
    DatasetSpec,
    build_dataset_from_parquet,
    dataset_schema,
    read_dataset,
)
from autotrader.ml.grid import load_sessions
from autotrader.ml.labels import LabelKind, LabelSpec, SessionPolicy, ThresholdMode
from autotrader.ml.registry import ModelRegistry, summarize
from autotrader.ml.schema import ColumnRole
from autotrader.ml.splits import SplitSpec, assert_no_leakage, temporal_split

#: `ml` exit codes, matching the trading CLI's shape. 0 is success.
#:
#: 1  a controlled refusal reported as a message: an invalid specification, a
#:    dataset that failed validation, a missing registry entry.
#: 2  the input could not be read at all - a missing file, an unset storage
#:    root, an unmounted external volume.
ML_REFUSED_EXIT_CODE = 1
ML_UNREADABLE_INPUT_EXIT_CODE = 2

#: Width of the label column in this application's reports.
_LABEL_WIDTH = 28

app = typer.Typer(
    name="ml",
    add_completion=False,
    no_args_is_help=True,
)


@app.callback()
def ml() -> None:
    """Offline ML foundation: datasets, labels, splits, the registry, and V4 training.

    Builds versioned feature datasets from bar files that already exist on
    disk, cuts them into leak-free temporal splits, inspects the model
    registry, and - with v4-compare and v4-train - produces the walk-forward
    evidence behind V4's model choice and fits the model it selects.

    Nothing here downloads market data, reaches a broker, or activates
    anything. A trained artifact is registered at the experimental stage;
    making a model trade is a deliberate change to a runtime, elsewhere.

    Heavy output goes to the external workspace named by AUTOTRADER_QA_DATASETS,
    AUTOTRADER_QA_MODELS and AUTOTRADER_QA_REPORTS. Those must be set - there
    is no fallback that would fill the internal disk.
    """


def _field(label: str, value: object) -> str:
    """One aligned `label: value` report line."""
    return f"{label + ':':<{_LABEL_WIDTH}}{value}"


def _fail(error: Exception, code: int) -> typer.Exit:
    typer.secho(str(error), fg=typer.colors.RED, err=True)
    return typer.Exit(code=code)


def _label_spec(
    name: str,
    kind: str,
    horizon_bars: int,
    entry_offset_bars: int,
    entry_price: str,
    exit_price: str,
    threshold_mode: str,
    upper_threshold: float,
    lower_threshold: float,
    session_policy: str,
) -> LabelSpec:
    """Build a `LabelSpec` from command-line strings, refusing an unknown enum value."""
    try:
        return LabelSpec(
            name=name,
            kind=LabelKind(kind),
            horizon_bars=horizon_bars,
            entry_offset_bars=entry_offset_bars,
            entry_price_column=entry_price,
            exit_price_column=exit_price,
            threshold_mode=ThresholdMode(threshold_mode),
            upper_threshold=upper_threshold,
            lower_threshold=lower_threshold,
            session_policy=SessionPolicy(session_policy),
        )
    except ValueError as error:
        raise MLError(f"Unknown option value: {error}") from None


LabelName = Annotated[
    str,
    typer.Option(
        "--label-name",
        help=(
            "Name recorded for this target. Kind-neutral by default: the name is an "
            "operator's word and the fingerprint is what actually identifies a label."
        ),
    ),
]
LabelKindOption = Annotated[
    str,
    typer.Option(
        "--label-kind",
        help=f"One of: {', '.join(kind.value for kind in LabelKind)}.",
    ),
]
HorizonBars = Annotated[
    int,
    typer.Option("--horizon-bars", help="How many bars the position is held for."),
]
EntryOffsetBars = Annotated[
    int,
    typer.Option(
        "--entry-offset-bars",
        help=(
            "Bars between the feature bar and the entry bar. At least 1: a decision "
            "taken once bar t closed cannot be filled inside bar t."
        ),
    ),
]
EntryPrice = Annotated[
    str, typer.Option("--entry-price", help="Entry price column: open or close.")
]
ExitPrice = Annotated[str, typer.Option("--exit-price", help="Exit price column: open or close.")]
ThresholdModeOption = Annotated[
    str,
    typer.Option(
        "--threshold-mode",
        help="absolute (a return fraction) or volatility (a multiple of trailing sigma).",
    ),
]
UpperThreshold = Annotated[
    float, typer.Option("--upper-threshold", help="Boundary above which the class is UP/BUY.")
]
LowerThreshold = Annotated[
    float, typer.Option("--lower-threshold", help="Boundary below which the class is SELL.")
]
SessionPolicyOption = Annotated[
    str,
    typer.Option(
        "--session-policy",
        help=(
            "span_sessions allows a holding period to cross a market closure and "
            "flags it; within_session refuses to label one (equity grids only)."
        ),
    ),
]


@app.command()
def schema(
    label_name: LabelName = "target",
    label_kind: LabelKindOption = LabelKind.FORWARD_RETURN.value,
    horizon_bars: HorizonBars = 4,
    entry_offset_bars: EntryOffsetBars = 1,
    entry_price: EntryPrice = "open",
    exit_price: ExitPrice = "open",
    threshold_mode: ThresholdModeOption = ThresholdMode.ABSOLUTE.value,
    upper_threshold: UpperThreshold = 0.0,
    lower_threshold: LowerThreshold = 0.0,
    session_policy: SessionPolicyOption = SessionPolicy.SPAN_SESSIONS.value,
) -> None:
    """Print the column contract a dataset with this label would carry.

    Reads nothing and writes nothing. The fingerprint shown is what a built
    dataset records, so this is how to check whether a schema change has landed
    without rebuilding anything.
    """
    try:
        label = _label_spec(
            label_name,
            label_kind,
            horizon_bars,
            entry_offset_bars,
            entry_price,
            exit_price,
            threshold_mode,
            upper_threshold,
            lower_threshold,
            session_policy,
        )
        contract = dataset_schema(label)
    except MLError as error:
        raise _fail(error, ML_REFUSED_EXIT_CODE) from None

    typer.echo(_field("Feature schema version", contract.version))
    typer.echo(_field("Schema fingerprint", contract.fingerprint))
    typer.echo(_field("Label", label.identifier))
    typer.echo(_field("Columns", len(contract.columns)))
    typer.echo(_field("Model input features", len(contract.feature_names)))
    typer.echo(_field("Longest feature window", f"{contract.max_lookback_bars} bars"))
    typer.echo(_field("Furthest label horizon", f"{contract.max_forward_bars} bars"))
    typer.echo("")
    typer.echo(f"Label interval: {label.describe()}")
    typer.echo("")
    for column in contract.columns:
        marker = "*" if column.role is ColumnRole.FEATURE else " "
        typer.echo(
            f"{marker} {column.name:<32} {column.role.value:<12} {column.dtype:<22} "
            f"back {column.lookback_bars:<4} forward {column.forward_bars}"
        )
    typer.echo("")
    typer.echo("* = model input. Every other column is identity, provenance, or the target.")


@app.command(name="build-dataset")
def build_dataset_command(
    bars: Annotated[
        Path,
        typer.Argument(help="Parquet bar file written by `download` or `equity-download`."),
    ],
    symbol: Annotated[str, typer.Option("--symbol", help="The symbol the bar file holds.")],
    sessions: Annotated[
        Path | None,
        typer.Option(
            "--sessions",
            help=(
                "JSON session calendar. Required for an equity symbol; refused for "
                "a crypto pair, which trades continuously."
            ),
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            help="Where to write. Defaults to AUTOTRADER_QA_DATASETS.",
        ),
    ] = None,
    label_name: LabelName = "target",
    label_kind: LabelKindOption = LabelKind.FORWARD_RETURN.value,
    horizon_bars: HorizonBars = 4,
    entry_offset_bars: EntryOffsetBars = 1,
    entry_price: EntryPrice = "open",
    exit_price: ExitPrice = "open",
    threshold_mode: ThresholdModeOption = ThresholdMode.ABSOLUTE.value,
    upper_threshold: UpperThreshold = 0.0,
    lower_threshold: LowerThreshold = 0.0,
    session_policy: SessionPolicyOption = SessionPolicy.SPAN_SESSIONS.value,
) -> None:
    """Build a versioned feature dataset from a stored bar file.

    The bar file is validated against the project's structural contract before
    anything is computed, and is never modified or repaired. Output is one
    Parquet file plus a metadata sidecar recording the schema, the label's
    exact future interval, the grid, the source file's hash, and the row
    counts - everything needed to rebuild the same dataset later.
    """
    try:
        label = _label_spec(
            label_name,
            label_kind,
            horizon_bars,
            entry_offset_bars,
            entry_price,
            exit_price,
            threshold_mode,
            upper_threshold,
            lower_threshold,
            session_policy,
        )
        spec = DatasetSpec(symbol=symbol, label=label)
        calendar = None
        if sessions is not None:
            calendar = load_sessions(sessions)
        artifact = build_dataset_from_parquet(
            bars, spec=spec, sessions=calendar, output_dir=output_dir
        )
    except FileNotFoundError as error:
        raise _fail(error, ML_UNREADABLE_INPUT_EXIT_CODE) from None
    except MLError as error:
        raise _fail(error, ML_REFUSED_EXIT_CODE) from None
    except Exception as error:  # noqa: BLE001 - an unreadable input is one failure
        raise _fail(error, ML_UNREADABLE_INPUT_EXIT_CODE) from None

    typer.secho("Dataset built", fg=typer.colors.GREEN)
    typer.echo("")
    typer.echo(_field("Symbol", spec.symbol))
    typer.echo(_field("Asset class", asset_class_for_symbol(spec.symbol).value))
    typer.echo(_field("Label", label.identifier))
    typer.echo(_field("Feature schema", artifact.schema_version))
    typer.echo(_field("Schema fingerprint", artifact.schema_fingerprint[:16]))
    typer.echo(_field("Dataset fingerprint", artifact.fingerprint[:16]))
    typer.echo(_field("Rows", artifact.row_count))
    typer.echo(_field("Labelled rows", artifact.labelled_row_count))
    typer.echo(_field("Parquet", artifact.parquet_path))
    typer.echo(_field("Metadata", artifact.metadata_path))


@app.command()
def split(
    dataset: Annotated[Path, typer.Argument(help="A dataset Parquet file built by build-dataset.")],
    train_fraction: Annotated[
        float, typer.Option("--train-fraction", help="Share of labelled rows used for training.")
    ] = 0.6,
    validation_fraction: Annotated[
        float, typer.Option("--validation-fraction", help="Share used for validation.")
    ] = 0.2,
    embargo_bars: Annotated[
        int,
        typer.Option(
            "--embargo-bars",
            help="Extra bars dropped before each boundary, on top of label purging.",
        ),
    ] = 0,
    snap_to_session: Annotated[
        bool,
        typer.Option(
            "--snap-to-session/--no-snap-to-session",
            help="Move each boundary to a session edge so no session is split.",
        ),
    ] = True,
) -> None:
    """Report the temporal split of a built dataset, and prove it does not leak.

    Computes the split and runs the leakage assertion over it: no part may
    overlap the next in time, and no label in an earlier part may resolve
    inside a later one. Writes nothing.
    """
    try:
        frame = read_dataset(dataset)
        spec = SplitSpec(
            train_fraction=train_fraction,
            validation_fraction=validation_fraction,
            embargo_bars=embargo_bars,
            snap_to_session=snap_to_session,
        )
        result = temporal_split(frame, spec)
        assert_no_leakage(result)
    except MLError as error:
        raise _fail(error, ML_REFUSED_EXIT_CODE) from None

    typer.secho("Split is temporally clean", fg=typer.colors.GREEN)
    typer.echo("")
    typer.echo(_field("Dataset", dataset))
    typer.echo(_field("Unlabelled rows excluded", result.unlabelled_rows))
    typer.echo("")
    for part in result.parts:
        first = "-" if part.first_timestamp is None else part.first_timestamp.isoformat()
        last = "-" if part.last_timestamp is None else part.last_timestamp.isoformat()
        typer.echo(f"{part.name:<12} rows {part.row_count:<8} {first}  ->  {last}")
        typer.echo(f"{'':<12} purged {part.purged_rows}, embargoed {part.embargoed_rows}")


@app.command(name="registry-list")
def registry_list(
    root: Annotated[
        Path | None,
        typer.Option("--root", help="Registry root. Defaults to AUTOTRADER_QA_MODELS/registry."),
    ] = None,
) -> None:
    """List every registered model artifact.

    Each row reports whether the stored file still hashes to the identity it
    was registered under, so a corrupted or replaced artifact is visible here
    rather than at the moment something tries to load it.
    """
    try:
        registry = ModelRegistry(root)
        artifacts = [
            artifact for name in registry.list_models() for artifact in registry.list_versions(name)
        ]
    except MLError as error:
        raise _fail(error, ML_UNREADABLE_INPUT_EXIT_CODE) from None

    if not artifacts:
        typer.echo(f"No artifacts registered under {registry.root}.")
        return
    typer.echo(_field("Registry", registry.root))
    typer.echo("")
    for row in summarize(artifacts):
        state = "ok" if row["verified"] else "HASH MISMATCH"
        typer.echo(
            f"{row['model_name']:<28} {row['model_version']:<12} {row['stage']:<14} "
            f"{row['artifact_version']}  {row['created_at_utc']}  {state}"
        )


@app.command(name="registry-show")
def registry_show(
    model_name: Annotated[str, typer.Argument(help="Registered model name.")],
    model_version: Annotated[str, typer.Argument(help="Registered model version.")],
    root: Annotated[
        Path | None,
        typer.Option("--root", help="Registry root. Defaults to AUTOTRADER_QA_MODELS/registry."),
    ] = None,
) -> None:
    """Show one artifact's full provenance record."""
    try:
        registry = ModelRegistry(root)
        artifact = registry.get(model_name, model_version)
        history = registry.stage_history(model_name, model_version)
    except MLError as error:
        raise _fail(error, ML_REFUSED_EXIT_CODE) from None

    metadata = artifact.metadata
    typer.echo(_field("Model", f"{metadata.model_name} {metadata.model_version}"))
    typer.echo(_field("Artifact version", metadata.artifact_version))
    typer.echo(_field("Stage", artifact.stage.value))
    typer.echo(_field("File verified", artifact.verify()))
    typer.echo(_field("Created", metadata.created_at_utc.isoformat()))
    typer.echo(_field("Asset class", metadata.asset_class))
    typer.echo(_field("Symbols", ", ".join(metadata.symbols)))
    typer.echo(_field("Feature schema", metadata.feature_schema_version))
    typer.echo(_field("Schema fingerprint", metadata.feature_schema_fingerprint[:16]))
    typer.echo(_field("Label", metadata.label_spec_id))
    typer.echo(_field("Dataset fingerprint", metadata.dataset_fingerprint[:16]))
    typer.echo(_field("Experiment", metadata.experiment_id[:16]))
    typer.echo(_field("Path", artifact.artifact_path))
    if metadata.metrics:
        typer.echo("")
        for name, value in sorted(metadata.metrics.items()):
            typer.echo(f"  {name:<28} {value:.6g}")
    if history:
        typer.echo("")
        typer.echo("Stage history:")
        for entry in history:
            typer.echo(f"  {entry.get('at_utc')}  {entry.get('stage')}  {entry.get('reason')}")


# --------------------------------------------------------------------------
# V4: comparing candidates, and training the one that wins
# --------------------------------------------------------------------------


BarsArgument = Annotated[
    Path, typer.Argument(help="Parquet bar file written by `download` or `equity-download`.")
]
SymbolOption = Annotated[str, typer.Option("--symbol", help="The symbol the bar file holds.")]
SessionsOption = Annotated[
    Path | None,
    typer.Option(
        "--sessions",
        help=(
            "JSON session calendar. Required for an equity symbol; refused for a "
            "crypto pair, which trades continuously."
        ),
    ),
]
FoldsOption = Annotated[
    int, typer.Option("--folds", help="Anchored walk-forward folds used to compare candidates.")
]
SeedOption = Annotated[
    int, typer.Option("--seed", help="Recorded with every run, whether or not a family uses it.")
]
EmbargoOption = Annotated[
    int,
    typer.Option(
        "--embargo-bars",
        help="Extra bars dropped before each fold boundary, on top of label purging.",
    ),
]
GitShaOption = Annotated[
    str | None,
    typer.Option("--git-sha", help="Commit the code was at. Supplied by the caller; not read."),
]
GitBranchOption = Annotated[
    str | None, typer.Option("--git-branch", help="Branch the code was on, for the record.")
]


def _v4_training_frame(
    bars_path: Path,
    symbol: str,
    sessions: Path | None,
    horizon_bars: int,
):
    """Read stored bars and build the V4 training frame they support."""
    from autotrader.data.validation import read_bars
    from autotrader.ml.dataset import DatasetSpec, grid_for_bars
    from autotrader.ml.v4 import build_training_frame, default_label_spec

    label = default_label_spec(horizon_bars=horizon_bars)
    frame = read_bars(bars_path)
    calendar = load_sessions(sessions) if sessions is not None else None
    grid = grid_for_bars(frame, DatasetSpec(symbol=symbol, label=label), sessions=calendar)
    return build_training_frame(frame, grid=grid, label=label), grid


@app.command(name="v4-compare")
def v4_compare_command(
    bars: BarsArgument,
    symbol: SymbolOption,
    sessions: SessionsOption = None,
    horizon_bars: HorizonBars = 4,
    folds: FoldsOption = 4,
    embargo_bars: EmbargoOption = 0,
    seed: SeedOption = 0,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", help="Where to write. Defaults to AUTOTRADER_QA_REPORTS."),
    ] = None,
) -> None:
    """Grade every V4 candidate on anchored walk-forward folds, and record the result.

    This is the command that produces the evidence behind V4's model choice. It
    fits a null baseline, a regularised logistic regression and a small boosted
    ensemble on identical folds, grades each on log loss, Brier score, expected
    calibration error and AUC, and applies the selection rule: beat the base
    rate materially, then prefer the simplest family that is not materially
    worse than the best.

    Nothing is registered and nothing is activated. The output is a report.
    """
    from autotrader.ml.v4 import compare_candidates, write_comparison

    try:
        training, _ = _v4_training_frame(bars, symbol, sessions, horizon_bars)
        comparison = compare_candidates(training, folds=folds, embargo_bars=embargo_bars, seed=seed)
        path = write_comparison(comparison, root=output_dir)
    except FileNotFoundError as error:
        raise _fail(error, ML_UNREADABLE_INPUT_EXIT_CODE) from None
    except MLError as error:
        raise _fail(error, ML_REFUSED_EXIT_CODE) from None
    except Exception as error:  # noqa: BLE001 - an unreadable input is one failure
        raise _fail(error, ML_UNREADABLE_INPUT_EXIT_CODE) from None

    typer.secho("Walk-forward comparison complete", fg=typer.colors.GREEN)
    typer.echo("")
    typer.echo(_field("Symbol", training.symbol))
    typer.echo(_field("Rows", training.row_count))
    typer.echo(_field("Labelled rows", training.labelled_row_count))
    typer.echo(_field("Label", training.label.identifier))
    typer.echo(_field("Feature schema", training.schema.version))
    typer.echo(_field("Folds", comparison.fold_count))
    typer.echo("")
    typer.echo(f"  {'candidate':<22}{'log loss':>11}{'brier':>10}{'ECE':>9}{'AUC':>9}")
    for result in comparison.results:
        metrics = result.mean_metrics
        typer.echo(
            f"  {result.candidate.name:<22}"
            f"{metrics['log_loss']:>11.6f}"
            f"{metrics['brier_score']:>10.4f}"
            f"{metrics['expected_calibration_error']:>9.4f}"
            f"{metrics['roc_auc']:>9.4f}"
        )
    typer.echo("")
    typer.echo(_field("Selected", comparison.chosen.name))
    typer.echo(_field("Report", path))
    typer.echo("")
    typer.echo(comparison.rationale)


@app.command(name="v4-train")
def v4_train_command(
    bars: BarsArgument,
    symbol: SymbolOption,
    model_version: Annotated[
        str, typer.Option("--model-version", help="The version this artifact is registered under.")
    ],
    sessions: SessionsOption = None,
    horizon_bars: HorizonBars = 4,
    folds: FoldsOption = 4,
    embargo_bars: EmbargoOption = 0,
    seed: SeedOption = 0,
    train_fraction: Annotated[
        float, typer.Option("--train-fraction", help="Share of labelled rows used for training.")
    ] = 0.6,
    validation_fraction: Annotated[
        float,
        typer.Option(
            "--validation-fraction",
            help="Share used for validation, which is where the calibration is fitted.",
        ),
    ] = 0.2,
    calibrate: Annotated[
        bool,
        typer.Option(
            "--calibrate/--no-calibrate",
            help="Fit an isotonic calibration on the validation split.",
        ),
    ] = True,
    models_dir: Annotated[
        Path | None,
        typer.Option("--models-dir", help="Where to write. Defaults to AUTOTRADER_QA_MODELS."),
    ] = None,
    git_sha: GitShaOption = None,
    git_branch: GitBranchOption = None,
    notes: Annotated[str, typer.Option("--notes", help="Free text stored with the artifact.")] = "",
) -> None:
    """Select a V4 model from walk-forward evidence, fit it, calibrate it, register it.

    The candidate is chosen by the same comparison `v4-compare` prints, so a
    training run cannot pick a model the evidence did not support. It is then
    fitted on the training split, calibrated on the validation split, and
    evaluated once on the test split, and the artifact records all three.

    The artifact is registered at the `experimental` stage. Registration is not
    activation: nothing about this command makes a model trade.
    """
    from autotrader.ml.dataset import frame_fingerprint
    from autotrader.ml.experiment import GitProvenance, write_experiment
    from autotrader.ml.registry import ArtifactStage
    from autotrader.ml.storage import model_root
    from autotrader.ml.v4 import (
        compare_candidates,
        experiment_for,
        register_model,
        train_model,
        write_comparison,
    )

    try:
        training, _ = _v4_training_frame(bars, symbol, sessions, horizon_bars)
        comparison = compare_candidates(training, folds=folds, embargo_bars=embargo_bars, seed=seed)
        comparison_path = write_comparison(comparison)
        split_spec = SplitSpec(
            train_fraction=train_fraction,
            validation_fraction=validation_fraction,
            embargo_bars=embargo_bars,
        )
        trained = train_model(
            training,
            comparison.chosen,
            model_version=model_version,
            split=split_spec,
            seed=seed,
            calibrate=calibrate,
            code_revision={"branch": git_branch, "sha": git_sha},
            notes=notes,
        )
        fingerprint = frame_fingerprint(training.frame)
        experiment = experiment_for(
            training,
            trained,
            name=f"v4-{training.symbol.replace('/', '_')}",
            dataset_fingerprint=fingerprint,
            split=split_spec,
            git=GitProvenance(branch=git_branch, sha=git_sha),
            notes=notes,
        )
        experiment_path = write_experiment(experiment)
        base = Path(models_dir) if models_dir is not None else model_root()
        registered = register_model(
            trained,
            training,
            experiment=experiment,
            dataset_fingerprint=fingerprint,
            registry=ModelRegistry(root=base / "registry"),
            directory=base / "v4",
            stage=ArtifactStage.EXPERIMENTAL,
            notes=notes,
        )
    except FileNotFoundError as error:
        raise _fail(error, ML_UNREADABLE_INPUT_EXIT_CODE) from None
    except MLError as error:
        raise _fail(error, ML_REFUSED_EXIT_CODE) from None
    except Exception as error:  # noqa: BLE001 - an unreadable input is one failure
        raise _fail(error, ML_UNREADABLE_INPUT_EXIT_CODE) from None

    typer.secho("Model trained and registered (experimental)", fg=typer.colors.GREEN)
    typer.echo("")
    typer.echo(_field("Model", f"{trained.candidate.name} {model_version}"))
    typer.echo(_field("Family", trained.artifact.family))
    typer.echo(_field("Artifact version", registered.metadata.artifact_version[:16]))
    typer.echo(_field("Stage", registered.stage.value))
    typer.echo(_field("Feature schema", trained.artifact.feature_version))
    typer.echo(_field("Label", trained.artifact.label_spec_id))
    typer.echo(_field("Calibration", trained.artifact.calibration_method))
    typer.echo(_field("Experiment", experiment.short_id))
    typer.echo(_field("Train rows", trained.split.train.row_count))
    typer.echo(_field("Validation rows", trained.split.validation.row_count))
    typer.echo(_field("Test rows", trained.split.test.row_count))
    typer.echo("")
    for name, value in sorted(trained.test_metrics.items()):
        typer.echo(f"  test {name:<28} {value:.6g}")
    typer.echo("")
    typer.echo(_field("Artifact", registered.artifact_path))
    typer.echo(_field("Experiment record", experiment_path))
    typer.echo(_field("Comparison", comparison_path))
    typer.echo("")
    typer.echo(comparison.rationale)


__all__ = [
    "ML_REFUSED_EXIT_CODE",
    "ML_UNREADABLE_INPUT_EXIT_CODE",
    "app",
]
