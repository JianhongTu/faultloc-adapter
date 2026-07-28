"""Entry point for the repair task family (`faultloc-repair`).

Separate from main.py because the two families take different options: repair has
conditions rather than ablation configs, and takes an optional directory of
authored root-cause reports.
"""

import argparse
from pathlib import Path

from .adapter import DEFAULT_AGENT_IMAGE, DEFAULT_ALLOWED_HOSTS
from .repair import REPAIR_CONFIGS, RepairAdapter

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "datasets" / "faultloc-adapter"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--limit", type=int, default=None, help="Generate only the first N tasks")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--task-ids", nargs="+", default=None)
    parser.add_argument("--manifest-dir", type=Path, default=None)
    parser.add_argument(
        "--configs",
        nargs="+",
        default=None,
        choices=sorted(REPAIR_CONFIGS),
        help="Conditions to generate (default: all)",
    )
    parser.add_argument(
        "--reports",
        type=Path,
        default=None,
        help=(
            "Directory of authored root-cause reports named <local_id>.json. "
            "Instances without one fall back to a report derived from the "
            "developer patch, which carries locations but no explanation."
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
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        manifest_dir=args.manifest_dir,
        configs=args.configs,
        reports_dir=args.reports,
        agent_image=args.agent_image,
        allowed_hosts=args.allowed_hosts,
    ).run()


if __name__ == "__main__":
    main()
