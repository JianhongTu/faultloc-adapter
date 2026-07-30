"""Assert a repair dataset differs from the `gold` dataset only by the report.

The uplift a repair experiment measures is only attributable to the report if the
report is the sole difference between the datasets being compared. Two ways that
silently breaks: the prompts drift apart for unrelated reasons, or the part of the
prompt that is NOT the report ends up carrying ground-truth content it should
never see. Both are checked here, statically, on the generated tasks -- no
containers, so it is cheap enough to run on every regeneration.

Repair datasets are a SET, not a pair: `gold` plus one dataset per localization
model evaluated. `gold` is the reference every other member is read against, so
this runs once per candidate, comparing it to gold:

    # gold on its own -- content checks only, nothing to compare against
    python scripts/repair_boundaries.py --tasks datasets/flbench-repair-eval500-gold

    # a diagnosis dataset -- content checks plus drift against gold
    python scripts/repair_boundaries.py \\
        --tasks datasets/flbench-repair-eval500-diagnosis-qwen3.6-27b

Run it for each candidate dataset before its job. A candidate that passes against
gold is comparable with gold; two candidates that each pass are comparable with
each other, because they are each identical to the same reference.
"""

import argparse
import difflib
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faultloc_adapter.adapter import DATASET_ROOT  # noqa: E402
from faultloc_adapter.anchoring import parse_diff_anchored  # noqa: E402
from faultloc_adapter.repair import GOLD_SOURCE, UNRUN_LEDGER, dataset_name  # noqa: E402

# Lines whose presence in a task.toml difference is expected: the two datasets are
# different conditions and say so. `name =` is included because a caller may point
# --tasks and --reference at directories generated under different --org values;
# within the standard layout the task names are identical.
_IDENTITY_KEYS = ("name =", "description =", "condition =")


_REPORT_HEADING = "## Reported Root Cause"


def _split_report(text: str) -> tuple[str, str | None]:
    """Split a prompt into (everything outside the report, the report section).

    The report runs from its heading to the next `## `. Returns None for the
    report when the arm rendered none, which is itself a failure -- both arms are
    supposed to carry one.
    """
    lines = text.splitlines()
    try:
        start = lines.index(_REPORT_HEADING)
    except ValueError:
        return text, None
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[:start] + lines[end:]), "\n".join(lines[start:end])


def _strip_sanitizer_report(body: str, crash_output: str) -> str:
    """Remove the verbatim reproducer output from the text the leak scan covers.

    The sanitizer report is evidence, not ground truth: it is what running the PoC
    against the unmodified binary prints, it is byte-identical in every arm, and
    nothing in it comes from the developer patch. It does contain line numbers --
    ASan names the declaration site of each stack variable, `'tempBuf' (line 318)`
    -- and on ~3% of the set one of those collides with a ground-truth line
    because the variable is declared near the fix. Scanning it flags that
    coincidence as a leak.

    So the scan covers only generator-authored prose. The strip must apply the
    same per-line rstrip the generator does (`_instruction` in repair.py) --
    manifests store this output with CRLF endings and trailing spaces, neither of
    which survives into the prompt, so matching the raw text finds nothing and the
    exclusion silently does nothing. A no-op is at least loud rather than
    dangerous: it leaves these instances failing, it cannot turn a real leak green.
    """
    normalized = "\n".join(line.rstrip() for line in crash_output.splitlines())
    return body.replace(normalized.strip(), "")


