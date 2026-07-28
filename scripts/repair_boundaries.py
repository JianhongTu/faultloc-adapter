"""Assert the repair conditions differ only by the report.

The uplift a repair experiment measures is only attributable to the report if the
report is the sole difference between the arms. Two ways that silently breaks:
the prompts drift apart for unrelated reasons, or `self` ends up carrying
ground-truth content it should never see. Both are checked here, statically, on
the generated tasks -- no containers, so it is cheap enough to run on every
regeneration.

    python scripts/repair_boundaries.py --tasks datasets/faultloc-adapter
"""

import argparse
import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faultloc_adapter.scorer import parse_diff  # noqa: E402

# Lines whose presence in a task.toml difference is expected: the two conditions
# are different Harbor tasks and say so.
_IDENTITY_KEYS = ("name =", "description =", "condition =")


def _prompt_delta(self_text: str, gold_text: str) -> list[str]:
    """Lines added by `gold`, excluding the rendered report section."""
    delta = [
        line[1:]
        for line in difflib.unified_diff(
            self_text.splitlines(), gold_text.splitlines(), n=0, lineterm=""
        )
        if line.startswith("+") and not line.startswith("+++")
    ]
    # The report section is the intended difference; anything else is drift.
    try:
        start = delta.index("## Reported Root Cause")
    except ValueError:
        return delta or ["gold renders no report section"]
    return delta[:start]


def check(tasks: Path, local_id: str, gt_diff: str) -> dict:
    self_dir = tasks / f"repair__{local_id}-self"
    gold_dir = tasks / f"repair__{local_id}-gold"
    result = {"local_id": local_id}
    if not (self_dir.exists() and gold_dir.exists()):
        result["status"] = "MISSING"
        return result

    self_prompt = (self_dir / "instruction.md").read_text()
    gold_prompt = (gold_dir / "instruction.md").read_text()

    # Every ground-truth line number, as it would be written in the report.
    gt_lines = {line for hunk in parse_diff(gt_diff) for line in hunk.lines}
    gt_files = {hunk.file.rsplit("/", 1)[-1] for hunk in parse_diff(gt_diff)}
    # Patch body text, minus diff syntax, as a leak signature.
    patch_bodies = [
        line[1:].strip()
        for line in gt_diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---")) and len(line.strip()) > 20
    ]

    self_toml = (self_dir / "task.toml").read_text().splitlines()
    gold_toml = (gold_dir / "task.toml").read_text().splitlines()
    toml_delta = [
        line[1:]
        for line in difflib.unified_diff(self_toml, gold_toml, n=0, lineterm="")
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]

    checks = {
        # Only the report section may differ.
        "prompts_differ_only_by_report": not _prompt_delta(self_prompt, gold_prompt),
        # self must not name a ground-truth file or line, nor quote the patch.
        "self_has_no_gt_file": not (gt_files & set(self_prompt.split())),
        "self_has_no_gt_lines": not any(
            f"lines {line}" in self_prompt or f"line {line}" in self_prompt for line in gt_lines
        ),
        "self_has_no_patch_text": not any(body in self_prompt for body in patch_bodies),
        # The verifier scores self without a report; shipping one would let the
        # rendered-report check pass while the arm was scored as assisted.
        "self_has_no_report_spans": not (self_dir / "tests" / "report_spans.json").exists(),
        "gold_has_report_spans": (gold_dir / "tests" / "report_spans.json").exists(),
        # task.toml may differ only in what identifies the task.
        "toml_differs_only_by_identity": all(
            any(key in line for key in _IDENTITY_KEYS) for line in toml_delta
        ),
        # The solution is the developer patch and must never be part of the task
        # the agent is handed; Harbor uploads tests/ only at verify time.
        "gt_patch_not_in_prompt": "diff --git" not in self_prompt
        and "diff --git" not in gold_prompt,
    }
    result["checks"] = checks
    result["failed"] = sorted(k for k, ok in checks.items() if not ok)
    result["status"] = "PASS" if not result["failed"] else "FAIL"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--manifests", type=Path, default=Path("manifests"))
    args = parser.parse_args()

    results = []
    for manifest_path in sorted(args.manifests.glob("*.json")):
        manifest = json.loads(manifest_path.read_text())
        r = check(args.tasks, manifest_path.stem, manifest["gt_diff"])
        if r["status"] == "MISSING":
            print(f"{manifest_path.stem}: no generated repair tasks, skipping")
            continue
        results.append(r)
        print(f"{r['status']:<6} {r['local_id']:<11} {','.join(r.get('failed', []))}")

    passed = sum(1 for r in results if r["status"] == "PASS")
    print(f"\n{passed}/{len(results)} passed")
    return 0 if results and passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
