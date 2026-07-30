"""Entry point for the repair task family (`faultloc-repair`).

Separate from main.py because the two families take different options: repair is
parameterized by the report source rather than by an ablation config, and takes a
directory of authored root-cause reports.

One invocation generates ONE dataset, for one report source:

    uv run faultloc-repair --source gold --reports reports/gold
    uv run faultloc-repair --source diagnosis-qwen3.6-27b \\
        --reports reports/diagnosis-qwen3.6-27b

The output directory is derived from the source, so a run cannot file one
source's tasks under another's name. See repair.py for the source vocabulary.
"""

import argparse
from pathlib import Path

from .adapter import DEFAULT_AGENT_IMAGE, DEFAULT_ALLOWED_HOSTS
from .repair import GOLD_SOURCE, RepairAdapter


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        required=True,
        help=(
            f"Report source this dataset is built from: '{GOLD_SOURCE}', or "
            "'diagnosis-<model-id>' naming the localization model whose "
            "predictions the reports are (e.g. diagnosis-qwen3.6-27b). Use the "
            "served model id, lowercased with the org prefix dropped -- not a "
            "gateway alias, which can be repointed at another checkpoint."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Generate only the first N instances (every source gets the same N)",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--task-ids", nargs="+", default=None)
    parser.add_argument("--manifest-dir", type=Path, default=None)
    parser.add_argument(
        "--reports",
        type=Path,
        default=None,
        help=(
            "Directory of this source's root-cause reports, one per instance as "
            "<dir>/<local_id>.json. Required: no report is derivable from the "
            "manifest, and a source that cannot find its reports is an error, "
            "not a fallback."
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
        args.source,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        manifest_dir=args.manifest_dir,
        reports_dir=args.reports,
        agent_image=args.agent_image,
        allowed_hosts=args.allowed_hosts,
    ).run()


if __name__ == "__main__":
    main()
