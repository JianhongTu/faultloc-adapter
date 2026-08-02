"""Generate Harbor *repair* tasks over the frozen eval50 sample.

Stage 2, built on the localization stage's environment -- adapter.compose() is
called, not copied, so the two families share one agent/sidecar shape and a
drift between them cannot happen unnoticed.

The question is not "does a good report help an agent fix this" but "is the
developer's location the only place this defect can be fixed at all". The agent
is handed the developer patch and asked to repair the same defect somewhere
else. A candidate is mechanically accepted only if it builds, suppresses the
reproducer, leaves the project's own tests no redder than it found them, touches
production source only, and has ZERO anchored overlap with the developer-patch
spans. Accepted candidates then go to a dual-agent root-cause audit, which no
code here performs; this family produces the candidates the audit consumes.

ONE DATASET, TWO JOBS. Both generation agents are required to receive identical
task materials, and the cheapest way to guarantee that is for them to receive the
SAME materials:

    uv run faultloc-repair
    uv run harbor run -c configs/repair-codex.yaml  -p datasets/... --job-name repair-codex
    uv run harbor run -c configs/repair-claude.yaml -p datasets/... --job-name repair-claude

The generator identity lives in the job name, which is where it belongs: it is a
property of the run, not of the task.

WHAT THE AGENT GETS, and why each is deliberate:

  * NO REPRODUCER FILE. It can RUN the reproducer but never read its bytes. The
    headline failure mode this study must exclude is a patch that special-cases
    the exact input, and an agent that cannot see the input cannot write one.
    The prohibition in the prompt is a backstop for a property the environment
    already enforces.
  * A BUILD TOOL. The search is for whether a second fix EXISTS, so iteration is
    what the search is made of and a candidate that never compiled is not
    evidence of anything. build.sh carries the compile token; see
    COMPILE_TOKEN_HEADER for why that token is derived rather than secret.
  * A REGRESSION SUITE, for the instances that have one. Building and
    suppressing is not evidence of a repair -- the smoke produced three
    candidates that did both and one of them broke libxml2's own tests. Giving
    the agent the suite rather than holding it for the verifier changes what the
    study reports: "how many instances admit an alternative fix that SURVIVES
    the project's tests" rather than "what fraction of candidates regress".

NOT EVERY INSTANCE HAS A SUITE YET, and every instance ships the tool anyway.
data/testset/<id>/test.sh is hand-written; where none exists the generator seeds
a placeholder that always exits non-zero. The verifier reads that exit code
either way, so an unauthored instance rejects every candidate rather than
accepting them ungated -- a placeholder that exited 0 would make an instance
nobody has written tests for look like one whose tests all pass. task.toml
records `regression_tested` so the two halves are never pooled.

THE DEVELOPER PATCH REACHES THE AGENT THROUGH THE PROMPT, not through the
container -- the requirement is only that the agent *receive* it, and the prompt
is where it reads everything else.

See README.md for the shared design constraints; sidecar/server.py for the
endpoints and why the build tree is synced rather than shared.
"""

import base64
import hashlib
import json
import shutil
from pathlib import Path

from . import manifest as manifest_mod
from .adapter import (
    BASELINE_REPO_STEPS,
    DATASET_ROOT,
    DEFAULT_AGENT_IMAGE,
    DEFAULT_ALLOWED_HOSTS,
    STAGED_SENTINEL,
    compose,
)
from .anchoring import parse_diff_anchored

_TEMPLATE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = Path(__file__).resolve().parents[2]

POC_TIMEOUT_SEC = 120
# Slowest `arvo compile` measured across the frozen instances is well under this;
# it bounds a pathological build rather than a normal one.
COMPILE_TIMEOUT_SEC = 1800

# Tag written over the staged tree before the agent starts. Every captured patch
# is `git diff` against it, which is what makes committed, staged, unstaged and
# newly created files all land in one diff.
BASELINE_TAG = "harbor-baseline"

