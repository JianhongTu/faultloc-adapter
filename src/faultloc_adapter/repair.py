"""Generate Harbor *repair* tasks from the same frozen manifests.

Separate task family from the localization adapter: nothing here changes
adapter.py, prompt.md, score.py, or the vendored scorer. The two share the
manifests, the ARVO digests, the agent image, and the split agent/sidecar shape.

The question a repair task measures is whether an external root-cause report
raises a fixed implementation agent's chance of a verified fix over that same
agent's own debugging. So the baseline is self-diagnosis, not "no root cause":

  * `self` -- source, PoC and sanitizer report; the agent diagnoses and fixes.
  * `gold` -- the same, plus a developer-derived root-cause report.

Both conditions get identical tools, budget and prompt; they differ only in the
report section and the task identity. `gold` is a calibration ceiling, not a
forecast: its locations come from the developer patch, so an agent that fixes
where the developer fixed scores attributable almost by construction.

See README.md for the shared design constraints; sidecar/repair_server.py for
why the build tree is synced rather than shared.
"""

import json
import shutil
from pathlib import Path

from . import manifest as manifest_mod
from .adapter import DEFAULT_AGENT_IMAGE, DEFAULT_ALLOWED_HOSTS, STAGED_SENTINEL
from .scorer import parse_diff

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

# `self` withholds the report; `gold` supplies one derived from the developer
# patch. `location` and `full` belong to the main experiment and are deliberately
# absent -- adding them is a new entry here plus a report file, nothing more.
REPAIR_CONFIGS = {
    "self": {"report": False},
    "gold": {"report": True},
}

DEFAULT_REPAIR_CONFIGS = tuple(REPAIR_CONFIGS)


