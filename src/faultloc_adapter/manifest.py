"""Frozen instance manifests.

A manifest is the single frozen input for one FLBench instance. It carries
everything needed to generate a Harbor task, so generation is reproducible
without access to FLBench's database, S3, or the network.

The manifests are shipped, not built: `data/eval900.tar.gz` holds all 900 of them
alongside the gold reports, and `manifests/` is gitignored because it is the
extracted copy. Generating tasks therefore needs no FLBench checkout and no
network -- only freezing does, and freezing happens once per instance set.

    sha256sum -c data/eval900.tar.gz.sha256
    tar -xzf data/eval900.tar.gz

`freeze` below is that one-time step, kept because the instance set moved once
already (the balanced 500 -> FLBench's full 900) and may move again. It is pure
metadata: crash fields come out of FLBench's arvo.db and the gold patch out of
its eval-patches/, so nothing is built, pulled or executed. See
faultloc_adapter/freeze.py for the CLI.
"""

import json
import sqlite3
from pathlib import Path

MANIFEST_VERSION = 2

# Fields read from the arvo table, in manifest key order.
_DB_FIELDS = ("project", "language", "sanitizer", "crash_type", "fuzz_target", "crash_output")


def freeze(local_id: int, flbench_root: Path) -> dict:
    """Build a manifest for one instance from a FLBench checkout.

    The image is pinned BY TAG, not by digest. `n132/arvo:<id>-vul` is an
    archival tag that upstream does not move, and resolving digests would put a
    registry round-trip in front of every freeze for a guarantee the tag already
    gives. Manifest v1 carried an `image_digest`; v2 dropped it.
    """
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

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "local_id": local_id,
        "image": f"n132/arvo:{local_id}-vul",
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


def write(manifest: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{manifest['local_id']}.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path


def load(path: Path) -> dict:
    manifest = json.loads(Path(path).read_text())
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError(
            f"{path}: manifest_version {manifest.get('manifest_version')} "
            f"!= supported {MANIFEST_VERSION}"
        )
    # `crash_output` is whatever the target printed, and a fuzzer input echoed back
    # can carry NUL bytes -- 4 of the 500 do. That text is interpolated into
    # instruction.md, which Harbor passes to the agent as an ARGV ELEMENT of
    # `docker compose exec`. execve cannot carry a NUL, so Python raises
    # `ValueError: embedded null byte` before the fork and the trial errors in the
    # agent phase, deterministically, on every attempt. Strip at load so the
    # generator and every checker read the same bytes; the manifest file itself is
    # frozen and stays as it is.
    # Conditional so a manifest missing the field still fails loudly at the point
    # of use, as it did before, rather than rendering an empty report section.
    if isinstance(manifest.get("crash_output"), str):
        manifest["crash_output"] = manifest["crash_output"].replace("\x00", "")
    return manifest
