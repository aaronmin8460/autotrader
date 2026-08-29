"""The run manifest: everything needed to say which run produced a number.

A stored metric without the code, data and model identity behind it is a claim
nobody can re-check. This writes the identity block: the repository revision and
library versions from `autotrader.research.reproducibility`, the dataset
fingerprints, the study's own constants, and the SHA-256 of every model artifact
the run fitted.

**Artifact identity is computed from the file, not taken on trust.** The same
rule `autotrader.ml.registry` applies: a model's identity is a property of its
bytes, so a record that names a model version is paired with the hash of the
artifact that version actually wrote.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from autotrader.research.reproducibility import collect
from studies.crypto_v1_v5.analysis import COST_MODELS
from studies.crypto_v1_v5.scoring import SHARED_LOOKBACK_BARS, STUDY_VERSIONS
from studies.crypto_v1_v5.walkforward import EMBARGO_BARS, TRAIN_TEST_GAP_BARS


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact_manifest(run_dir: Path) -> list[dict[str, object]]:
    """Every fitted model, named by its file's own digest."""
    directory = run_dir / "artifacts"
    entries: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.json")):
        record = json.loads(path.read_text())
        entries.append(
            {
                "file": path.name,
                "artifact_sha256": sha256_of_file(path),
                "model_version": record.get("model_version"),
                "family": record.get("family"),
                "feature_version": record.get("feature_version"),
                "label_spec_id": record.get("label_spec_id"),
                "calibration": record.get("calibration", {}).get("method"),
                "seed": record.get("seed"),
                "trained_at": record.get("trained_at"),
            }
        )
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--base-sha", required=True)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    provenance = json.loads((run_dir / "dataset_provenance.json").read_text())
    folds = json.loads((run_dir / "v4_walkforward_folds.json").read_text())

    reproducibility = collect(created_at=datetime.now(UTC), seed=0, root=Path(args.repo_root))
    manifest = {
        "study": "crypto-v1-v5-historical-evaluation",
        "purpose": (
            "Research validation only. No engine is activated, no runtime is changed, "
            "no order is placed, and nothing here authorizes production trading."
        ),
        "evaluation_baseline_sha": args.base_sha,
        "reproducibility": reproducibility.to_json_dict(),
        "engines_compared": list(STUDY_VERSIONS),
        "shared_lookback_bars": SHARED_LOOKBACK_BARS,
        "walk_forward": {
            "scheme": "anchored; every fold trains on all data before its own window",
            "embargo_bars": EMBARGO_BARS,
            "train_test_gap_bars": TRAIN_TEST_GAP_BARS,
            "folds": [
                {
                    k: fold[k]
                    for k in (
                        "symbol",
                        "fold_id",
                        "variant",
                        "test_start",
                        "test_end",
                        "train_end",
                        "is_holdout",
                        "chosen_family",
                        "beat_baseline",
                    )
                }
                for fold in folds
            ],
        },
        "cost_models": {
            name: {
                "label": model.label,
                "fee_rate": str(model.fee_rate),
                "slippage_rate": str(model.slippage_rate),
            }
            for name, model in COST_MODELS.items()
        },
        "cost_model_source": "autotrader.research.costs",
        "datasets": provenance,
        "model_artifacts": artifact_manifest(run_dir),
    }
    target = run_dir / "run_manifest.json"
    target.write_text(json.dumps(manifest, indent=2, default=str))
    print(f"wrote {target}")
    print(f"  code_version : {reproducibility.code_version}")
    print(f"  reproducible : {reproducibility.reproducible}")
    print(f"  artifacts    : {len(manifest['model_artifacts'])}")


if __name__ == "__main__":
    main()