# What makes /compile verifier-only.
#
# The agent must not build. Withholding is structural everywhere else in this
# design -- the resource is not in its container -- but /compile cannot be: the
# agent needs `poc` allowlisted to run the reproducer, Harbor's allowlist is
# host-based and rejects ports outright ("not URLs, ports, or paths"), and both
# endpoints live on the one sidecar. A POST from the agent container reached
# /compile and started a real build.
#
# So the gate is a token the agent cannot have YET. Harbor uploads `tests/` inside
# verify() (harbor/verifier/verifier.py:147), after the agent phase has ended, so
# /tests does not exist while the agent runs. The sidecar learns the same value
# from its compose environment, which no other container can read. Structural
# again, just in time rather than in space.
#
# Derived from the instance rather than random, so regeneration is byte-stable.
# The value is not a secret -- this file is public -- but the agent has neither
# the repo nor the file, and nothing in its prompt mentions /compile.
COMPILE_TOKEN_HEADER = "X-Compile-Token"
COMPILE_TOKEN_FILE = "compile_token"


def compile_token(local_id: int) -> str:
    return hashlib.sha256(f"faultloc-compile-{local_id}".encode()).hexdigest()


def sidecar_env(manifest: dict) -> dict[str, str]:
    """The sidecar's configuration for a repair trial.

    PROJECT is what turns /compile on: the sidecar refuses to serve it without
    one. COMPILE_TOKEN lives in the container environment, so `main` cannot read
    it -- it can only reach :8080.
    """
    return {
        "PROJECT": manifest["project"],
        "TIMEOUT": str(POC_TIMEOUT_SEC),
        "COMPILE_TIMEOUT": str(COMPILE_TIMEOUT_SEC),
        "COMPILE_TOKEN": compile_token(manifest["local_id"]),
    }








def _runs(lines: list[int]) -> list[tuple[int, int]]:
    """Split sorted line numbers into (start, end-exclusive) contiguous runs."""
    runs: list[tuple[int, int]] = []
    for line in lines:
        if runs and line == runs[-1][1]:
            runs[-1] = (runs[-1][0], line + 1)
        else:
            runs.append((line, line + 1))
    return runs


def gold_report(manifest: dict) -> dict:
    """Root-cause report derived from the developer patch, one location per edit.

    Deliberately carries locations only. A `cause` written by summarizing the
    patch would leak the fix, and this family does not need one: the agent is
    told WHERE the developer fixed it so it can stay away, not why.

    One location per CONTIGUOUS RUN, not per hunk. A hunk is a `@@` block, and git
    merges edits into one block whenever they are within a few context lines of
    each other, so `hunk.lines` is a set with gaps far more often than not --
    221 of the shipped set's hunks, over 116 of its 500 instances. Spanning
    lines[0]..lines[-1] treated that set as a range and claimed every untouched
    line between the extremes: 5507 reported lines against 4263 real ones, worst
    case 13 real lines reported as a 76-line window. That feeds the agent's prompt
    and, through report_spans(), the spans the verifier rejects a candidate for
    overlapping -- so an over-wide span puts a candidate off-limits from lines the
    developer never touched.
    """
    locations = []
    for hunk in parse_diff_anchored(manifest["gt_diff"]):
        for start, end in _runs(sorted(hunk.lines)):
            locations.append(
                {
                    "file": hunk.file,
                    "line_start": start,
                    # Exclusive, matching the FL span convention.
                    "line_end": end,
                }
            )
    return {
        "report_id": f"gold-{manifest['local_id']}",
        "instance_id": manifest["local_id"],
        "locations": locations,
        "cause": "",
    }








def report_spans(report: dict) -> list[dict]:
    """The report's locations in FL span form, for the verifier."""
    return [
        {"file": loc["file"], "line_start": loc["line_start"], "line_end": loc["line_end"]}
        for loc in report["locations"]
    ]




# The 10% subset, frozen by scripts/select_repair_eval50.py before any patch was
# generated. Read rather than re-derived: re-deriving would let a change to the
# sampler silently move the sample under results already collected on it.
SAMPLE_FILE = _REPO_ROOT / "data" / "alternative-patch-eval50.json"

# Per-instance test scripts, one directory each. Hand-authored; the adapter only
# seeds a placeholder where none exists yet.
TESTSET_DIR = _REPO_ROOT / "data" / "testset"

CONDITION = "repair"
DATASET_NAME = "flbench-repair-eval50"

# The agent builds AND runs the project's test suite here, so its budget has to
# cover several `arvo compile` cycles (repair.COMPILE_TIMEOUT_SEC bounds a single
# build at 1800s) plus a suite run per iteration. Raised from 3600 when the suite
# became a tool rather than a verifier-only check: the smoke's agents already
# spent 22-30 builds in an hour with no tests to run.
AGENT_TIMEOUT_SEC = 7200.0

