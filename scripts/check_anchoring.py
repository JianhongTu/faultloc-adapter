#!/usr/bin/env python3
"""Gate: the two scoring conventions each stage picks, asserted behaviourally.

ANCHORING -- routed through anchoring.py, and each stage uses the right rule.
PATH MATCHING -- headline metrics compare whole paths, parity columns compare
bare filenames. Both are caller-side choices over the same vendored scorer, and
both fail silently: the run completes and the numbers look plausible.

The two stages deliberately differ on anchoring, and the gate exists to keep that
deliberate:

  * Localization keeps the vendored FLBench rule as the headline. prompt.md tells the
    agent to name the line before an insertion, so prediction and ground truth share
    the convention and there is no defect to fix; widening it would break FLBench
    parity and penalise an agent for following instructions. The {L, L+1} values ride
    along under `gap_`.
  * Repair uses {L, L+1} as the headline. The agent writes a patch, not spans, so no
    convention can be disclosed to it, and its insertions are scored against report
    spans built from developer deletions. That mismatch is a real false negative.

Both checks are behavioural. An anchoring bug here is silent -- the run completes and
the numbers look plausible -- so asserting the import exists is not enough.

    uv run python scripts/check_anchoring.py \\
        --tasks datasets/flbench-repair-eval500-gold
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faultloc_adapter.anchoring import parse_diff_anchored  # noqa: E402
from faultloc_adapter.scorer import parse_diff  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "faultloc_adapter"

# Insertion of `int guard;` between old lines 10 and 11.
ADD_ONLY = """diff --git a/foo.c b/foo.c
--- a/foo.c
+++ b/foo.c
@@ -10,3 +10,4 @@
 int a;
+int guard;
 int b;
 int c;
"""

# Deletion of old line 21 -- no anchoring ambiguity, must pass through untouched.
DELETION = """diff --git a/bar.c b/bar.c
--- a/bar.c
+++ b/bar.c
@@ -20,3 +20,2 @@
 int x;
-int bad;
 int y;
"""

# dav1d (42523221): one patch touching two same-named files at different lines.
# Under basename matching the two collapse into one `picture.h`, so naming either
# one at the other's line scores a hit. This is the collision the path convention
# exists for.
DUPLICATE_BASENAME = """diff --git a/src/picture.h b/src/picture.h
--- a/src/picture.h
+++ b/src/picture.h
@@ -104,3 +104,2 @@
 int x;
-int bad;
 int y;
diff --git a/include/dav1d/picture.h b/include/dav1d/picture.h
--- a/include/dav1d/picture.h
+++ b/include/dav1d/picture.h
@@ -299,3 +299,2 @@
 int p;
-int also_bad;
 int q;
"""

# One file, no collision -- the shape of the other 497. aom (42470635) is the real
# case: the diff header is project-relative, the agent reads /src/aom and writes
# the container path, and they must still be the same file.
SINGLE_FILE = """diff --git a/av1/common/av1_loopfilter.c b/av1/common/av1_loopfilter.c
--- a/av1/common/av1_loopfilter.c
+++ b/av1/common/av1_loopfilter.c
@@ -104,3 +104,2 @@
 int x;
-int bad;
 int y;
