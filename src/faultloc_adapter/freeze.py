"""Freeze FLBench instances into manifests -- the one-time step that locks a set.

Everything downstream reads manifests, never FLBench, so this is the only tool in
the repo that needs a FLBench checkout. It needs no network and no containers:
the crash fields come out of `data/arvo.db` and the gold patch out of
`eval-patches/<id>.diff`.

    python -m faultloc_adapter.freeze --flbench ~/codes/FLBench \\
        --instance-list data/eval900_instance_list.json

Re-freezing an instance that already has a manifest is skipped by default, so the
command is safe to re-run and safe to point at a superset of what is already
frozen; --force overwrites. Instances FLBench itself would skip (a gold patch
that yields no hunks) are rejected here rather than at run time.
"""

import argparse
import json
from pathlib import Path

from . import manifest as manifest_mod

DEFAULT_MANIFEST_DIR = Path(__file__).resolve().parents[2] / "manifests"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flbench", type=Path, required=True, help="Path to a FLBench checkout")
    parser.add_argument("--task-ids", nargs="+", type=int)
    parser.add_argument(
        "--instance-list",
        type=Path,
        help="JSON array of local_ids to freeze (alternative to --task-ids)",
    )
    parser.add_argument("--manifest-dir", type=Path, default=DEFAULT_MANIFEST_DIR)
    parser.add_argument(
        "--force", action="store_true", help="Re-freeze instances that already have a manifest"
    )
    args = parser.parse_args()

    ids = list(args.task_ids or [])
    if args.instance_list:
        ids += json.loads(args.instance_list.read_text())
    if not ids:
        parser.error("give --task-ids or --instance-list")

    written = skipped = 0
    for local_id in dict.fromkeys(ids):
        out = args.manifest_dir / f"{local_id}.json"
        if out.exists() and not args.force:
            skipped += 1
            continue
        m = manifest_mod.freeze(local_id, args.flbench)
        manifest_mod.write(m, args.manifest_dir)
        written += 1

    print(f"froze {written}, skipped {skipped} already present -> {args.manifest_dir}")


if __name__ == "__main__":
    main()
