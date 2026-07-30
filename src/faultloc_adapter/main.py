"""
Main entry point for the localization task family (`faultloc-adapter`).

Constructs the Adapter class defined in adapter.py and calls run() to generate one
dataset per requested config under datasets/ (see DATASET_NAMES in adapter.py).

The template's `--output-dir` is deliberately absent: task names carry the instance
only, so pointing two configs at one directory would collide on the same name and
keep whichever was written last. The destination is derived from the config instead.
"""

import argparse
from pathlib import Path

from .adapter import (
    CONFIGS,
    DEFAULT_AGENT_IMAGE,
    DEFAULT_ALLOWED_HOSTS,
    FLBenchAdapter,
)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Generate only the first N instances (each config gets the same N)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing tasks",
    )
    parser.add_argument(
        "--task-ids",
        nargs="+",
        default=None,
        help="Only generate these task IDs",
    )
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=None,
        help="Directory of frozen instance manifests (default: <repo>/manifests)",
    )
    parser.add_argument(
        "--configs",
        nargs="+",
        default=None,
        choices=sorted(CONFIGS),
        help="Benchmark configs to generate (default: all)",
    )
    parser.add_argument(
        "--agent-image",
        default=DEFAULT_AGENT_IMAGE,
        help="Image the agent runs in (contains no source, PoC or reproducer)",
    )
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

    adapter = FLBenchAdapter(
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        manifest_dir=args.manifest_dir,
        configs=args.configs,
        agent_image=args.agent_image,
        allowed_hosts=args.allowed_hosts,
    )

    adapter.run()


if __name__ == "__main__":
    main()
