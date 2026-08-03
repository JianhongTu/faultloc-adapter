"""Generate Harbor tasks from frozen FLBench instance manifests.

One task per (instance, config), as two compose services:

  * `main` runs the agent in `faultloc-agent:v1` and holds no ARVO content of
    its own -- no source, no PoC, no reproducer;
  * `poc` runs the archival `n132/arvo:<id>-vul`, stages the source onto a
    shared tmpfs volume, and serves the reproducer over four fixed HTTP
    endpoints.

Withholding is therefore structural: a config's resources are absent from the
agent's container unless staging puts them there, which is what makes the
ablations enforceable. The verifier shares the agent's container -- the scorer
is stdlib-only and that image already ships python3 -- and Harbor uploads
`tests/` only at verify time, so the ground truth never coexists with a live
agent.

See README.md for the design, the gates, and the msan/ASLR note.
"""

import json
import shutil
from pathlib import Path

from . import manifest as manifest_mod
from .anchoring import parse_diff_flbench

_TEMPLATE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Container the agent runs in; override with --agent-image. Pin it by digest before
# any calibration or main experiment -- a mutable tag lets the harness drift between runs.
DEFAULT_AGENT_IMAGE = "faultloc-agent:v1"

# Model API endpoints reachable during agent.run(). The environment baseline is
# no-network, so nothing else is: the reference relied on command-level denylists
# its own README calls bypassable, which let an agent fetch the upstream fix.
# `poc` is the sidecar's compose hostname: run_poc.sh must still reach it while the
# agent is policed. The rest are the model endpoints the supported agents dial --
# api.openai.com for codex on an API key, chatgpt.com and auth.openai.com for codex
# on a ChatGPT subscription (auth.json), api.anthropic.com for claude-code, and the
# self-hosted vLLM endpoint qwen-coder dials through OPENAI_BASE_URL. A host missing
# here fails at the network layer mid-run. Other endpoints need --allowed-hosts.
#
# The vLLM endpoint is listed BY HOSTNAME AND BY ADDRESS because only the address
# works today. Harbor's allowlist sidecar captures the agent's network namespace and
# its nftables ruleset rejects all non-TCP egress, which includes UDP DNS: inside
# that namespace an external name does not resolve (`getaddrinfo EAI_AGAIN`), and the
# agent never reaches the model. Container names still resolve -- Docker answers
# those from its own table without an upstream query -- which is why `poc` is
# unaffected. Docker 29 does not exhibit this; the evaluation box runs 25.0.14.
# Keep the hostname so the entry survives a Docker upgrade, and keep the address so
# the run works before one. Both are ephemeral: an EC2 replacement changes them.
# TWO self-hosted endpoints are listed, and a run reaches whichever OPENAI_BASE_URL
# names -- the allowlist only decides what is reachable, never what is dialled.
# Both are here so a batch can move between them without regenerating datasets,
# which would otherwise change every task.toml mid-experiment.
#
# The NRP entry widens this more than it looks: 137.164.28.180 is NRP's shared
# ingress, so allowlisting it also reaches the S3 pools and everything else behind
# that address. Accepted here because both agents are cooperative and the archival
# fix is already withheld structurally (STRIP_ARCHIVAL_GIT), not because the
# address is narrow.
#
# api.z.ai is Zhipu's OpenAI-compatible endpoint, dialled through OPENAI_BASE_URL
# the same way the self-hosted ones are. It is fronted by a CDN, so the two
# addresses here are what it resolved to when this was written, not a fixed pair;
# re-resolve before a batch if the run cannot reach the model.
DEFAULT_ALLOWED_HOSTS = (
    "poc",
    "api.anthropic.com",
    "api.openai.com",
    "chatgpt.com",
    "auth.openai.com",
    "ec2-16-56-22-55.compute-1.amazonaws.com",
    "172.31.96.73",
    "ellm.nrp-nautilus.io",
    "137.164.28.180",
    "api.z.ai",
    "128.14.14.140",
    "128.14.14.141",
)