# Bounds one suite run in the sidecar. The per-instance cap and
# `n_concurrent_trials` are deliberately NOT set from this number -- they trade
# against each other on a fixed core count and are chosen together once
# scripts/build_testset.py has measured the distribution across all 50.
TEST_TIMEOUT_SEC = 1800

# The verifier compiles, reproduces, and then runs the suite from a clean tree.
# One value, not two: /test runs for every instance now -- a placeholder suite is
# still a suite run -- so a budget that assumed the gate was optional would cut a
# slow-but-clean run short and report it as an infrastructure error.
VERIFIER_TIMEOUT_SEC = 5400.0

BUILD_TOOL = "/usr/local/bin/build.sh"
TEST_TOOL = "/usr/local/bin/run_tests.sh"
# Where the frozen suite lives inside the SIDECAR. Never in the agent's tree:
# the tree is what the suite tests.
SIDECAR_TEST_SCRIPT = "/usr/local/bin/frozen_test.sh"
# Optional, per instance: whatever the suite needs installed before it can build.
# It runs at STAGING time rather than inside the suite because installing a build
# dependency needs the network, and a network failure inside the suite is
# indistinguishable from a regression -- the verifier reads one exit code. The
# staging shell runs under `set -eu`, so a failure here kills the trial instead.
SIDECAR_SETUP_SCRIPT = "/usr/local/bin/frozen_setup.sh"


def _build_tool(local_id: int) -> str:
    """`build.sh`, the counterpart to the image's run_poc.sh.

    Carries the compile token inline. The token is derived from the instance and
    this file is public, so it was never a secret -- what it gates is WHICH agent
    can build, and repair's agent still cannot because nothing puts the token in
    its container.
    """
    return f"""#!/bin/sh
# Compile the current /workspace/src in the sidecar and print the compiler output.
# Exit 0 means it built. Run this before run_poc.sh: the reproducer always tests
# whatever was built last, so without a build you are testing the original binary.
exec python3 - "$@" <<'PY'
import json, sys, urllib.request
req = urllib.request.Request("http://poc:8080/compile", method="POST", data=b"")
req.add_header("{COMPILE_TOKEN_HEADER}", "{compile_token(local_id)}")
try:
    with urllib.request.urlopen(req, timeout={int(COMPILE_TIMEOUT_SEC) + 100}) as resp:
        result = json.loads(resp.read())
except Exception as e:
    sys.stderr.write("build request failed: %s\\n" % e)
    sys.exit(2)
if result.get("sync_failed"):
    sys.stderr.write("the sidecar could not sync your source tree\\n")
    sys.exit(2)
sys.stdout.write(result.get("output", ""))
sys.exit(result.get("exit_code", 1))
PY
"""


def _test_tool(local_id: int) -> str:
    """`run_tests.sh`, the third tool. Prints the suite's output and its exit code.

    Deliberately computes nothing. The suite script itself decides whether the
    result matches the unpatched tree -- that is its contract, and it is the
    same number the verifier reads -- so the agent sees exactly what the
    verifier will see, rather than a second opinion computed under different
    rules inside its own container.
    """
    return f"""#!/bin/sh
# Run the project's own test suite against your current /workspace/src.
# EXIT 0 MEANS THE RESULT MATCHES THE UNPATCHED TREE -- not that every test
# passed. Some tests already fail; those are not yours and are not counted
# against you. This is the same exit code the verifier reads.
# Slower than build.sh; the tree is rebuilt from scratch for each run.
exec python3 - "$@" <<'PY'
import json, sys, urllib.request
req = urllib.request.Request("http://poc:8080/test", method="POST", data=b"")
req.add_header("{COMPILE_TOKEN_HEADER}", "{compile_token(local_id)}")
try:
    with urllib.request.urlopen(req, timeout={TEST_TIMEOUT_SEC + 300}) as resp:
        result = json.loads(resp.read())
except Exception as e:
    sys.stderr.write("test request failed: %s\\n" % e)
    sys.exit(2)
if result.get("sync_failed"):
    sys.stderr.write("the sidecar could not sync your source tree\\n")
    sys.exit(2)
sys.stdout.write(result.get("output", ""))
sys.exit(result.get("exit_code", 1))
PY
"""


PLACEHOLDER_EXIT = 66