def check(tasks: Path, local_id: str, gt_diff: str, label: str,
          crash_output: str = "") -> dict:
    """Check one dataset's task for one instance, in isolation.

    Every dataset carrying a report needs this. `gold`'s cause is written by a
    model that was shown the developer patch, so it is the likeliest source to
    leak the fix -- and `_review()` in annotate_cause.py is a regex heuristic,
    while the patch-body and ground-truth-line checks here are not.
    """
    arm_dir = tasks / f"repair__{local_id}"
    result = {"local_id": local_id, "arm": label}
    if not arm_dir.exists():
        result["status"] = "MISSING"
        return result

    prompt = (arm_dir / "instruction.md").read_text()
    body, report = _split_report(prompt)
    if crash_output:
        body = _strip_sanitizer_report(body, crash_output)

    # Every ground-truth line number, as it would be written in the report.
    # Repair side, so the widened rule: a leak check must recognise every line the
    # report could legitimately name, and for an insertion that includes L+1.
    gt_lines = {line for hunk in parse_diff_anchored(gt_diff) for line in hunk.lines}
    gt_files = {hunk.file.rsplit("/", 1)[-1] for hunk in parse_diff_anchored(gt_diff)}
    # Patch text as a leak signature, split by side. Outside the report nothing
    # from either side belongs. Inside it, only the ADDED lines are a leak: they
    # are the developer's fix. A removed line is the buggy code, and naming the
    # buggy code is what a root-cause report is for -- flagging that would fail
    # the arm for doing its job, and annotate_cause.py's own _review() already
    # draws the line at '+' only.
    patch_added = [
        line[1:].strip()
        for line in gt_diff.splitlines()
        if line.startswith("+") and not line.startswith("+++") and len(line.strip()) > 20
    ]
    patch_bodies = patch_added + [
        line[1:].strip()
        for line in gt_diff.splitlines()
        if line.startswith("-") and not line.startswith("---") and len(line.strip()) > 20
    ]

    checks = {
        # Naming the fault is the report's job, so these run on the rest of the
        # prompt: what every arm shares must be free of ground truth, or the
        # report is not the only thing telling the agent where to look.
        "outside_report_has_no_gt_file": not (gt_files & set(body.split())),
        "outside_report_has_no_gt_lines": not any(
            f"lines {line}" in body or f"line {line}" in body for line in gt_lines
        ),
        "outside_report_has_no_patch_text": not any(body_ in body for body_ in patch_bodies),
        # The report may name the fault; it may not restate the developer's edit.
        # That is the difference between a diagnosis and a leaked answer.
        "renders_report_section": report is not None,
        "report_does_not_quote_fix": report is not None
        and not any(added in report for added in patch_added),
        "has_report_spans": (arm_dir / "tests" / "report_spans.json").exists(),
        # The solution is the developer patch and must never be part of the task
        # the agent is handed; Harbor uploads tests/ only at verify time.
        "gt_patch_not_in_prompt": "diff --git" not in prompt,
    }
    result["checks"] = checks
    result["failed"] = sorted(k for k, ok in checks.items() if not ok)
    result["status"] = "PASS" if not result["failed"] else "FAIL"
    return result


def load_unrun(tasks: Path) -> set[str]:
    """Instances this dataset deliberately does not contain."""
    path = tasks / UNRUN_LEDGER
    if not path.exists():
        return set()
    return {str(e["local_id"]) for e in json.loads(path.read_text())}