POC_TIMEOUT_SEC = 120

# The locked instance set these datasets are built over (data/eval900_instance_list.json).
# This is FLBench's own 900-instance eval list, verbatim, so a result here is
# directly comparable with a published FLBench row. It replaced a balanced 500-
# instance subset (scripts/select_eval500.py, kept for its category analysis): that
# set was flatter across fault types, but being flatter than FLBench is exactly what
# makes a number not comparable to FLBench. The skew is inherited on purpose.
INSTANCE_SET = "eval900"

DATASET_ROOT = _REPO_ROOT / "datasets"

# One dataset directory per condition, named for the experiment and for what the
# agent could see. `-main` is the full-information condition; each `-ablation-*`
# names the resource it withholds rather than a serial number, so a directory
# says what it is without opening a task. Generation derives the directory from
# the config, which is what stops a run filing its tasks under another
# condition's name -- the failure mode a caller-supplied path invites.
#
# Keep in step with CONFIGS: a config with no entry here cannot be generated
# without an explicit --output-dir.
DATASET_NAMES = {
    "main": f"flbench-diagnosis-{INSTANCE_SET}-main",
    "ablation1": f"flbench-diagnosis-{INSTANCE_SET}-ablation-static-only",
    "ablation2": f"flbench-diagnosis-{INSTANCE_SET}-ablation-no-poc-file",
    "sanity": f"flbench-diagnosis-{INSTANCE_SET}-sanity-no-source",
}

# Written once staging finishes; the main service's healthcheck gates on it.
STAGED_SENTINEL = "/workspace/.staged"

# Sidecar staging: drop the archival history before the agent can read it.
#
# `git clean -fdx` restores the working tree but keeps the object store, so the
# copied checkout still carries every branch, tag and remote-tracking ref the
# image was built with. Measured on the locked set: all 13 locally cached images
# had an `origin/HEAD` ahead of the vulnerable revision, 12436 commits ahead on
# 42470093, where `git log --all` shows the upstream fix and its diff -- the
# ground truth both task families are scored against. Reading history is ordinary
# fault localization, so this leaks to a cooperative agent, not just an
# adversarial one.
#
# `find` rather than `rm -rf /shared/.git` because a submodule keeps its own
# history in a nested `.git` (a directory, or a file pointing at one); none of the
# 13 cached images has one, but the other 487 are not inspectable without pulling
# them, and the wider form costs nothing. It also frees the larger part of the
# tmpfs volume: .git ran 2x-20x the working tree (aom: 394M .git, 20M tree).
STRIP_ARCHIVAL_GIT = "find /shared -name .git -prune -exec rm -rf {} +"

# Agent staging: give the tree back a repository with no past.
#
# Stripping alone would leave the agent without `git status`/`git diff`, which the
# reference hands it and agent CLIs probe for. One commit over the staged tree is
# all that is needed, and for repair it is also the patch baseline (BASELINE_TAG).
# Cheap: 206ms on harfbuzz, and the fresh object store is 7M against 107M.
BASELINE_REPO_STEPS = (
    "cd /workspace/src",
    "git init -q",
    # -f because the baseline is a SNAPSHOT, not a repository state. A plain
    # `git add -A` honours .gitignore, and the archival repo tracked files its own
    # .gitignore matches -- 5 of the 13 cached images, including .cpp, .cil and
    # CMakeLists.txt. Under the archival history those files were already in HEAD,
    # so edits to them showed up; against a fresh index they would be invisible to
    # `git diff harbor-baseline` and a valid repair could be captured as no patch
    # at all. Nothing ignorable survives here anyway: clean -fdx ran first.
    "git add -f -A",
    "git -c user.email=harbor@local -c user.name=harbor "
    "commit -q --allow-empty -m 'harbor baseline'",
)