def placeholder(project: str) -> str:
    """The test script an instance gets until a human writes the real one.

    Fails, always, and says why. There is no way to derive a working suite from
    the tree: ARVO images carry the source and a fuzzer build, never a
    configured test build, so the tests exist but nothing can run them until
    someone writes the configure-and-build recipe for this project. Two
    independent efforts over this same corpus -- OSS-Fuzz's Chronos
    `run_tests.sh` and e2e-cyber-bench's `test.sh` -- both hand-write it per
    project, and neither attempts detection.

    Non-zero rather than a no-op on purpose. A placeholder that exited 0 would
    make an unauthored instance look like a project whose tests all pass, which
    is the one reading that must never happen -- launching would then produce
    results nobody could tell apart from real ones.
    """
    return f"""#!/bin/bash
# PLACEHOLDER -- replace this with {project}'s test suite.
#
# CONTRACT: exit 0 when the result MATCHES the unpatched tree. NOT "all tests
# pass" -- some already fail, and demanding green would reject the developer's
# own patch. The verifier reads this exit code and nothing else.
#
# Configure, build and run the tests, then compare. Reconfiguring is fine: the
# fuzzer binary lives in /out and this runs after the reproducer is checked.
#
#   cd /src/{project}
#   ./autogen.sh && ./configure && make -j"$(nproc)"
#   make -k check > /tmp/out 2>&1 || true
#   # Name what must keep passing, rather than counting what fails: a test that
#   # stops running disappears from the output, so a failure count still says 0.
#   for t in parser_ok entities_ok xpath_ok; do grep -q "^PASS: $t\\b" /tmp/out || exit 1; done
#
# Check it against the unpatched tree before relying on it: it must exit 0
# there, or every candidate on this instance is rejected.
echo "FAULTLOC: no test suite has been authored for {project}." >&2
echo "FAULTLOC: see data/testset/ -- this task must not be launched yet." >&2
exit {PLACEHOLDER_EXIT}
"""


def write_suite(testset_dir: Path, local_id: int, text: str) -> Path:
    path = testset_dir / str(local_id) / "test.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def resolve_suite(manifest: dict, testset_dir: Path) -> dict:
    """This instance's test script: the authored one, or a placeholder.

    THE SCRIPT IS SELF-CONTAINED. It encodes its own pass condition -- what was
    passing must still pass, what was already failing is not checked -- and exit
    0 means no regression. Nothing outside it records which tests exist or how
    they behaved: a second copy of that knowledge is a second thing to keep in
    step, and the script is the one that runs.

    A script is "authored" when it is not byte-identical to the placeholder we
    would have written. That is the whole test: no status field to set, no
    record to keep in sync with the file it describes.
    """
    stub = placeholder(manifest["project"])
    base = testset_dir / str(manifest["local_id"])
    path = base / "test.sh"
    setup = base / "setup.sh"
    resolved = {"path": path, "setup": setup.read_text() if setup.exists() else ""}
    if path.exists() and path.read_text() != stub:
        return {**resolved, "script": path.read_text(), "authored": True}
    if not path.exists():
        write_suite(testset_dir, manifest["local_id"], stub)
    return {**resolved, "script": stub, "authored": False}


def _sidecar_setup(script: str, setup: str = "") -> str:
    """Stage the test script into the sidecar, base64-encoded.

    Same reason build.sh is encoded: this string is embedded in a compose block
    scalar and then `$`-escaped, and the payload is an arbitrary shell script
    full of quotes, `$` and heredocs of its own.

    An instance's setup.sh, where it has one, is staged and RUN here -- see
    SIDECAR_SETUP_SCRIPT for why it does not belong inside the suite.
    """
    lines = []
    if setup:
        blob = base64.b64encode(setup.encode()).decode()
        lines += [
            f"echo {blob} | base64 -d > {SIDECAR_SETUP_SCRIPT}",
            f"chmod +x {SIDECAR_SETUP_SCRIPT}",
            SIDECAR_SETUP_SCRIPT,
        ]
    blob = base64.b64encode(script.encode()).decode()
    lines += [
        f"echo {blob} | base64 -d > {SIDECAR_TEST_SCRIPT}",
        f"chmod +x {SIDECAR_TEST_SCRIPT}",
    ]
    return "\n".join(lines)


