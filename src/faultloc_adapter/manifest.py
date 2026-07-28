"""Frozen instance manifests.

A manifest is the single frozen input for one FLBench instance. It carries
everything needed to generate a Harbor task, so generation is reproducible
without access to FLBench's database, S3, or the network.

Freeze once from a FLBench checkout, commit the result, and generate from it:

    python -m faultloc_adapter.freeze --flbench ~/codes/FLBench --task-ids 42470093
"""

import json
import subprocess
from pathlib import Path

MANIFEST_VERSION = 1

# Fields read from the arvo table, in manifest key order.
_DB_FIELDS = ("project", "language", "sanitizer", "crash_type", "fuzz_target", "crash_output")


def resolve_image_digest(image: str) -> str:
    """Return the registry digest for an image tag, without pulling its layers."""
    out = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", image, "--format", "{{json .Manifest.Digest}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout.strip())


def freeze(local_id: int, flbench_root: Path, resolve_digest: bool = True) -> dict:
    """Build a manifest for one instance from a FLBench checkout."""
    import sqlite3

    db_path = flbench_root / "data" / "arvo.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {', '.join(_DB_FIELDS)} FROM arvo WHERE localId = ?", (local_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"instance {local_id} not found in {db_path}")

    diff_path = flbench_root / "eval-patches" / f"{local_id}.diff"
    if not diff_path.exists():
        raise FileNotFoundError(f"no ground-truth patch at {diff_path}")

    image = f"n132/arvo:{local_id}-vul"
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "local_id": local_id,
        "image": image,
        "image_digest": resolve_image_digest(image) if resolve_digest else None,
        **dict(zip(_DB_FIELDS, row)),
        # Match flbench.eval.score, which decodes patch bytes with errors="replace";
        # some eval-patches are not valid UTF-8.
        "gt_diff": diff_path.read_bytes().decode("utf-8", errors="replace"),
    }
    missing = [k for k in ("project", "crash_output") if not manifest.get(k)]
    if missing:
        raise ValueError(f"instance {local_id} has empty required field(s): {missing}")
    if not manifest.get("crash_type"):
        raise ValueError(f"instance {local_id} has no crash_type")
    # FLBench skips instances whose patch yields no hunks (eval/score.py). Freezing
    # one would produce tasks whose verifier raises instead of scoring, so reject
    # it here rather than at run time.
    from .scorer import parse_diff

    if not parse_diff(manifest["gt_diff"]):
        raise ValueError(
            f"instance {local_id}: ground-truth patch yields no hunks; FLBench skips these"
        )
    return manifest


def load(path: Path) -> dict:
    manifest = json.loads(Path(path).read_text())
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError(
            f"{path}: manifest_version {manifest.get('manifest_version')} "
            f"!= supported {MANIFEST_VERSION}"
        )
    return manifest


def write(manifest: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{manifest['local_id']}.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path


def pinned_image(manifest: dict) -> str:
    """Image reference pinned by digest when one was resolved at freeze time."""
    digest = manifest.get("image_digest")
    return f"{manifest['image'].split(':')[0]}@{digest}" if digest else manifest["image"]