# Which resources each benchmark config exposes, mirroring the reference
# entrypoint (src/flbench/eval/image/entrypoint.sh).
CONFIGS = {
    "main": {"source": True, "poc_file": True, "report": True, "run_tool": True},
    "ablation1": {"source": True, "poc_file": True, "report": False, "run_tool": False},
    "ablation2": {"source": True, "poc_file": False, "report": True, "run_tool": True},
    "sanity": {"source": False, "poc_file": True, "report": True, "run_tool": True},
}

# All four are generated by default. The split agent/sidecar design is what makes the
# ablations enforceable: withholding is structural, not prompt-only. ablation2 needs the
# PoC runnable but not readable, which a single container cannot express -- here the
# reproducer lives only in the sidecar and the agent reaches it over HTTP, so the sidecar
# can serve /poc while refusing /poc-file. Verified by scripts/config_boundaries.py.
DEFAULT_CONFIGS = tuple(CONFIGS)

_RUN_POC = f"""#!/bin/sh
# Run the proof-of-concept crash reproducer.
# Exit 0 means the PoC did NOT crash; non-zero means it still crashes.
timeout {POC_TIMEOUT_SEC} arvo 2>&1
"""


def _staging_command(caps: dict) -> str:
    """Shell run as the agent container's command, before the agent starts.

    Withholding is structural here: the agent image contains no source, no PoC
    and no reproducer, so a resource is absent unless this script fetches it.
    Source arrives on the shared volume only when the `poc` sidecar stages it.
    """
    steps = [
        "set -eu",
        "mkdir -p /workspace /logs/artifacts",
        # The reference does this before handing over (entrypoint.sh:211); the tree
        # is staged by another container, so git would otherwise refuse it.
        "git config --global --add safe.directory '*'",
    ]
    if caps["source"]:
        # The sidecar stripped the archival .git (STRIP_ARCHIVAL_GIT); rebuild a
        # repository over the staged snapshot so `git status` and `git diff` work.
        steps.extend(BASELINE_REPO_STEPS)
    if caps["poc_file"]:
        steps.append("curl -sf http://poc:8080/poc-file -o /workspace/poc")
    if not caps["run_tool"]:
        # Belt and braces: the sidecar also refuses /poc for this config.
        steps.append("rm -f /usr/local/bin/run_poc.sh")
    # Signal completion last: `docker compose up --wait` returns when the
    # container is *running*, not when this script finishes.
    steps.append(f"touch {STAGED_SENTINEL}")
    steps.append("exec sleep infinity")
    return "\n".join(steps)


