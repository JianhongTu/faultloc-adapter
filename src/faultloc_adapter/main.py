"""
Main entry point for the template adapter. Do not modify any of the existing flags.
You can add any additional flags you need.

Constructs the Adapter class defined in adapter.py and calls run() to generate tasks
in the Harbor format at the configured output directory.
"""

import argparse
from pathlib import Path

from .adapter import CONFIGS, DEFAULT_AGENT_IMAGE, FLBenchAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "datasets" / "faultloc-adapter"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write generated tasks",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Generate only the first N tasks",
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
    args = parser.parse_args()

    adapter = FLBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        manifest_dir=args.manifest_dir,
        configs=args.configs,
        agent_image=args.agent_image,
    )

    adapter.run()


if __name__ == "__main__":
    main()
