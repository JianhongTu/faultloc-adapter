"""Harbor verifier entrypoint for the repair task.

The mechanical gates of the study, in code. The agent is handed the developer's
patch and asked to repair the same defect somewhere else, so capture, build and
reproduce are joined by three further gates and one inversion:

  * ADDED: the project's own regression suite must not go redder than it already
    was. The smoke that motivated this produced three candidates that passed every
    other gate, and one of them broke libxml2's suite -- 3158 checks clean before,
    2 errors after. Building and suppressing the reproducer is not evidence of a
    repair.
  * RECORDED: anchored line overlap with the developer's spans, and the metrics
    behind it. Not a gate -- whether a candidate is a genuinely different repair
    is the audit's judgement, and a line intersection answers a narrower
    question than that.

REGRESSION IS TERMINAL, and reported as its own outcome. Terminal because the
agent has the same suite as a tool: a regression that survives to verify time is
one the agent either never ran or could not fix, not a near miss for an auditor to
weigh. Separately reported because `regression_detected` is a fourth distinct way
to reach "no accepted reference" -- alongside no candidate found, a candidate
rejected on substance, and a candidate rejected for want of evidence -- and
section 10 must not average them.

TWO OUTPUTS, because they have different consumers and different type rules.

`reward.json` is numbers only -- Harbor validates it as `dict[str, float | int]`
(harbor/models/verifier/result.py:5), so a changed-file list or an outcome string
in there is a pydantic error that fails the trial rather than a field Harbor
ignores. The outcome therefore ships as one-hot `outcome_*` keys, which is not a
workaround but the useful shape: Harbor's mean over trials of `outcome_poc_failed`
IS that row of the section 10 funnel.

`/logs/artifacts/mechanical.json` carries everything with structure -- which files
were changed, which lines collided, and the agent's
own summary.json parsed and preserved. That is the section 5.2 gate-7 record and
the input the section 7 audit reads.

Mechanical acceptance is NOT the claim. It establishes claims 1 and 2 of section
2; claim 3 -- that the patch repairs the root cause rather than suppressing the
symptom -- is a dual-agent audit that runs on the artifacts this writes.

Gate order follows section 5.2: the free checks first, then the build, then the
overlap test. Overlap could be decided without building at all, and doing so would
save a compile on every candidate that lands on the developer's lines -- but then
the funnel could not say whether those candidates were even executable, and
"rejected for overlap" would silently mean two different things.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

try:  # inside the verifier image the scorer sits next to this file
    from anchoring import parse_diff_anchored
    from scorer import Span, evaluate_sample
except ImportError:  # running from the adapter package
    from .anchoring import parse_diff_anchored
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
    # /test is gated on the same token as /compile: both sync and act on the
    # agent's tree, and both must be unavailable to the family that gets neither.
    if path in ("/compile", "/test"):
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


# Section 5.2's mechanical outcomes, minus `infra_error`: that one deliberately
# writes no reward file at all, so Harbor errors the trial instead of recording a
# result the agent did not produce. Emitted one-hot into reward.json so a job's
# mean over trials is the funnel directly.
OUTCOMES = (
    "no_patch",
    "build_failed",
    "poc_failed",
    "regression_detected",
    "executable_candidate",
)

# The agent's structured submission. Its causal explanation is what the later
# root-cause audit reads, so a candidate that arrives without one is a patch with
# no stated reasoning behind it -- still mechanically classifiable, but weaker
# evidence. Checked here rather than trusted, and preserved verbatim.
SUMMARY_FILE = "summary.json"

# A field filled with a stand-in renders downstream exactly like a real
# explanation, so placeholders are rejected rather than accepted as text.
_PLACEHOLDERS = frozenset({"n/a", "na", "none", "tbd", "unknown", "-", "(none)"})


def _text(doc: dict, key: str) -> str | None:
    value = doc.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return None if not value or value.lower() in _PLACEHOLDERS else value


def check_summary(artifacts: Path, produced_patch: bool) -> tuple[dict | None, list[str]]:
    """Validate the agent's summary.json. Returns (parsed doc or None, errors).

    `produced_patch` cross-checks the agent's own `alternative_fix_exists` against
    what is actually in its working tree. That claim is the one field an auditor
    would otherwise take at face value, and it is free to verify here: an agent
    that reports success while leaving the tree untouched, or reports failure over
    a real patch, has told us something about the attempt that the diff alone does
    not. It is recorded, not punished -- the mechanical outcome still comes from
    the patch.
    """
    path = artifacts / SUMMARY_FILE
    if not path.exists():
        return None, ["summary.json is missing"]
    try:
        doc = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, [f"summary.json is not valid JSON: {e}"]
    if not isinstance(doc, dict):
        return None, ["summary.json is not a JSON object"]

    errors = []
    for key in ("root_cause", "gold_patch_explanation"):
        if _text(doc, key) is None:
            errors.append(f"{key} is missing, empty, or a placeholder")

    claimed = doc.get("alternative_fix_exists")
    if not isinstance(claimed, bool):
        errors.append("alternative_fix_exists is missing or not a boolean")
    else:
        required = "alternative_patch_explanation" if claimed else "no_alternative_explanation"
        forbidden = "no_alternative_explanation" if claimed else "alternative_patch_explanation"
        if _text(doc, required) is None:
            errors.append(f"{required} is required when alternative_fix_exists is {claimed}")
        if _text(doc, forbidden) is not None:
            errors.append(f"{forbidden} must be null when alternative_fix_exists is {claimed}")
        if claimed != produced_patch:
            errors.append(
                f"alternative_fix_exists is {claimed} but the working tree "
                f"{'has' if produced_patch else 'has no'} production-source change"
            )
    return doc, errors


def changed_files(source: Path, baseline: str, diff_filter: str = "") -> list[str]:
    """Paths the agent changed. Read from git, not from the diff body.

    A mode change or a binary file produces a diff header with no hunks, so
    parsing the patch would miss it, and the record of what a candidate touched
    has to cover those too.

    `diff_filter` is passed through to git. "MD" asks the narrower question --
    what that shipped with the project did the agent modify or delete -- which is
    what the summary cross-check needs: a file the agent ADDED is as likely to be
    a note or a scratch script as a fix, and counting it as one manufactured a
    contradiction against an agent that honestly reported finding nothing.
    """
    out = subprocess.run(
        ["git", "-C", str(source), "diff", "--name-only",
         *([f"--diff-filter={diff_filter}"] if diff_filter else []), baseline],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise InfrastructureError(f"git diff --name-only failed: {out.stderr.strip()}")
    return [line for line in out.stdout.splitlines() if line.strip()]


def gold_overlap(patch: str, gold_spans: list[Span]) -> tuple[list[dict], dict]:
    """Anchored lines the candidate shares with the developer patch, and metrics.

    The overlap itself is a direct set intersection rather than a thresholded
    metric, because the gate is "zero" and a metric would leave the question of
    which side of a rounding boundary zero sits on. The scorer metrics come along
    for the ledger: they are the same convention section 9 rescores under, so a
    near-miss candidate is legible later without recomputing anything.
    """
    by_file: dict[str, set[int]] = {}
    for span in gold_spans:
        by_file.setdefault(span.file, set()).update(range(span.line_start, span.line_end))

    hunks = parse_diff_anchored(patch)
    collisions = []
    for hunk in hunks:
        shared = sorted(set(hunk.lines) & by_file.get(hunk.file, set()))
        if shared:
            collisions.append({"file": hunk.file, "lines": shared})

    metrics = (
        {k: evaluate_sample(gold_spans, hunks, full_path=True)[k] for k in METRIC_KEYS}
        if hunks else {k: 0.0 for k in METRIC_KEYS}
    )
    return collisions, metrics


def changed_spans(patch: str) -> list[dict]:
    """The candidate's own anchored spans, frozen for grouping (section 6.1)."""
    return [
        {"file": h.file, "lines": sorted(h.lines)} for h in parse_diff_anchored(patch)
    ]