def _instruction(manifest: dict, caps: dict) -> str:
    """Render the agent-facing prompt.

    Same template and section logic as the reference, except the sanitizer
    report is the one captured at freeze time rather than produced by running
    the PoC during task setup.
    """
    template = (_TEMPLATE_DIR / "prompt.md").read_text()

    resources = []
    if caps["source"]:
        resources.append("- The **vulnerable source code** at `/workspace/src`")
    if caps["poc_file"]:
        resources.append(
            "- A **PoC input binary** at `/workspace/poc` that you may inspect"
            + ("" if caps["run_tool"] else " as a file")
        )
    if caps["report"]:
        resources.append("- A **pre-run sanitizer report** (see below)")
    else:
        resources.append("- **Vulnerability metadata** (see below)")
    if caps["run_tool"]:
        resources.append("- `run_poc.sh` to re-execute the PoC and get a fresh sanitizer report")

    source_section = (
        "## Source Code\n\n"
        "The vulnerable source code is at `/workspace/src`. Explore it to understand "
        "the codebase."
        if caps["source"]
        else ""
    )

    metadata = (
        "**Vulnerability metadata:**\n"
        f"- Crash type: {manifest['crash_type'] or 'unknown'}\n"
        f"- Sanitizer: {manifest['sanitizer'] or 'unknown'}\n"
        f"- Fuzz target: {manifest['fuzz_target'] or 'unknown'}"
    )
    if caps["poc_file"]:
        poc_section = (
            "## Proof-of-Concept Input\n\n"
            "A fuzzer input (PoC) that triggers the vulnerability is available at "
            "`/workspace/poc`.\nYou may inspect it as a binary file to understand what "
            f"input patterns exercise the bug."
            + (f"\n\n{metadata}" if caps["report"] else "")
        )
    else:
        poc_section = ""

    if caps["report"]:
        rerun = (
            "\nYou can re-run it at any time using `run_poc.sh` to get a fresh report."
            if caps["run_tool"]
            else ""
        )
        sanitizer_section = (
            "## Sanitizer Report\n\n"
            "The following report was produced by running the PoC against the vulnerable "
            f"binary.{rerun}\n\n```\n{manifest['crash_output']}\n```"
        )
    else:
        sanitizer_section = f"## Vulnerability Metadata\n\n{metadata}"

    tools_section = (
        "- `run_poc.sh` — execute the PoC against the instrumented binary and capture "
        "a fresh sanitizer report"
        if caps["run_tool"]
        else "(none)"
    )

    text = (
        template.replace("{{INSTRUCTIONS}}", "\n".join(resources))
        .replace("{{SOURCE_SECTION}}", source_section)
        .replace("{{POC_SECTION}}", poc_section)
        .replace("{{SANITIZER_SECTION}}", sanitizer_section)
        .replace("{{DEDICATED_TOOLS_SECTION}}", tools_section)
    )
    if not caps["source"]:
        text += (
            "\n> **Important:** No source code is available in this mode. You must reason "
            "solely from\n> the sanitizer report and PoC. Do **not** attempt to fetch source "
            "code or documentation\n> from the internet.\n"
        )
    if not caps["run_tool"]:
        text += (
            "\n> **Note:** Try your best to identify the root cause. If after thorough "
            "investigation you cannot find any suspicious location, write an empty "
            "prediction (`[]`) and exit.\n"
        )
    return "\n".join(line.rstrip() for line in text.splitlines()) + "\n"


def _oracle_spans(gt_diff: str) -> list[dict]:
    """Exact ground-truth spans, used only by solution/solve.sh."""
    spans = []
    for hunk in parse_diff_flbench(gt_diff):
        for line in sorted(hunk.lines):
            spans.append({"file": hunk.file, "line_start": line, "line_end": line + 1})
    return spans