def check_drift(tasks: Path, reference: Path, local_id: str, unrun: set[str]) -> dict:
    """Assert a candidate dataset's task differs from gold's only by the report.

    Three outcomes, not two. An absent task is SKIP -- never PASS -- because the
    check did not run, and a board of PASS lines exiting 0 would read as "the
    datasets were compared and matched" when nothing was compared.

    But an instance the generator deliberately did not write is NOTRUN: stage 1
    named no location, it scores 0 without a rollout, and this dataset's not-run
    ledger says so. That is an expected state for a complete diagnosis dataset, so
    it does not fail the gate -- otherwise every batch containing one
    no-prediction instance would be permanently red, and the exit code people
    learn to ignore is the one that stops protecting them.

    An instance missing from the REFERENCE is always a failure, never NOTRUN: gold
    is derived from the developer patch and has no legitimate reason to be short.
    """
    dirs = {"candidate": tasks / f"repair__{local_id}",
            "reference": reference / f"repair__{local_id}"}
    missing = [a for a, d in dirs.items() if not d.exists()]
    if missing:
        if missing == ["candidate"] and local_id in unrun:
            return {"local_id": local_id, "arm": "vs-gold", "failed": [],
                    "status": "NOTRUN",
                    "detail": "not run (no stage-1 prediction)"}
        return {"local_id": local_id, "arm": "vs-gold", "failed": [],
                "status": "SKIP", "detail": f"absent from {'/'.join(missing)}"}

    split = {a: _split_report((d / "instruction.md").read_text()) for a, d in dirs.items()}
    bodies = {a: s[0] for a, s in split.items()}
    reports = {a: s[1] for a, s in split.items()}
    tomls = {a: (d / "task.toml").read_text().splitlines() for a, d in dirs.items()}
    toml_delta = [
        line[1:]
        for line in difflib.unified_diff(
            tomls["reference"], tomls["candidate"], n=0, lineterm=""
        )
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]

    checks = {
        # Outside the report the two datasets must be byte-identical.
        "prompts_differ_only_by_report": bodies["reference"] == bodies["candidate"],
        # ... and inside it they must NOT be. The manipulated variable has to
        # actually vary: a diagnosis report directory populated by copying the gold
        # reports would otherwise generate cleanly and run gold against gold, and
        # the null result would be indistinguishable from a real one.
        "reports_actually_differ": reports["reference"] != reports["candidate"],
        # task.toml may differ only in what identifies the condition.
        "toml_differs_only_by_identity": all(
            any(key in line for key in _IDENTITY_KEYS) for line in toml_delta
        ),
    }
    return {
        "local_id": local_id,
        "arm": "vs-gold",
        "checks": checks,
        "failed": sorted(k for k, ok in checks.items() if not ok),
        "status": "PASS" if all(checks.values()) else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True,
                        help="the repair dataset to check")
    parser.add_argument(
        "--reference", type=Path, default=DATASET_ROOT / dataset_name(GOLD_SOURCE),
        help=("the dataset to compare against (default: the gold dataset). When it "
              "is the same directory as --tasks, only the content checks run: "
              "gold has nothing to be read against."),
    )
    parser.add_argument("--manifests", type=Path, default=Path("manifests"))
    args = parser.parse_args()

    label = args.tasks.resolve().name
    self_check = args.tasks.resolve() == args.reference.resolve()
    if not self_check and not args.reference.exists():
        parser.error(
            f"reference dataset {args.reference} does not exist -- generate the "
            f"gold dataset first, or pass --reference explicitly"
        )
    unrun = load_unrun(args.tasks)

    results = []
    for manifest_path in sorted(args.manifests.glob("*.json")):
        manifest = json.loads(manifest_path.read_text())
        r = check(args.tasks, manifest_path.stem, manifest["gt_diff"], label,
                  manifest.get("crash_output", ""))
        found = r["status"] != "MISSING"
        if found:
            results.append(r)
            print(f"{r['status']:<6} {r['local_id']:<11} {label:<30} "
                  f"{','.join(r.get('failed', []))}")
        if self_check:
            # Nothing to compare the reference against. An instance it is missing
            # is still a hole in the evaluation and must not pass silently -- left
            # as a bare print it contributed nothing to `graded`, so a run that
            # lost an instance still exited 0 and reported "500/500" over 499.
            if found:
                continue
            drift = {"local_id": manifest_path.stem, "arm": "vs-gold",
                     "failed": [], "status": "SKIP",
                     "detail": "no task generated for this instance"}
        else:
            # Handles the absent-task cases itself: NOTRUN when this dataset's
            # ledger accounts for it, SKIP otherwise.
            drift = check_drift(args.tasks, args.reference, manifest_path.stem, unrun)
        results.append(drift)
        print(f"{drift['status']:<6} {drift['local_id']:<11} "
              f"{drift['arm']:<30} "
              f"{','.join(drift['failed']) or drift.get('detail', '')}")

    per_arm = [r for r in results if r["arm"] != "vs-gold"]
    drifts = [r for r in results if r["arm"] == "vs-gold"]
    skipped = [r for r in drifts if r["status"] == "SKIP"]
    notrun = [r for r in drifts if r["status"] == "NOTRUN"]
    graded = per_arm + [r for r in drifts if r["status"] not in ("SKIP", "NOTRUN")]
    passed = sum(1 for r in graded if r["status"] == "PASS")
    by_arm = Counter(r["arm"] for r in graded)
    print(f"\n{passed}/{len(graded)} passed across {len(by_arm)} check(s): "
          f"{', '.join(f'{a}={n}' for a, n in sorted(by_arm.items()))}")
    if notrun:
        # Expected, and recorded in the ledger -- reported so the count is
        # visible, but it does not fail the gate.
        print(f"drift check NOT RUN for {len(notrun)} instance(s): stage 1 named "
              f"no location, those instances score 0 without a rollout.")
    if skipped:
        # Loudly, and non-zero: the drift invariant is the one this script exists
        # to defend, and "it did not run" must never look like "it passed". Broken
        # out by reason -- "this dataset is short an instance" and "the reference
        # is short an instance" are the same exit code and very different problems.
        print(f"drift check SKIPPED for {len(skipped)} instance(s) "
              f"-- nothing was compared:")
        for detail, n in sorted(Counter(r.get("detail", "?") for r in skipped).items()):
            print(f"  {n:>4}  {detail}")
    return 0 if graded and passed == len(graded) and not skipped else 1


if __name__ == "__main__":
    sys.exit(main())