"""


def check_rule(results):
    """The transform itself: insertions gain the following line, deletions do not."""
    raw = parse_diff(ADD_ONLY)[0]
    wide = parse_diff_anchored(ADD_ONLY)[0]
    results.append(("addition_flagged", raw.addition_only))
    results.append(("addition_raw_is_single", sorted(raw.lines) == [10]))
    results.append(("addition_widened_to_gap", sorted(wide.lines) == [10, 11]))

    d_raw = parse_diff(DELETION)[0]
    d_wide = parse_diff_anchored(DELETION)[0]
    results.append(("deletion_not_flagged", not d_raw.addition_only))
    results.append(("deletion_unchanged", d_raw.lines == d_wide.lines))


def check_single_entry_point(results):
    """No module may reach parse_diff directly; anchoring.py is the only door.

    Covers scripts/ as well as the package: a repair-side analysis script that
    computes attribution with the un-widened rule disagrees with repair_score.py
    just as silently as one inside the package would.
    """
    bare = re.compile(r"\bparse_diff\b(?!_anchored|_flbench)")
    # scorer_parity.py is the one sanctioned caller outside anchoring.py: its whole
    # job is reproducing flbench.eval.score byte for byte, so it must use the
    # vendored parse_diff unwidened. Widening it would destroy what it measures.
    # check_anchoring.py compares raw against widened, so it must reach both.
    allowed = {"scorer_parity.py", "check_anchoring.py"}
    offenders = []
    for root in (_SRC, _ROOT / "scripts"):
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(_ROOT)
            if "scorer" in rel.parts or path.name in {"anchoring.py"} | allowed:
                continue  # the vendored copy, and the sanctioned callers
            if bare.search(path.read_text()):
                offenders.append(str(rel))
    results.append(("no_direct_parse_diff", not offenders, offenders))


def check_localization_call_site(results):
    """score.py headline must stay on the FLBench rule, with gap_ alongside."""
    from faultloc_adapter.score import score

    with tempfile.TemporaryDirectory() as d:
        gt = Path(d) / "gt.diff"
        gt.write_text(ADD_ONLY)
        pred = Path(d) / "pred.json"
        # Line 11 is the line the insertion precedes -- the natural answer, and a
        # miss under the unwidened rule.
        pred.write_text(json.dumps([{"file": "foo.c", "line_start": 11, "line_end": 12}]))
        out = score(pred, gt)
    # Naming L+1 alone: a miss under the disclosed FLBench convention, a hit under gap_.
    results.append(("localization_headline_is_flbench", out["hunk_recall"] == 0.0,
                    out["hunk_recall"]))
    results.append(("localization_emits_gap", out["gap_hunk_recall"] == 1.0,
                    out["gap_hunk_recall"]))

    # The parity column must be the UN-widened value. Applying the rule twice is
    # idempotent, so a scorer that re-widens silently collapses the two columns
    # into one and the FLBench comparison disappears without any error.
    with tempfile.TemporaryDirectory() as d:
        gt = Path(d) / "gt.diff"
        gt.write_text(ADD_ONLY)
        pred = Path(d) / "pred.json"
        # Naming only L is perfect under FLBench parity, partial under the rule.
        pred.write_text(json.dumps([{"file": "foo.c", "line_start": 10, "line_end": 11}]))
        out = score(pred, gt)
    # Naming L alone is perfect under FLBench, partial under the gap rule. If the two
    # columns agree here, one of them is being computed twice under the same rule --
    # applying {L, L+1} is idempotent, so that collapse is completely silent.
    results.append(("localization_parity_is_exact", out["iou"] == 1.0, out["iou"]))
    results.append(("columns_not_collapsed", out["iou"] != out["gap_iou"],
                    (out["iou"], out["gap_iou"])))


def check_repair_call_site(results):
    """repair_score.py must widen the agent hunks: an insertion before a reported
    block has to be attributable. This is the uGDjXUn case from the wasm3 run.

    Driven through repair_score.score() rather than through parse_diff_anchored
    directly. Checking the parser in isolation would still pass if score() were
    switched to the un-widened rule while the import stayed (it is also used for
    the flbench parity column), which is precisely the regression that matters.
    """
    from faultloc_adapter import repair_score

    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "repo"
        src.mkdir()
        f = src / "foo.c"
        f.write_text("".join(f"int l{i};\n" for i in range(1, 13)))
        run = lambda *a: subprocess.run(["git", "-C", str(src), *a], capture_output=True)
        run("init", "-q")
        run("config", "user.email", "gate@example.com")
        run("config", "user.name", "gate")
        run("add", "-A")
        run("commit", "-qm", "base")
        run("tag", "base")
        # The agent inserts between old lines 10 and 11 -- anchored at 10 by the
        # vendored rule, so a report naming 11 misses unless the hunk is widened.
        lines = f.read_text().splitlines(keepends=True)
        f.write_text("".join(lines[:10] + ["int guard;\n"] + lines[10:]))

        spans = Path(d) / "spans.json"
        spans.write_text(json.dumps([{"file": "foo.c", "line_start": 11, "line_end": 12}]))

        # The compile/PoC legs need the sidecar; stub them so the attribution
        # branch is what the check actually exercises.
        original_post = repair_score._post
        repair_score._post = lambda path, timeout: {"exit_code": 0, "output": ""}
        try:
            rewards = repair_score.score(
                source=src, baseline="base", condition="gold",
                report_spans_path=spans, artifacts=Path(d) / "art",
                compile_timeout=1, poc_timeout=1,
            )
        finally:
            repair_score._post = original_post

    results.append(("repair_patch_captured", rewards["patch_present"] == 1))
    results.append(("repair_widened", rewards["at_location"] == 1, rewards["at_location"]))
    # The parity column must stay un-widened, or the two are the same number.
    results.append(("repair_flbench_unwidened", rewards["at_location_flbench"] == 0,
                    rewards["at_location_flbench"]))


def _score_prediction(gt_text: str, pred_file: str, line: int) -> dict:
    """Run score() on a one-span prediction against a one-hunk ground truth."""
    from faultloc_adapter.score import score

    with tempfile.TemporaryDirectory() as d:
        gt = Path(d) / "gt.diff"
        gt.write_text(gt_text)
        pred = Path(d) / "pred.json"
        pred.write_text(
            json.dumps([{"file": pred_file, "line_start": line, "line_end": line + 1}])
        )
        return score(pred, gt)


def check_localization_paths(results):
    """A prediction must name the right file, not a file with the right name."""
    # src/picture.h's line, attributed to the other picture.h. FLBench credits it.
    wrong = _score_prediction(DUPLICATE_BASENAME, "include/dav1d/picture.h", 105)
    results.append(("loc_basename_collision_misses", wrong["hunk_recall"] == 0.0,
                    wrong["hunk_recall"]))
    results.append(("loc_parity_keeps_basename", wrong["flbench_hunk_recall"] == 0.5,
                    wrong["flbench_hunk_recall"]))

    # A bare filename suffixes both GT paths, so it identifies neither and must
    # not be resolved to one of them by accident.
    bare = _score_prediction(DUPLICATE_BASENAME, "picture.h", 105)
    results.append(("loc_ambiguous_suffix_misses", bare["hunk_recall"] == 0.0,
                    bare["hunk_recall"]))

    # The exact path still scores, and an equivalent spelling of it must too --
    # a normalisation gap would turn the fix into a blanket zero.
    right = _score_prediction(DUPLICATE_BASENAME, "src/picture.h", 105)
    dotted = _score_prediction(DUPLICATE_BASENAME, "./src/picture.h", 105)
    results.append(("loc_exact_path_hits", right["hunk_recall"] == 0.5,
                    right["hunk_recall"]))
    results.append(("loc_path_normalised", dotted["hunk_recall"] == 0.5,
                    dotted["hunk_recall"]))

    # Rooting invariance, the case that dominates real predictions: the agent
    # reads /src/aom and writes the container path, the diff header is relative to
    # the project root. Same file, and scoring it zero would be a far larger
    # regression than the collision this convention exists to fix.
    deep = _score_prediction(SINGLE_FILE, "src/aom/av1/common/av1_loopfilter.c", 105)
    results.append(("loc_rooting_invariant", deep["iou"] == 1.0, deep["iou"]))
    # A boundary-blind suffix test would let `evil_av1_loopfilter.c` through.
    glued = _score_prediction(SINGLE_FILE, "av1/common/evil_av1_loopfilter.c", 105)
    results.append(("loc_suffix_respects_boundary", glued["iou"] == 0.0, glued["iou"]))


def check_repair_paths(results):
    """A patch must be attributable to the reported file, not to its basename.

    Through repair_score.score() for the same reason as check_repair_call_site:
    the parser is not where the convention is chosen.
    """
    from faultloc_adapter import repair_score

    def attribution(report_path: str) -> dict:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "repo"
            (src / "src").mkdir(parents=True)
            (src / "include" / "dav1d").mkdir(parents=True)
            body = "".join(f"int l{i};\n" for i in range(1, 13))
            (src / "src" / "picture.h").write_text(body)
            (src / "include" / "dav1d" / "picture.h").write_text(body)
            run = lambda *a: subprocess.run(["git", "-C", str(src), *a], capture_output=True)
            run("init", "-q")
            run("config", "user.email", "gate@example.com")
            run("config", "user.name", "gate")
            run("add", "-A")
            run("commit", "-qm", "base")
            run("tag", "base")
            # The agent edits src/picture.h and leaves include/dav1d/picture.h alone.
            lines = (src / "src" / "picture.h").read_text().splitlines(keepends=True)
            (src / "src" / "picture.h").write_text(
                "".join(lines[:4] + ["int fixed;\n"] + lines[5:])
            )

            spans = Path(d) / "spans.json"
            spans.write_text(
                json.dumps([{"file": report_path, "line_start": 5, "line_end": 6}])
            )
            original_post = repair_score._post
            repair_score._post = lambda path, timeout: {"exit_code": 0, "output": ""}
            try:
                return repair_score.score(
                    source=src, baseline="base", condition="gold",
                    report_spans_path=spans, artifacts=Path(d) / "art",
                    compile_timeout=1, poc_timeout=1,
                )
            finally:
                repair_score._post = original_post

    wrong = attribution("include/dav1d/picture.h")
    right = attribution("src/picture.h")
    results.append(("repair_basename_collision_misses", wrong["at_location"] == 0,
                    wrong["at_location"]))
    results.append(("repair_parity_keeps_basename", wrong["at_location_flbench"] == 1,
                    wrong["at_location_flbench"]))
    results.append(("repair_exact_path_hits", right["at_location"] == 1,
                    right["at_location"]))


def check_shipped(results, tasks: Path):
    """Every generated task must carry anchoring.py, byte-identical to source."""
    want = (_SRC / "anchoring.py").read_bytes()
    missing, stale = [], []
    task_dirs = [p for p in sorted(tasks.iterdir()) if (p / "tests").is_dir()]
    for t in task_dirs:
        shipped = t / "tests" / "anchoring.py"
        if not shipped.exists():
            missing.append(t.name)
        elif shipped.read_bytes() != want:
            stale.append(t.name)
    results.append(("tasks_found", bool(task_dirs), len(task_dirs)))
    results.append(("all_tasks_ship_anchoring", not missing, missing[:5]))
    results.append(("shipped_copies_current", not stale, stale[:5]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=Path, help="generated dataset dir")
    args = ap.parse_args()

    results: list[tuple] = []
    check_rule(results)
    check_single_entry_point(results)
    check_localization_call_site(results)
    check_repair_call_site(results)
    check_localization_paths(results)
    check_repair_paths(results)
    if args.tasks:
        check_shipped(results, args.tasks)

    failed = 0
    for row in results:
        name, ok = row[0], row[1]
        detail = f"  {row[2]}" if len(row) > 2 and not ok else ""
        print(f"{'PASS' if ok else 'FAIL'}   {name}{detail}")
        failed += not ok
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