class FLBenchAdapter:
    def __init__(
        self,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        manifest_dir: Path | None = None,
        configs: list[str] | None = None,
        agent_image: str = DEFAULT_AGENT_IMAGE,
        allowed_hosts: list[str] | None = None,
    ):
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.manifest_dir = Path(manifest_dir or Path(__file__).resolve().parents[2] / "manifests")
        self.configs = configs or list(DEFAULT_CONFIGS)
        self.agent_image = agent_image
        self.allowed_hosts = list(allowed_hosts or DEFAULT_ALLOWED_HOSTS)

        unknown = set(self.configs) - set(CONFIGS)
        if unknown:
            raise ValueError(f"unknown config(s): {sorted(unknown)}")

    def dataset_dir(self, config: str) -> Path:
        """Where this config's tasks go.

        Derived, never caller-supplied. Task names carry the instance only, so a
        caller-chosen directory shared by two configs would collide on the same
        name and silently keep one condition.
        """
        return DATASET_ROOT / DATASET_NAMES[config]

    def _manifests(self) -> list[dict]:
        paths = sorted(self.manifest_dir.glob("*.json"))
        if self.task_ids:
            wanted = {str(t) for t in self.task_ids}
            paths = [p for p in paths if p.stem in wanted]
            missing = wanted - {p.stem for p in paths}
            if missing:
                raise FileNotFoundError(
                    f"no manifest in {self.manifest_dir} for: {sorted(missing)}. "
                    f"Extract the shipped set with `tar -xzf data/eval500.tar.gz`."
                )
        return [manifest_mod.load(p) for p in paths]

    def run(self) -> None:
        manifests = self._manifests()
        if not manifests:
            raise FileNotFoundError(f"no manifests found in {self.manifest_dir}")
        # --limit counts INSTANCES, not tasks. Counting tasks would stop mid-fan-out
        # and leave one config's dataset a task longer than another's -- datasets
        # that are supposed to differ only in what the agent sees would differ in
        # size too. The two readings coincide for a single-config run.
        if self.limit is not None:
            manifests = manifests[: self.limit]
        for manifest in manifests:
            for config in self.configs:
                self._write_task(manifest, config)

    def _write_task(self, manifest: dict, config: str) -> bool:
        caps = CONFIGS[config]
        # The task names the instance; the dataset directory names the condition.
        # Putting the condition in both was redundant, and it is the dataset that
        # Harbor is pointed at -- one job per condition, never pooled.
        task_id = f"faultloc__{manifest['local_id']}"
        task_dir = self.dataset_dir(config) / task_id
        if task_dir.exists():
            if not self.overwrite:
                print(f"skip {task_id} (exists; use --overwrite)")
                return False
            shutil.rmtree(task_dir)

        for sub in ("environment", "solution", "tests/scorer"):
            (task_dir / sub).mkdir(parents=True, exist_ok=True)

        (task_dir / "task.toml").write_text(self._task_toml(manifest, task_id, caps, config))
        (task_dir / "instruction.md").write_text(_instruction(manifest, caps))
        (task_dir / "environment" / "docker-compose.yaml").write_text(
            self._compose(manifest, caps, config)
        )

        self._write_solution(task_dir, manifest)
        self._write_tests(task_dir, manifest)
        print(f"wrote {task_dir}")
        return True

    def _task_toml(self, manifest: dict, task_id: str, caps: dict, config: str) -> str:
        hosts = ", ".join(f'"{h}"' for h in self.allowed_hosts)
        workdir = "/workspace/src" if caps["source"] else "/tmp"
        return f"""version = "1.0"

[task]
name = "flbench/{task_id}"
description = "Root-cause localization for {manifest['project']}: {manifest['crash_type']}"
authors = []
keywords = ["fault-localization", "security", "oss-fuzz", "arvo"]

[metadata]
difficulty = "hard"
category = "debugging"
local_id = {manifest['local_id']}
project = "{manifest['project']}"
sanitizer = "{manifest['sanitizer']}"
# The condition is no longer in the task name, so this is where a reward file can
# be traced back to what the agent was allowed to see.
config = "{config}"

[agent]
timeout_sec = 1800.0
# Applies during agent.run() only, so the model API is reachable while the
# upstream repository, the OSS-Fuzz issue tracker, and the fix commit are not.
network_mode = "allowlist"
allowed_hosts = [{hosts}]

[verifier]
timeout_sec = 300.0
network_mode = "no-network"

[environment]
docker_image = "{self.agent_image}"
# The reference cds into the discovered git root before launching the agent
# (FLBench entrypoint.sh:203-211); sanity starts from /tmp with no source. Without
# this the agent inherits `/`, which changes repository discovery and search.
workdir = "{workdir}"
# Baseline is public because staging must reach the PoC sidecar before any agent
# exists; `[agent]` below is what policies the run itself. Harbor applies phase
# policy during agent.run() but not during environment start or agent.setup().
network_mode = "public"
build_timeout_sec = 1800.0
cpus = 2
memory_mb = 4096
"""

    def _compose(self, manifest: dict, caps: dict, config: str) -> str:
        return compose(
            manifest,
            _staging_command(caps),
            source=caps["source"],
            environment={"EVAL_CONFIG": config, "TIMEOUT": str(POC_TIMEOUT_SEC)},
        )


    def _write_solution(self, task_dir: Path, manifest: dict) -> None:
        spans = json.dumps(_oracle_spans(manifest["gt_diff"]), indent=2)
        solve = task_dir / "solution" / "solve.sh"
        solve.write_text(
            f"""#!/bin/bash
# Oracle: emit the ground-truth spans. Never shipped to the agent.
set -eu
mkdir -p /logs/artifacts
cat > /logs/artifacts/prediction.json <<'JSON'
{spans}
JSON
echo "Oracle prediction for {manifest['local_id']}." > /logs/artifacts/summary.txt
cat /logs/artifacts/prediction.json
"""
        )
        solve.chmod(0o755)

    def _write_tests(self, task_dir: Path, manifest: dict) -> None:
        tests = task_dir / "tests"
        (tests / "gt.diff").write_text(manifest["gt_diff"])
        for name in ("__init__.py", "types.py", "ground_truth.py", "metrics.py"):
            shutil.copy(_TEMPLATE_DIR / "scorer" / name, tests / "scorer" / name)
        shutil.copy(_TEMPLATE_DIR / "anchoring.py", tests / "anchoring.py")
        shutil.copy(_TEMPLATE_DIR / "score.py", tests / "score.py")
        test_sh = tests / "test.sh"
        # Start from a clean slate: the verifier shares the agent's container, so
        # /logs/verifier may already hold something. A scorer crash deliberately
        # leaves no reward file -- Harbor then errors the trial, keeping the
        # infrastructure failure separate from a genuine agent score, as the plan
        # requires. Malformed agent output is already scored by score.py itself.
        test_sh.write_text(
            "#!/bin/bash\n"
            "set -u\n"
            "mkdir -p /logs/verifier\n"
            "rm -f /logs/verifier/reward.json /logs/verifier/reward.txt\n"
            "python3 /tests/score.py \\\n"
            "  --prediction /logs/artifacts/prediction.json \\\n"
            "  --ground-truth /tests/gt.diff \\\n"
            "  --out /logs/verifier/reward.json\n"
            "cat /logs/verifier/reward.json\n"
        )
        test_sh.chmod(0o755)


