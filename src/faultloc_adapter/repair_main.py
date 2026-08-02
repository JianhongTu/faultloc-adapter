"""Entry point for the repair task family (`faultloc-repair`).

One invocation generates the ONE dataset both generation agents run:

    uv run faultloc-repair

There is no `--source`: the task carries the developer patch itself rather than
an external report, and the generator identity belongs to the Harbor job rather
than to the task. See repair.py.
"""

import argparse
from pathlib import Path

from .adapter import DEFAULT_AGENT_IMAGE, DEFAULT_ALLOWED_HOSTS
from .repair import RepairAdapter


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Generate only the first N sampled instances (sample order, not task order)",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--task-ids",
        nargs="+",
        default=None,
        help="Instance ids to generate; must be inside the frozen eval50 sample",
    )
    parser.add_argument("--manifest-dir", type=Path, default=None)
    parser.add_argument(
        "--sample",
        type=Path,
        default=None,
        help="Frozen sample manifest (default: data/alternative-patch-eval50.json)",
    )
    parser.add_argument(
        "--testset",
        type=Path,
        default=None,
        help=(
            "Per-instance test scripts (default: data/testset). Every instance "
            "ships one; where none has been written the generator seeds a "
            "placeholder that always fails, so that instance rejects every "
            "candidate until a real script replaces it."
        ),
    )
    parser.add_argument("--agent-image", default=DEFAULT_AGENT_IMAGE)
    parser.add_argument(
        "--allowed-hosts",
        nargs="+",
        default=None,
        help=(
            "Hosts the agent may reach during agent.run() "
            f"(default: {' '.join(DEFAULT_ALLOWED_HOSTS)}). "
            "Must include 'poc' and the model endpoint host."
        ),
    )
    args = parser.parse_args()

    RepairAdapter(
        limit=args.limit,
        overwrite=args.overwrite,
        task_ids=args.task_ids,
        manifest_dir=args.manifest_dir,
        sample_file=args.sample,
        testset_dir=args.testset,
        agent_image=args.agent_image,
        allowed_hosts=args.allowed_hosts,
    ).run()


if __name__ == "__main__":
    main()
