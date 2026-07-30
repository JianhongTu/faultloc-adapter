"""Harbor verifier entrypoint for the repair task.

Captures the agent's patch, rebuilds from the agent's own tree, re-runs the PoC,
and scores whether the patch landed where the report said it would. Every arm
carries a report, so attribution is always scored.

Two things this deliberately does NOT do:

  * collapse the outcomes. `compiled`, `poc_suppressed`, `patch_present` and
    `at_location` are all emitted separately, and `repair_ok` carries the plain
    build-and-suppress result. `verified` additionally requires attribution --
    a working fix the report did not cause is not what this measures -- so
    without `repair_ok` the raw repair uplift would not be recoverable from the
    logs.
  * turn infrastructure into a score. A sidecar that cannot be reached, or a
    source sync that fails, raises: no reward file is written and Harbor errors
    the trial. Nothing downstream could tell a broken sidecar from a bad agent.

Attribution is `hunk_recall > 0` on the report-vs-patch scoring, i.e. at least one
hunk of the agent's patch sits at a reported location. That is threshold-free on
purpose: the continuous metrics are all stored, so a calibrated cutoff can replace
this rule later without re-running anything.

It is measured with the agent's insertions re-anchored to the gap they occupy
(anchoring.py). Without that, the two sides of this comparison are built under
different conventions -- the agent's insertion anchors to the line before it,
the developer's deletion has concrete lines -- and a patch that fixes the bug at
exactly the reported site scores zero attribution. Unlike stage 1 there is
nothing to disclose to the agent: it writes a patch, not spans, so it cannot
comply with an anchoring convention even in principle.

It is also measured on whole project-relative paths (scorer/metrics.py,
`full_path`). FLBench compares bare filenames, which here would credit a patch for
changing a different file that shares a final component with the reported one --
attribution is the entire point of this metric, so borrowing that defect is not an
option. `report_flbench_*` and `at_location_flbench` carry the published convention
on both axes -- bare filenames, un-widened anchor -- for comparability only.
"""

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

try:  # inside the verifier image the scorer sits next to this file
    from anchoring import parse_diff_anchored, parse_diff_flbench
    from scorer import Span, evaluate_sample
except ImportError:  # running from the adapter package
    from .anchoring import parse_diff_anchored, parse_diff_flbench
    from .scorer import Span, evaluate_sample

SIDECAR = "http://poc:8080"
METRIC_KEYS = ("iou", "hunk_recall", "file_recall", "line_recall", "line_precision", "line_f1")

# Ships in tests/, which Harbor uploads only at verify time, so this file exists
# for the verifier and never for the agent. That is what makes /compile
# verifier-only; see repair.py, COMPILE_TOKEN_HEADER.
COMPILE_TOKEN_PATH = Path("/tests/compile_token")


class InfrastructureError(Exception):
    """Raised for failures that are not the agent's: no reward file is written."""


def _post(path: str, timeout: int) -> dict:
    req = urllib.request.Request(f"{SIDECAR}{path}", method="POST", data=b"")
    if path == "/compile":
        try:
            req.add_header("X-Compile-Token", COMPILE_TOKEN_PATH.read_text().strip())
        except OSError as e:
            raise InfrastructureError(f"no compile token at {COMPILE_TOKEN_PATH}: {e}") from e
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:  # unreachable sidecar, timeout, malformed body
        raise InfrastructureError(f"POST {path}: {e}") from e


def capture_patch(source: Path, baseline: str) -> str:
    """Diff the agent's tree against the staging baseline.

    `git add -A` first so files the agent created are tracked and therefore
    appear; the diff itself is worktree-vs-baseline, which also covers anything
    the agent committed or staged along the way.
    """
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(source), *args], capture_output=True, text=True
        )

    if git("rev-parse", "--verify", baseline).returncode != 0:
        raise InfrastructureError(
            f"baseline ref {baseline!r} missing from {source}; staging did not complete"
        )
    git("add", "-A")
    out = git("diff", baseline)
    if out.returncode != 0:
        raise InfrastructureError(f"git diff {baseline} failed: {out.stderr.strip()}")
    return out.stdout