def compose(
    manifest: dict,
    staging_command: str,
    *,
    source: bool = True,
    environment: dict[str, str] | None = None,
    sidecar_setup: str = "",
) -> str:
    """The agent/sidecar environment. One copy, used by every family.

    The sidecar half -- ARVO image, build tree, reproducer, tmpfs volume -- is
    the same environment for localization and for repair, and a second copy of
    it would be a second thing to keep in step. A drift between two copies is
    also exactly what the boundary gates cannot see: they compare datasets
    within a family, never across two.

    Only three things vary, and each is a parameter. `staging_command` is the
    AGENT half -- which tools it gets, whether the reproducer file is staged.
    `source` is sanity's withholding switch: leave the shared volume empty and
    the agent container, which has no /src of its own, genuinely has none.
    `environment` is the sidecar's own configuration, including the switches
    that make withholding hold over the NETWORK rather than only on disk --
    not staging the reproducer leaves /poc-file serving the same bytes to
    anything that can reach the sidecar, and `curl http://poc:8080/poc-file` is
    one line.

    `sidecar_setup` runs in the sidecar after the tree is staged and before the
    server starts. The frozen regression suite is placed with it: the suite has
    to reach the sidecar and must NOT reach the agent's tree, since the tree is
    what it tests.
    """
    # Compose interpolates ${...} in the YAML before the shell ever sees it,
    # so a bare $ in the staging script silently becomes an empty string.
    staging = staging_command.replace("$", "$$")
    indented = "\n".join("        " + line for line in staging.splitlines())
    arvo = manifest["image"]
    project = manifest["project"]
    server = "\n".join(
        "        " + line
        for line in (_REPO_ROOT / "sidecar" / "server.py").read_text().splitlines()
    )
    env_block = "".join(
        f'\n      {key}: "{value}"' for key, value in (environment or {}).items()
    )
    setup = "".join(
        "        " + line + "\n" for line in sidecar_setup.replace("$", "$$").splitlines()
    )

    if source:
        source_stage = "\n".join(
            "        " + line
            for line in (
                f"cp -a /src/{project}/. /shared/",
                # No `|| true`: a failed clean leaves image build output in the
                # tree, the baseline commits it, and the trial runs on a state
                # no other trial shares. Under `set -eu` the staging shell dies
                # instead, the sentinel is never written and Harbor errors the
                # trial. stderr is left connected so the reason reaches the logs.
                "git -C /shared clean -fdx >/dev/null",
                # Order matters: clean needs the archival index, so the
                # history goes only after it has run.
                STRIP_ARCHIVAL_GIT,
            )
        )
    else:
        # sanity: leave the volume empty; the agent image has no source of its own.
        source_stage = "        true"

    # WHY THE FILE BELOW LOOKS THE WAY IT DOES. The rationale lives here rather
    # than in the emitted YAML: the YAML is a generated artifact written once per
    # task, and prose repeated across every copy is noise in a diff and cannot be
    # corrected in place -- the source is the only place worth explaining it.
    #
    #   * SPLIT DESIGN, mirroring FLBench's eval job. The agent runs in `main`,
    #     which contains no source, PoC or reproducer of its own; the ARVO image
    #     is confined to the `poc` sidecar. Withholding is therefore structural
    #     -- the agent cannot reach what was never put in its container -- which
    #     is what makes the ablation configs enforceable.
    #   * `init: true` gives tini as PID 1, which forwards SIGTERM. Without it
    #     the staging shell's `exec`ed process ignores it and docker waits out
    #     the full 10s grace period on every teardown (measured: 21s -> 1s).
    #   * The `main` HEALTHCHECK gates on the sentinel staging writes last.
    #     `up --wait` otherwise returns as soon as /bin/sh starts, letting the
    #     agent begin before /workspace/poc exists.
    #   * The SIDECAR holds /out, /tmp/poc and the reproducer, and stages the
    #     source onto the shared volume. The copy lives there rather than in a
    #     one-shot service because the volume is tmpfs: it exists only while a
    #     container has it mounted, and this sidecar is up for the whole trial.
    #     Its mount point is an empty path, never /src/<project>, so Docker
    #     cannot prepopulate the volume from the image behind the config's back.
    #   * `seccomp=unconfined` on the sidecar only: the server disables ASLR so
    #     MSan's fixed shadow ranges cannot collide with a randomly placed
    #     mapping (sidecar/server.py:_disable_aslr), and Docker's default profile
    #     denies the personality syscall with EPERM. The agent container keeps
    #     the default profile.
    #   * The `src` VOLUME is tmpfs so the staged source lives in RAM. Harbor's
    #     `delete: false` teardown runs a plain `docker compose down`, which
    #     keeps named volumes -- disk-backed, every trial would leak a full
    #     source tree (~120MB each, unbounded). `size` is a ceiling, not an
    #     allocation, so it costs nothing until written.
    return f"""# Generated by faultloc_adapter/adapter.py -- do not edit.
# Design and rationale: src/faultloc_adapter/adapter.py, compose().
services:
  main:
    init: true
    volumes:
      - "src:/workspace/src"
    healthcheck:
      test: ["CMD", "test", "-f", "{STAGED_SENTINEL}"]
      interval: 2s
      timeout: 5s
      retries: 300
      start_period: 3s
    depends_on:
      poc:
        condition: service_healthy
    command:
      - /bin/sh
      - -c
      - |
{indented}

  poc:
    init: true
    security_opt:
      - seccomp=unconfined
    environment:{env_block}
    image: {arvo}
    networks: [default]
    volumes:
      - "src:/shared"
    command:
      - /bin/sh
      - -c
      - |
        set -eu
{source_stage}
{setup}        cat > /tmp/server.py <<'SERVER_EOF'
{server}
        SERVER_EOF
        exec python3 /tmp/server.py
    healthcheck:
      test: ["CMD", "python3", "-c",
             "import urllib.request;urllib.request.urlopen('http://localhost:8080/health')"]
      interval: 2s
      timeout: 5s
      retries: 150
      start_period: 3s

volumes:
  src:
    driver: local
    driver_opts:
      type: tmpfs
      device: tmpfs
      o: size=4g
"""