def _staging(manifest: dict, with_tests: bool) -> str:
    """Agent-side staging: repair's, minus the reproducer, plus the tools.

    build.sh is delivered base64-encoded rather than through a heredoc. This
    string is embedded in a compose block scalar and then `$`-escaped, and the
    script it carries is itself a heredoc containing `$@` and quotes -- encoding
    it removes every one of those interactions at the cost of one `base64 -d`.
    """
    local_id = manifest["local_id"]
    build = base64.b64encode(_build_tool(local_id).encode()).decode()
    lines = [
        "set -eu",
        "mkdir -p /workspace /logs/artifacts",
        "git config --global --add safe.directory '*'",
        *BASELINE_REPO_STEPS,
        f"git tag {BASELINE_TAG}",
        # Deliberately NO `curl .../poc-file`. run_poc.sh still reaches the
        # sidecar, so the reproducer can be RUN but never read.
        f"echo {build} | base64 -d > {BUILD_TOOL}",
        f"chmod +x {BUILD_TOOL}",
    ]
    if with_tests:
        # The TOOL is staged here; the SUITE is not. What this container gets is a
        # client that POSTs to the sidecar -- the script the sidecar runs never
        # enters the agent's filesystem, so there is no copy of it for the agent
        # to weaken.
        tool = base64.b64encode(_test_tool(local_id).encode()).decode()
        lines += [
            f"echo {tool} | base64 -d > {TEST_TOOL}",
            f"chmod +x {TEST_TOOL}",
        ]
    lines += [
        f"touch {STAGED_SENTINEL}",
        "exec sleep infinity",
    ]
    return "\n".join(lines)


def _spans_section(report: dict) -> str:
    return "\n".join(
        f"- `{loc['file']}` lines {loc['line_start']}-{loc['line_end'] - 1}"
        for loc in report["locations"]
    )



_PLACEHOLDER_NOTICE = (
    "**No test suite has been written for this project yet.** `run_tests.sh` is "
    "a placeholder that always fails, and the verifier reads its exit code, so "
    "no answer to this task can be accepted as it stands. This task was not "
    "ready to be run. Do the work anyway and describe it in your summary \u2014 but "
    "nothing you write will pass the test gate, and that is not your doing."
)

_SUITE_NOTICE = (
    "Run it before you change anything and again after. **Exit 0 means you have "
    "broken nothing** \u2014 not that every test passes. Some of this project's tests "
    "already fail, and the script knows which; it checks only that what was "
    "passing still passes. The verifier runs the same script and reads the same "
    "exit code, so what you see is what it sees."
)


def _tests_section(suite: dict) -> str:
    """The `run_tests.sh` half of the Available Tools section.

    Deliberately carries no list of already-failing tests. The script encodes
    its own pass condition, so a list here would be a second copy of that
    knowledge, maintained by hand, free to disagree with the script that
    actually decides.
    """
    lines = [
        "- `run_tests.sh` \u2014 run the project's own test suite against your current "
        "working tree. Slower than `build.sh`; the tree is rebuilt from scratch "
        "each time.",
        "",
        "### The project's own test suite",
        "",
        _SUITE_NOTICE if suite["authored"] else _PLACEHOLDER_NOTICE,
        "",
        "If you cannot find a change that leaves the suite as you found it, say "
        "so \u2014 that is a real result, and a far better one than a patch you know "
        "regresses.",
    ]
    return "\n".join(lines) + "\n"


def _instruction(manifest: dict, report: dict, suite: dict) -> str:
    template = (_TEMPLATE_DIR / "repair_prompt.md").read_text()
    text = (
        template.replace("{{DEVELOPER_SPANS}}", _spans_section(report))
        .replace("{{TESTS_SECTION}}", _tests_section(suite))
        .replace("{{CRASH_TYPE}}", manifest["crash_type"] or "unknown")
        .replace("{{SANITIZER}}", manifest["sanitizer"] or "unknown")
        .replace("{{FUZZ_TARGET}}", manifest["fuzz_target"] or "unknown")
        .replace("{{SANITIZER_REPORT}}", manifest["crash_output"])
    )
    text = "\n".join(line.rstrip() for line in text.splitlines()) + "\n"
    # The patch is substituted AFTER that normalization, never before. A diff's
    # context lines begin with a single space, so a blank context line IS that
    # space -- rstrip deletes it, and the agent is then shown a patch whose lines
    # do not line up with the ones it is being told not to touch. It mangled 40 of
    # the 50 sampled instances before this was split out.
    return text.replace("{{DEVELOPER_PATCH}}", manifest["gt_diff"].rstrip("\n"))


