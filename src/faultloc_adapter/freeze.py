"""Freeze FLBench instances into committed manifests.

    python -m faultloc_adapter.freeze --flbench ~/codes/FLBench --task-ids 42470093
"""

import argparse
from pathlib import Path

from . import manifest as manifest_mod

DEFAULT_MANIFEST_DIR = Path(__file__).resolve().parents[2] / "manifests"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flbench", type=Path, required=True, help="Path to a FLBench checkout")
    parser.add_argument("--task-ids", nargs="+", type=int, required=True)
    parser.add_argument("--manifest-dir", type=Path, default=DEFAULT_MANIFEST_DIR)
    parser.add_argument(
        "--no-digest",
        action="store_true",
        help="Skip registry digest resolution (offline; produces an unpinned manifest)",
    )
    args = parser.parse_args()

    for local_id in args.task_ids:
        m = manifest_mod.freeze(local_id, args.flbench, resolve_digest=not args.no_digest)
        path = manifest_mod.write(m, args.manifest_dir)
        print(f"{local_id}: {m['project']} {m['crash_type']} -> {path}")


if __name__ == "__main__":
    main()