def score(
    source: Path,
    baseline: str,
    condition: str,
    report_spans_path: Path,
    artifacts: Path,
    compile_timeout: int,
    poc_timeout: int,
) -> dict:
    artifacts.mkdir(parents=True, exist_ok=True)
    patch = capture_patch(source, baseline)
    (artifacts / "patch.diff").write_text(patch)

    rewards = {
        "patch_present": int(bool(patch.strip())),
        "compiled": 0,
        "poc_suppressed": 0,
    }
    compile_out = poc_out = ""

    # Nothing to build if the tree is unchanged, and nothing to learn from
    # building it: the outcome is the known-vulnerable baseline by construction.
    if rewards["patch_present"]:
        result = _post("/compile", compile_timeout)
        if result.get("sync_failed"):
            raise InfrastructureError("sidecar could not sync the agent's source tree")
        compile_out = result.get("output", "")
        rewards["compiled"] = int(result.get("exit_code") == 0)

        if rewards["compiled"]:
            result = _post("/poc", poc_timeout)
            poc_out = result.get("output", "")
            # Exit 0 means the reproducer ran to completion without tripping the
            # sanitizer. The sidecar has already retried the sanitizer-init crash,
            # so a non-zero code here is a real surviving crash.
            rewards["poc_suppressed"] = int(result.get("exit_code") == 0)

    (artifacts / "compile.log").write_text(compile_out)
    (artifacts / "poc.log").write_text(poc_out)

    rewards["repair_ok"] = int(
        rewards["patch_present"] and rewards["compiled"] and rewards["poc_suppressed"]
    )

    raw = json.loads(report_spans_path.read_text())
    spans = [Span(s["file"], s["line_start"], s["line_end"]) for s in raw]
    hunks = parse_diff_anchored(patch)
    if hunks:
        metrics = evaluate_sample(spans, hunks, full_path=True)
        fl = evaluate_sample(spans, parse_diff_flbench(patch))
        rewards.update({f"report_{k}": metrics[k] for k in METRIC_KEYS})
        rewards.update({f"report_flbench_{k}": fl[k] for k in METRIC_KEYS})
        rewards["at_location"] = int(metrics["hunk_recall"] > 0)
        rewards["at_location_flbench"] = int(fl["hunk_recall"] > 0)
    else:
        # An empty or unparseable patch touches no line, so it can sit at no
        # reported location.
        rewards.update({f"report_{k}": 0.0 for k in METRIC_KEYS})
        rewards.update({f"report_flbench_{k}": 0.0 for k in METRIC_KEYS})
        rewards["at_location"] = 0
        rewards["at_location_flbench"] = 0
    # A fix only counts when it is attributable to the reported root cause, so
    # at_location is a hard requirement and not a diagnostic. There is
    # deliberately no branch that scores `verified` without it: the retired
    # `self` arm was the only condition without a report, and letting a
    # report-less task through would score a fix nothing caused.
    rewards["verified"] = int(rewards["repair_ok"] and rewards["at_location"])

    # `condition` is not emitted as a reward: Harbor averages these keys, and the
    # condition is already carried by the task name and [metadata].
    return {"reward": float(rewards["verified"]), **rewards}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--condition", required=True)
    parser.add_argument("--report-spans", type=Path, required=True,
                        help="Spans the fix must be attributable to")
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--compile-timeout", type=int, default=2000)
    parser.add_argument("--poc-timeout", type=int, default=600)
    args = parser.parse_args()

    rewards = score(
        args.source,
        args.baseline,
        args.condition,
        args.report_spans,
        args.artifacts,
        args.compile_timeout,
        args.poc_timeout,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rewards, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