def score(
    source: Path,
    baseline: str,
    gold_spans_path: Path,
    artifacts: Path,
    compile_timeout: int,
    poc_timeout: int,
    local_id: int = 0,
    test_timeout: int = 2400,
) -> dict:
    artifacts.mkdir(parents=True, exist_ok=True)
    patch = capture_patch(source, baseline)
    (artifacts / "patch.diff").write_text(patch)

    raw = json.loads(gold_spans_path.read_text())
    gold_spans = [Span(s["file"], s["line_start"], s["line_end"]) for s in raw]

    files = changed_files(source, baseline)
    edited = changed_files(source, baseline, "MD")

    # Cross-checked against the agent's own claim, so it is resolved before the
    # gates branch -- every exit path below must carry the same summary verdict.
    summary, summary_errors = check_summary(artifacts, bool(edited))

    rewards = {
        "patch_present": int(bool(patch.strip())),
        "source_edited": int(bool(edited)),
        "summary_present": int(summary is not None),
        "summary_valid": int(summary is not None and not summary_errors),
        "summary_consistent": int(
            summary is not None
            and summary.get("alternative_fix_exists") == bool(edited)
        ),
        "compiled": 0,
        "poc_suppressed": 0,
        "tests_ran": 0,
        "no_regression": 0,
        "gold_overlap": 0,
        **{f"gold_{k}": 0.0 for k in METRIC_KEYS},
    }
    record = {
        # Self-identifying, because mechanical.json is THE per-trial record: a
        # hundred of these collected into a ledger must not depend on the
        # directory they were read from to say which instance they describe.
        "local_id": local_id,
        "condition": "repair",
        "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
        "changed_files": files,
        "edited_files": edited,
        "gold_overlap_lines": [],
        # The whole comparison, not a verdict: `regression_detected` with no test
        # names is a rejection nobody can check, and step 4's calibration depends
        # on being able to see which tests moved and on what basis.
        "regression": None,
        # Preserved verbatim next to the mechanical result, so the root-cause
        # audit reads one file rather than joining two.
        "summary": summary,
        "summary_errors": summary_errors,
    }
    (artifacts / "changed_spans.json").write_text(
        json.dumps(changed_spans(patch), indent=2) + "\n"
    )

    def finish(outcome: str) -> dict:
        record["outcome"] = outcome
        record.update(rewards)
        (artifacts / "mechanical.json").write_text(json.dumps(record, indent=2) + "\n")
        return {
            "reward": float(outcome == "executable_candidate"),
            **rewards,
            **{f"outcome_{o}": int(o == outcome) for o in OUTCOMES},
        }

    # Gate 2, and the no-patch case. An agent that concluded no alternative exists
    # lands here too, and that is a search result rather than a failure -- the
    # prompt invites it explicitly.
    #
    # The agent's own declaration decides this, not the file list. Inferring it
    # from the diff made any file left in the tree -- a note, a scratch script, a
    # .orig backup -- count as a patch, so an honest negative was carried past
    # this gate, built, and then filed as poc_failed for a crash it never claimed
    # to have fixed. Trusting the declaration is safe in the direction that
    # matters: `no_patch` earns no reward, so claiming it cannot buy anything.
    # There is nothing to build or test once the agent has said there is nothing
    # to evaluate.
    if summary is not None and summary.get("alternative_fix_exists") is False:
        return finish("no_patch")

    # Fallback for a trial with no usable summary -- the agent died, or wrote
    # something unparseable. An empty tree is the same result either way.
    if not patch.strip():
        return finish("no_patch")


    result = _post("/compile", compile_timeout)
    if result.get("sync_failed"):
        raise InfrastructureError("sidecar could not sync the agent's source tree")
    (artifacts / "compile.log").write_text(result.get("output", ""))
    rewards["compiled"] = int(result.get("exit_code") == 0)
    if not rewards["compiled"]:
        return finish("build_failed")

    result = _post("/poc", poc_timeout)
    (artifacts / "poc.log").write_text(result.get("output", ""))
    rewards["poc_suppressed"] = int(result.get("exit_code") == 0)
    if not rewards["poc_suppressed"]:
        return finish("poc_failed")

    # Gate 5, and only where a suite exists. Placed after the reproducer and
    # before the overlap test: a candidate that breaks the project is rejected
    # whatever it did about location, and running the suite for a candidate that
    # never suppressed the crash would spend the most expensive check in the
    # funnel on a trial already decided.
    #
    # The SCRIPT never comes from the agent's tree: it is staged into the sidecar
    # (sidecar/server.py, TEST_SCRIPT), so a candidate that edited the project's
    # own test sources still gets run against the suite we froze.
    # THE SUITE'S EXIT CODE IS THE VERDICT. test.sh is hand-authored per project
    # and its contract is "exit 0 when the result matches what was recorded on
    # the unpatched tree" -- which is NOT "every test passed". Two of the three
    # instances measured are red before anything is patched, so a green-suite
    # rule would reject the developer's own patch; the author encodes the
    # expected result in the script, and the verifier reads one number.
    #
    # Run unconditionally, including for instances whose suite is still the
    # generated placeholder. The placeholder exits non-zero on purpose: an
    # unauthored instance must fail visibly rather than pass quietly.
    result = _post("/test", test_timeout)
    if result.get("sync_failed"):
        raise InfrastructureError("sidecar could not sync the tree for the suite")
    (artifacts / "tests.log").write_text(result.get("output", ""))
    rewards["tests_ran"] = 1

    # A suite that was killed at the cap was not measured. 124 is `timeout`'s
    # exit code and -1 is the sidecar giving up on its own subprocess; either
    # way this is our cap and the suite's slowness, and neither is the
    # candidate's doing.
    rc = result.get("exit_code", 1)
    if rc in (124, -1):
        record["regression"] = {"timed_out": True, "exit_code": rc}
        (artifacts / "mechanical.json").write_text(json.dumps(record, indent=2) + "\n")
        raise InfrastructureError(
            f"regression suite did not finish at the sidecar's cap (exit {rc})"
        )

    rewards["no_regression"] = int(rc == 0)
    record["regression"] = {"exit_code": rc}
    if rc != 0:
        return finish("regression_detected")

    # Measured, not gated. Whether a candidate sits far enough from the
    # developer's lines to be a different repair is a judgement about the code,
    # and an exact line intersection answers a narrower question than that --
    # anchoring puts an insertion on two candidate lines, so a fix one line off
    # the developer's edit collides while a fix ten lines away that reimplements
    # the same idea does not. The overlap and its metrics are recorded for the
    # audit that does make the judgement.
    collisions, metrics = gold_overlap(patch, gold_spans)
    record["gold_overlap_lines"] = collisions
    rewards["gold_overlap"] = int(bool(collisions))
    rewards.update({f"gold_{k}": metrics[k] for k in METRIC_KEYS})
    return finish("executable_candidate")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--gold-spans", type=Path, required=True,
                        help="Developer-patch spans the candidate must NOT touch")
    parser.add_argument("--local-id", type=int, default=0,
                        help="Instance id, recorded in mechanical.json")
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--compile-timeout", type=int, default=2000)
    parser.add_argument("--poc-timeout", type=int, default=600)
    parser.add_argument("--test-timeout", type=int, default=2400)
    args = parser.parse_args()

    rewards = score(
        args.source,
        args.baseline,
        args.gold_spans,
        args.artifacts,
        args.compile_timeout,
        args.poc_timeout,
        args.local_id,
        args.test_timeout,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rewards, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