def gold_report(manifest: dict) -> dict:
    """Root-cause report derived from the developer patch, one location per hunk.

    Deliberately carries locations only. A `cause` written by summarizing the
    patch would leak the fix, so authored explanations arrive out of band via
    --reports and are the author's responsibility to keep fix-free.
    """
    locations = []
    for hunk in parse_diff(manifest["gt_diff"]):
        lines = sorted(hunk.lines)
        locations.append(
            {
                "file": hunk.file,
                "line_start": lines[0],
                # Exclusive, matching the FL span convention.
                "line_end": lines[-1] + 1,
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


def _render_report(report: dict) -> str:
    lines = ["## Reported Root Cause", ""]
    lines.append(
        "An external analysis identified the root cause at the following location(s). "
        "Treat it as a strong hint, not as proof; verify it against the source."
    )
    lines.append("")
    for loc in report["locations"]:
        where = f"`{loc['file']}` lines {loc['line_start']}-{loc['line_end'] - 1}"
        if loc.get("function"):
            where += f", in `{loc['function']}`"
        lines.append(f"- {where}")
    if report.get("cause"):
        lines += ["", f"**Cause:** {report['cause']}"]
    return "\n".join(lines)


def _staging_command(project: str) -> str:
    """Shell run as the agent container's command, before the agent starts.

    Establishes the patch baseline. The tag is created over whatever the sidecar
    staged, so the diff is against the tree the agent was actually handed rather
    than against upstream HEAD, which `git clean -fdx` may have moved away from.
    """
    return "\n".join(
        [
            "set -eu",
            "mkdir -p /workspace /logs/artifacts",
            # The staged tree was written by another container, so git refuses it
            # as dubious ownership without this.
            "git config --global --add safe.directory '*'",
            "cd /workspace/src",
            "git add -A",
            "git -c user.email=harbor@local -c user.name=harbor "
            "commit -q --allow-empty -m 'harbor baseline'",
            f"git tag {BASELINE_TAG}",
            # Written here rather than baked into the agent image so the image
            # stays shared with the localization task.
            "cat > /usr/local/bin/compile.sh <<'CEOF'",
            _COMPILE_SH,
            "CEOF",
            "chmod +x /usr/local/bin/compile.sh",
            # Signal completion last: `docker compose up --wait` returns when the
            # container is *running*, not when this script finishes.
            f"touch {STAGED_SENTINEL}",
            "exec sleep infinity",
        ]
    )


_COMPILE_SH = """#!/bin/sh
# Rebuild the project from your working tree at /workspace/src.
# Exit 0 means the build succeeded. Anything else is a build failure; the
# compiler output is printed so you can fix it.
exec python3 - "$@" <<'PY'
import json, sys, urllib.request
req = urllib.request.Request("http://poc:8080/compile", method="POST", data=b"")
with urllib.request.urlopen(req, timeout=2000) as resp:
    r = json.loads(resp.read())
if r.get("sync_failed"):
    sys.stderr.write("infrastructure error: could not sync the source tree\\n")
    sys.exit(70)
sys.stdout.write(r.get("output", ""))
sys.exit(r.get("exit_code", 1))
PY
"""


def _instruction(manifest: dict, config: str, report: dict | None) -> str:
    template = (_TEMPLATE_DIR / "repair_prompt.md").read_text()
    report_section = _render_report(report) if REPAIR_CONFIGS[config]["report"] else ""
    text = (
        template.replace("{{REPORT_SECTION}}", report_section)
        .replace("{{CRASH_TYPE}}", manifest["crash_type"] or "unknown")
        .replace("{{SANITIZER}}", manifest["sanitizer"] or "unknown")
        .replace("{{FUZZ_TARGET}}", manifest["fuzz_target"] or "unknown")
        .replace("{{SANITIZER_REPORT}}", manifest["crash_output"])
    )
    return "\n".join(line.rstrip() for line in text.splitlines()) + "\n"


class RepairAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        manifest_dir: Path | None = None,
        configs: list[str] | None = None,
        reports_dir: Path | None = None,
        agent_image: str = DEFAULT_AGENT_IMAGE,
        allowed_hosts: list[str] | None = None,
        org: str = "flbench",
        **kwargs,
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.manifest_dir = Path(manifest_dir or _REPO_ROOT / "manifests")
        self.configs = configs or list(DEFAULT_REPAIR_CONFIGS)
        self.reports_dir = Path(reports_dir) if reports_dir else None
        self.agent_image = agent_image
        self.allowed_hosts = list(allowed_hosts or DEFAULT_ALLOWED_HOSTS)
        self.org = org

        unknown = set(self.configs) - set(REPAIR_CONFIGS)
        if unknown:
            raise ValueError(f"unknown repair config(s): {sorted(unknown)}")

    def _manifests(self) -> list[dict]:
        paths = sorted(self.manifest_dir.glob("*.json"))
        if self.task_ids:
            wanted = {str(t) for t in self.task_ids}
            paths = [p for p in paths if p.stem in wanted]
            missing = wanted - {p.stem for p in paths}
            if missing:
                raise FileNotFoundError(
                    f"no manifest in {self.manifest_dir} for: {sorted(missing)}. "
                    f"Freeze it first with `python -m faultloc_adapter.freeze`."
                )
        return [manifest_mod.load(p) for p in paths]

    def _report(self, manifest: dict) -> dict:
        """The report `gold` renders: an authored one if supplied, else derived."""
        if self.reports_dir:
            path = self.reports_dir / f"{manifest['local_id']}.json"
            if path.exists():
                return json.loads(path.read_text())
        return gold_report(manifest)

    def run(self) -> None:
        manifests = self._manifests()
        if not manifests:
            raise FileNotFoundError(f"no manifests found in {self.manifest_dir}")
        written = 0
        for manifest in manifests:
            for config in self.configs:
                if self.limit is not None and written >= self.limit:
                    return
                if self._write_task(manifest, config):
                    written += 1

    def _write_task(self, manifest: dict, config: str) -> bool:
        task_id = f"repair__{manifest['local_id']}-{config}"
        task_dir = self.output_dir / task_id
        if task_dir.exists():
            if not self.overwrite:
                print(f"skip {task_id} (exists; use --overwrite)")
                return False
            shutil.rmtree(task_dir)

        for sub in ("environment", "solution", "tests/scorer"):
            (task_dir / sub).mkdir(parents=True, exist_ok=True)

        report = self._report(manifest) if REPAIR_CONFIGS[config]["report"] else None
        (task_dir / "task.toml").write_text(self._task_toml(manifest, task_id, config))
        (task_dir / "instruction.md").write_text(_instruction(manifest, config, report))
        (task_dir / "environment" / "docker-compose.yaml").write_text(self._compose(manifest))

        self._write_solution(task_dir, manifest)
        self._write_tests(task_dir, manifest, config, report)
        print(f"wrote {task_dir}")
        return True

    def _task_toml(self, manifest: dict, task_id: str, config: str) -> str:
        hosts = ", ".join(f'"{h}"' for h in self.allowed_hosts)
        return f"""version = "1.0"

[task]
name = "{self.org}/{task_id}"
description = "Repair {manifest['project']}: {manifest['crash_type']} ({config})"
authors = []
keywords = ["program-repair", "security", "oss-fuzz", "arvo"]

[metadata]
difficulty = "hard"
category = "debugging"
local_id = {manifest['local_id']}
project = "{manifest['project']}"
sanitizer = "{manifest['sanitizer']}"
condition = "{config}"

[agent]
# Larger than localization's: the agent compiles and re-runs the PoC in a loop.
timeout_sec = 3600.0
network_mode = "allowlist"
allowed_hosts = [{hosts}]

[verifier]
# The verifier compiles the agent's tree and re-runs the PoC, both through the
# sidecar, so it needs `poc` and nothing else.
timeout_sec = 2400.0
network_mode = "allowlist"
allowed_hosts = ["poc"]

[environment]
docker_image = "{self.agent_image}"
workdir = "/workspace/src"
# Staging must reach the sidecar before any agent exists; `[agent]` above is what
# policies the run itself.
network_mode = "public"
build_timeout_sec = 1800.0
cpus = 4
memory_mb = 8192
"""

    def _compose(self, manifest: dict) -> str:
        staging = _staging_command(manifest["project"]).replace("$", "$$")
        indented = "\n".join("        " + line for line in staging.splitlines())
        arvo = manifest_mod.pinned_image(manifest)
        project = manifest["project"]
        server = "\n".join(
            "        " + line
            for line in (_REPO_ROOT / "sidecar" / "repair_server.py").read_text().splitlines()
        )

        return f"""# Same split as the localization task: the agent runs in `main` with no ARVO
# content of its own, and the build tree, reproducer and toolchain stay in the
# `poc` sidecar. The agent edits /workspace/src and reaches compile.sh and
# run_poc.sh over HTTP; the sidecar syncs that tree onto /src/{project} before
# each build, so what compiles is exactly what the agent wrote.
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
    # Lets sidecar/repair_server.py:_disable_aslr clear the randomization bit;
    # Docker's default profile denies `personality` with EPERM. Without it msan
    # instances intermittently die during sanitizer init, which a repair task
    # would silently read as "the fix did not work".
    security_opt:
      - seccomp=unconfined
    environment:
      PROJECT: "{project}"
      TIMEOUT: "{POC_TIMEOUT_SEC}"
      COMPILE_TIMEOUT: "{COMPILE_TIMEOUT_SEC}"
    image: {arvo}
    networks: [default]
    volumes:
      - "src:/shared"
    command:
      - /bin/sh
      - -c
      - |
        set -eu
        cp -a /src/{project}/. /shared/
        git -C /shared clean -fdx >/dev/null 2>&1 || true
        cat > /tmp/server.py <<'SERVER_EOF'
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
  # tmpfs, as in the localization task: teardown keeps named volumes, so a
  # disk-backed one would leak a source tree per trial. Build output does NOT
  # land here -- it stays in the sidecar's own filesystem -- so this holds only
  # source and .git and the ceiling is not load-bearing for build size.
  src:
    driver: local
    driver_opts:
      type: tmpfs
      device: tmpfs
      o: size=4g
"""

    def _write_solution(self, task_dir: Path, manifest: dict) -> None:
        """Oracle: apply the developer patch. Never shipped to the agent."""
        solve = task_dir / "solution" / "solve.sh"
        solve.write_text(
            f"""#!/bin/bash
# Oracle: apply the frozen developer patch to the agent's working tree. The
# verifier compiles and re-runs the PoC exactly as it would for an agent, so a
# passing oracle proves the whole build/verify path, not just the scorer.
set -eu
mkdir -p /logs/artifacts
cat > /tmp/gt.diff <<'PATCH_EOF'
{manifest['gt_diff']}
PATCH_EOF
cd /workspace/src
patch -p1 < /tmp/gt.diff
echo "Applied the developer patch for {manifest['local_id']}." \\
  > /logs/artifacts/summary.txt
"""
        )
        solve.chmod(0o755)

    def _write_tests(self, task_dir: Path, manifest: dict, config: str, report: dict | None) -> None:
        tests = task_dir / "tests"
        for name in ("__init__.py", "types.py", "ground_truth.py", "metrics.py"):
            shutil.copy(_TEMPLATE_DIR / "scorer" / name, tests / "scorer" / name)
        shutil.copy(_TEMPLATE_DIR / "repair_score.py", tests / "repair_score.py")
        if report is not None:
            (tests / "report_spans.json").write_text(
                json.dumps(report_spans(report), indent=2) + "\n"
            )
        report_arg = (
            "  --report-spans /tests/report_spans.json \\\n" if report is not None else ""
        )
        test_sh = tests / "test.sh"
        # A scorer or sidecar crash deliberately leaves no reward file, so Harbor
        # errors the trial instead of recording a zero the agent did not earn.
        test_sh.write_text(
            "#!/bin/bash\n"
            "set -u\n"
            "mkdir -p /logs/verifier\n"
            "rm -f /logs/verifier/reward.json /logs/verifier/reward.txt\n"
            "python3 /tests/repair_score.py \\\n"
            "  --source /workspace/src \\\n"
            f"  --baseline {BASELINE_TAG} \\\n"
            f"  --condition {config} \\\n"
            f"{report_arg}"
            "  --artifacts /logs/artifacts \\\n"
            "  --out /logs/verifier/reward.json\n"
            "cat /logs/verifier/reward.json\n"
        )
        test_sh.chmod(0o755)