class RepairAdapter:
    def __init__(
        self,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        manifest_dir: Path | None = None,
        sample_file: Path | None = None,
        testset_dir: Path | None = None,
        agent_image: str = DEFAULT_AGENT_IMAGE,
        allowed_hosts: list[str] | None = None,
    ):
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.manifest_dir = Path(manifest_dir or _REPO_ROOT / "manifests")
        self.sample_file = Path(sample_file or SAMPLE_FILE)
        self.testset_dir = Path(testset_dir or TESTSET_DIR)
        self.agent_image = agent_image
        self.allowed_hosts = list(allowed_hosts or DEFAULT_ALLOWED_HOSTS)
        self.output_dir = DATASET_ROOT / DATASET_NAME

    def _manifests(self) -> list[dict]:
        sample = json.loads(self.sample_file.read_text())
        wanted = [str(i) for i in sample["instance_ids"]]
        if self.task_ids:
            asked = {str(t) for t in self.task_ids}
            outside = asked - set(wanted)
            # A smoke run picks its instances with --task-ids, and picking one from
            # outside the frozen sample is how a 51st instance quietly joins it.
            if outside:
                raise ValueError(
                    f"not in the frozen sample {self.sample_file.name}: "
                    f"{sorted(outside)}. The sample is fixed; see section 3."
                )
            wanted = [w for w in wanted if w in asked]
        if self.limit is not None:
            wanted = wanted[: self.limit]

        manifests = []
        for local_id in wanted:
            path = self.manifest_dir / f"{local_id}.json"
            if not path.exists():
                raise FileNotFoundError(
                    f"no manifest for sampled instance {local_id}: {path}. "
                    f"Extract the shipped set with `tar -xzf data/eval500.tar.gz`."
                )
            manifests.append(manifest_mod.load(path))
        return manifests

    def run(self) -> None:
        manifests = self._manifests()
        written = 0
        skipped: list[int] = []
        suites: list[dict] = []
        errors: list[tuple[int, str]] = []
        for manifest in manifests:
            try:
                if (result := self._write_task(manifest)) is not None:
                    written += 1
                    suites.append(result)
                else:
                    skipped.append(manifest["local_id"])
            except Exception as e:
                errors.append((manifest["local_id"], f"{type(e).__name__}: {e}"))
                print(f"{manifest['local_id']}: FAILED {type(e).__name__}: {e}")

        self._warn_skipped(skipped)
        self._report_suites(suites, written)


        if errors:
            print(f"\nwrote {written} task(s); {len(errors)} instance(s) failed:")
            for local_id, msg in errors:
                print(f"  {local_id}  {msg}")
            raise RuntimeError(
                f"{len(errors)} of {len(manifests)} instance(s) failed; the dataset "
                f"covers the rest. Re-run with --task-ids for the failures."
            )
        print(f"\nwrote {written} of {len(manifests)} task(s) to {self.output_dir}")

    def _warn_skipped(self, skipped: list[int]) -> None:
        """Say how many task directories were left as they were, and what that hides.

        A task freezes its inputs: the test script is base64-embedded into the
        compose file, and so are the sidecar, the tools, the prompt and the
        verifier. Editing any of them on disk changes nothing until the task is
        rewritten -- so the common move, "write the suite, regenerate", is a
        silent no-op without --overwrite. Counting the skips is what keeps that
        from looking like a clean run.
        """
        if not skipped:
            return
        print(f"\nskipped {len(skipped)} existing task(s); they were NOT updated:")
        print(f"  {', '.join(str(i) for i in skipped[:10])}"
              + (f", ...and {len(skipped) - 10} more" if len(skipped) > 10 else ""))
        print("  A task freezes its test script, prompt, sidecar and verifier at the")
        print("  moment it was written. If you have edited any of those -- most")
        print("  likely data/testset/<id>/test.sh -- re-run with --overwrite or the")
        print("  change does not reach the task.")

    def _report_suites(self, suites: list[dict], written: int) -> None:
        """Say how much of the sample has a real test suite, and stop short of nothing.

        Printed on every run. An unauthored instance ships a placeholder that
        always fails, so the state is impossible to mistake for "the tests pass"
        -- but it is equally impossible to notice from inside a task directory,
        which is why the count is stated here.
        """
        authored = [s for s in suites if s["authored"]]
        stub = [s for s in suites if not s["authored"]]
        with_setup = [s for s in authored if s["setup"]]
        print(f"\ntest suites: {len(authored)} authored, "
              f"{len(stub)} PLACEHOLDER, of {written} generated task(s)")
        if with_setup:
            print(f"  {len(with_setup)} carry a setup.sh the sidecar runs before the "
                  "server starts")
        if not stub:
            return
        print(
            f"\n  DO NOT LAUNCH. {len(stub)} instance(s) have no test suite. Each got a"
            "\n  placeholder at data/testset/<id>/test.sh that always exits non-zero."
            "\n"
            "\n  ARVO images carry the source and a fuzzer build, never a configured"
            "\n  test build, so the tests are present but nothing can run them until"
            "\n  the configure-and-build recipe is written per project -- by hand."
            "\n  Detection does not work here and is not attempted."
            "\n"
            "\n  For each: write the script so it exits 0 when nothing has regressed,"
            "\n  check it against the unpatched tree, and regenerate."
        )

    def _write_task(self, manifest: dict) -> bool | None:
        """Write one task. Returns whether it carries a suite, or None if skipped."""
        task_id = f"repair__{manifest['local_id']}"
        task_dir = self.output_dir / task_id
        if task_dir.exists() and not self.overwrite:
            return None

        # The developer patch is both the prompt's evidence and the verifier's
        # rejection boundary, so it is derived once, here, from the manifest. Two
        # derivations could disagree, and the direction that disagreement would
        # take is an agent told to avoid one region and scored against another.
        report = gold_report(manifest)
        if not report["locations"]:
            raise ValueError("developer patch yields no spans; manifest is corrupt")

        # Resolved BEFORE anything is written: a half-built task directory would
        # be skipped forever by the exists() check above.
        suite = resolve_suite(manifest, self.testset_dir)

        if task_dir.exists():
            shutil.rmtree(task_dir)
        for sub in ("environment", "solution", "tests/scorer"):
            (task_dir / sub).mkdir(parents=True, exist_ok=True)

        (task_dir / "task.toml").write_text(
            self._task_toml(manifest, task_id, suite["authored"])
        )
        (task_dir / "instruction.md").write_text(_instruction(manifest, report, suite))
        (task_dir / "environment" / "docker-compose.yaml").write_text(
            compose(
                manifest,
                _staging(manifest, with_tests=True),
                environment={
                    # Closes /poc-file over the network too, not just on disk.
                    "EVAL_CONFIG": "ablation2",
                    **sidecar_env(manifest),
                    "TEST_SCRIPT": SIDECAR_TEST_SCRIPT,
                    "TEST_TIMEOUT": str(TEST_TIMEOUT_SEC),
                },
                sidecar_setup=_sidecar_setup(suite["script"], suite["setup"]),
            )
        )
        self._write_solution(task_dir, manifest)
        self._write_tests(task_dir, manifest, report, suite)
        mark = "authored" if suite["authored"] else "PLACEHOLDER -- author it before launch"
        print(f"wrote {task_dir.name:<24} suite: {mark}")
        return suite

    def _task_toml(self, manifest: dict, task_id: str, regression_tested: bool) -> str:
        # Budgets, and why they are what they are -- kept here rather than in the
        # emitted file, which is a generated artifact per task:
        #   * `regression_tested` records whether this instance has an authored
        #     test script. The two halves of the sample are measured under
        #     different protocols and must be reported separately, never pooled.
        #   * agent 7200s: this agent builds and runs the project's tests, and a
        #     single `arvo compile` can take 1800s on its own.
        #   * verifier 5400s: compile, reproduce, then run the suite from a clean
        #     tree. The suite is bounded at TEST_TIMEOUT_SEC in the sidecar, so
        #     the verifier has to outlast it or a slow suite reads as an
        #     infrastructure error.
        hosts = ", ".join(f'"{h}"' for h in self.allowed_hosts)
        return f"""version = "1.0"

[task]
name = "flbench/{task_id}"
description = "Alternative repair for {manifest['project']}: {manifest['crash_type']}"
authors = []
keywords = ["program-repair", "security", "oss-fuzz", "arvo"]

[metadata]
difficulty = "hard"
category = "debugging"
local_id = {manifest['local_id']}
project = "{manifest['project']}"
sanitizer = "{manifest['sanitizer']}"
condition = "{CONDITION}"
regression_tested = {str(regression_tested).lower()}

[agent]
timeout_sec = {AGENT_TIMEOUT_SEC}
network_mode = "allowlist"
allowed_hosts = [{hosts}]

[verifier]
timeout_sec = {VERIFIER_TIMEOUT_SEC}
network_mode = "allowlist"
allowed_hosts = ["poc"]

[environment]
docker_image = "{self.agent_image}"
workdir = "/workspace/src"
network_mode = "public"
build_timeout_sec = 1800.0
cpus = 4
memory_mb = 8192
"""

    def _write_solution(self, task_dir: Path, manifest: dict) -> None:
        """Oracle: apply the developer patch. The per-instance precondition check.

        Not a solution. It rides the same path a candidate takes -- capture,
        build, reproduce, suite -- so that this instance is known to build and to
        have a suppressible reproducer before any agent time is spent on it. Read
        `compiled` and `poc_suppressed`, not `reward`: the patch overlaps its own
        spans by construction, which is recorded in `gold_overlap` and is not a
        rejection.
        """
        solve = task_dir / "solution" / "solve.sh"
        solve.write_text(
            f"""#!/bin/bash
# Precondition check, not a solution. Read `compiled`/`poc_suppressed`, not `reward`.
set -eu
mkdir -p /logs/artifacts
cat > /tmp/gt.diff <<'PATCH_EOF'
{manifest['gt_diff']}
PATCH_EOF
cd /workspace/src
patch -p1 < /tmp/gt.diff
cat > /logs/artifacts/summary.json <<'SUMMARY_EOF'
{{
  "root_cause": "Precondition check for instance {manifest['local_id']}: this run applies the accepted developer patch rather than searching for an alternative, so no root-cause analysis was performed.",
  "gold_patch_explanation": "The developer patch is applied verbatim; `compiled` and `poc_suppressed` in the reward file record whether it builds and suppresses the reproducer on this instance.",
  "alternative_fix_exists": true,
  "alternative_patch_explanation": "The developer patch itself, applied verbatim. Declared true because a patch IS in the tree: `alternative_fix_exists: false` is the agent's way of saying it left nothing to evaluate, and the verifier takes it at its word and stops -- which would skip the build and the reproducer, the two things this run exists to check.",
  "no_alternative_explanation": null
}}
SUMMARY_EOF
"""
        )
        solve.chmod(0o755)

    def _write_tests(
        self, task_dir: Path, manifest: dict, report: dict, suite: dict
    ) -> None:
        tests = task_dir / "tests"
        for name in ("__init__.py", "types.py", "ground_truth.py", "metrics.py"):
            shutil.copy(_TEMPLATE_DIR / "scorer" / name, tests / "scorer" / name)
        shutil.copy(_TEMPLATE_DIR / "anchoring.py", tests / "anchoring.py")
        shutil.copy(_TEMPLATE_DIR / "repair_score.py", tests / "repair_score.py")
        # The region the candidate must stay OUT of, derived from the developer
        # patch: a fix at those lines is a different expression of the same edit,
        # not a different localization.
        (tests / "gold_spans.json").write_text(
            json.dumps(report_spans(report), indent=2) + "\n"
        )
        (tests / COMPILE_TOKEN_FILE).write_text(compile_token(manifest["local_id"]) + "\n")

        test_sh = tests / "test.sh"
        test_sh.write_text(
            "#!/bin/bash\n"
            "set -u\n"
            "mkdir -p /logs/verifier\n"
            "rm -f /logs/verifier/reward.json /logs/verifier/reward.txt\n"
            "python3 /tests/repair_score.py \\\n"
            "  --source /workspace/src \\\n"
            f"  --baseline {BASELINE_TAG} \\\n"
            "  --gold-spans /tests/gold_spans.json \\\n"
            f"  --local-id {manifest['local_id']} \\\n"
            f"  --test-timeout {TEST_TIMEOUT_SEC + 300} \\\n"
            "  --artifacts /logs/artifacts \\\n"
            "  --out /logs/verifier/reward.json\n"
            "cat /logs/verifier/reward.json\n"
        )
        test_sh.chmod(0o755)
