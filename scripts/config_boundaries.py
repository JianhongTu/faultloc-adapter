"""Config boundary gate: assert each config withholds what it claims to.

An earlier version of this check grepped
the generated files, which could only ever confirm what staging *writes* -- the
leak was everything staging never touched, so the check could not fail. This one
brings the environment up and interrogates the agent's container directly.

Each config is its own dataset, so this walks all four and probes whichever are
generated -- the comparison is across datasets, not within one.

`repair` is checked here too. It is not a fifth localization condition: it is the
workspace the two families SHARE, so the same probe is the way to state that. The
repair task is the full-information localization task plus the developer patch
and a build tool, and the endpoints that differ -- /compile and /test -- are
asserted unreachable from every localization arm.

    python scripts/config_boundaries.py --task-id 42470093
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faultloc_adapter.adapter import DATASET_NAMES, DATASET_ROOT  # noqa: E402
from faultloc_adapter.repair import DATASET_NAME as REPAIR_DATASET  # noqa: E402

# What the agent's container must and must not expose, per config.
#
# `repair` is the one config that may build. It is checked here rather than
# trusted because it is the only place the compile token reaches an agent, and
# the property that matters is not "repair can build" on its own -- it is that
# NOTHING ELSE can, which only a probe across every config can state: a
# localization arm that could compile would be measuring a different task.
# repair ships run_tests.sh for every instance -- a placeholder where no suite has
# been written yet -- so the tool is asserted PRESENT there and absent everywhere
# else, in both directions.
EXPECT = {
    "main": {"source": True, "poc_file": True, "run_tool": True,
             "build_tool": False, "test_tool": False},
    "ablation1": {"source": True, "poc_file": True, "run_tool": False,
                  "build_tool": False, "test_tool": False},
    "ablation2": {"source": True, "poc_file": False, "run_tool": True,
                  "build_tool": False, "test_tool": False},
    "sanity": {"source": False, "poc_file": True, "run_tool": True,
               "build_tool": False, "test_tool": False},
    # No reproducer FILE, on purpose: the agent may run it but not read it, so a
    # patch cannot special-case the input bytes. See repair.py.
    "repair": {"source": True, "poc_file": False, "run_tool": True,
               "build_tool": True, "test_tool": True},
}

PROBE = r"""
echo "src_entries=$(ls /workspace/src 2>/dev/null | wc -l)"
echo "poc_file=$([ -s /workspace/poc ] && echo yes || echo no)"
echo "run_tool=$(command -v run_poc.sh >/dev/null && echo yes || echo no)"
echo "build_tool=$(command -v build.sh >/dev/null && echo yes || echo no)"
echo "test_tool=$(command -v run_tests.sh >/dev/null && echo yes || echo no)"
# The suite the sidecar runs must never be reachable from the agent's tree: the
# tree is what it tests, and a copy here is a copy the agent can weaken.
echo "test_script=$([ -e /usr/local/bin/frozen_test.sh ] && echo LEAK || echo absent)"
# The ARVO filesystem must not be reachable from the agent's container at all.
echo "arvo_bin=$(command -v arvo >/dev/null && echo LEAK || echo absent)"
echo "src_root=$([ -d /src ] && echo LEAK || echo absent)"
echo "out_dir=$([ -d /out ] && echo LEAK || echo absent)"
echo "gt_leak=$([ -e /tests/gt.diff ] && echo LEAK || echo absent)"
# The staged repository must be the snapshot and nothing else. Archival history
# is a ground-truth leak reachable with `git log --all`, and it survives
# `git clean -fdx`, so counting commits is the check that it was stripped.
echo "git_commits=$(git -C /workspace/src rev-list --count --all 2>/dev/null || echo 0)"
echo "git_remotes=$(git -C /workspace/src remote 2>/dev/null | wc -l)"
# --ignored as well as untracked: the baseline must cover every file in the
# snapshot, or an edit to one is invisible to the captured diff.
echo "git_outside=$(git -C /workspace/src status --porcelain --ignored 2>/dev/null | wc -l)"
# Withholding must hold over the network too, not just on disk: the sidecar
# serves the same bytes to anything that can reach it.
echo "poc_endpoint=$(curl -s -o /tmp/pf.bin -w '%{http_code}' http://poc:8080/poc-file)"
echo "poc_endpoint_bytes=$(wc -c < /tmp/pf.bin 2>/dev/null || echo 0)"
echo "run_endpoint=$(curl -s -X POST -o /dev/null -w '%{http_code}' http://poc:8080/poc)"
run_poc.sh >/tmp/rp.log 2>&1; echo "run_poc_exit=$?"
echo "poc_frames=$(grep -cE '#[0-9]+ 0x[0-9a-f]+ in .+ /.+:[0-9]+' /tmp/rp.log 2>/dev/null || echo 0)"
"""

# The endpoint exists in the one sidecar both families run, so what keeps an agent
# from building is a gate, not the absence of code -- worth probing rather than
# reading. 404 for localization (no PROJECT), 403 for repair (no token: the
# verifier's copy arrives in /tests only after the agent phase). Either way the
# request is refused before any build starts, so this is cheap.
COMPILE_PROBE = r"""
echo "compile_endpoint=$(curl -s -X POST -o /dev/null -w '%{http_code}' http://poc:8080/compile)"
echo "compile_token_file=$([ -e /tests/compile_token ] && echo LEAK || echo absent)"
# /test is refused for everyone at agent time: 404 where no suite was staged,
# 403 where one was and the token has not arrived yet. The assertion is that it
# is never 200 -- which is the property, and the one form that covers both.
echo "test_endpoint=$(curl -s -X POST -o /dev/null -w '%{http_code}' http://poc:8080/test)"
"""


def parse(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def check_one(task_dir: Path, config: str, agent_image: str) -> dict:
    env_dir = task_dir / "environment"
    project = f"cfg{config}"
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        json.dump({"services": {"main": {"image": agent_image}}}, fh)
        override = fh.name
    compose = ["docker", "compose", "-p", project, "-f", "docker-compose.yaml", "-f", override]
    result = {"config": config}
    try:
        up = subprocess.run(
            # --build: compose reuses an existing built image otherwise, so a
            # changed sidecar (e.g. server.py) would be silently ignored.
            [*compose, "up", "-d", "--wait", "--build"],
            cwd=env_dir, capture_output=True, text=True
        )
        if up.returncode != 0:
            result["status"] = "ENV_FAILED"
            result["error"] = (up.stderr or up.stdout)[-300:]
            return result
        script = PROBE + COMPILE_PROBE
        probe = subprocess.run(
            [*compose, "exec", "-T", "main", "sh", "-c", script],
            cwd=env_dir, capture_output=True, text=True, timeout=600,
        )
        p = parse(probe.stdout)
        result["probe"] = p
        want = EXPECT[config]

        checks = {
            "source": (int(p.get("src_entries", "0") or 0) > 0) == want["source"],
            "poc_file": (p.get("poc_file") == "yes") == want["poc_file"],
            "run_tool": (p.get("run_tool") == "yes") == want["run_tool"],
            # The whole point of the probe: a build tool exists for repair and
            # for nothing else. Its absence elsewhere is what keeps repair's
            # one-attempt-no-oracle measurement true.
            "build_tool": (p.get("build_tool") == "yes") == want["build_tool"],
            # None means "may or may not be present" -- see EXPECT.
            "test_tool": (p.get("test_tool") == "yes") == want["test_tool"],
            "no_test_script": p.get("test_script") == "absent",
            # A withheld resource must be unreachable, not merely absent.
            "poc_endpoint": (p.get("poc_endpoint") == "200") == want["poc_file"],
            "run_endpoint": (p.get("run_endpoint") == "200") == want["run_tool"],
            # Universal: the ARVO filesystem is confined to the sidecar.
            "no_arvo_bin": p.get("arvo_bin") == "absent",
            "no_src_root": p.get("src_root") == "absent",
            "no_out_dir": p.get("out_dir") == "absent",
            "no_gt_leak": p.get("gt_leak") == "absent",
        }
        if want["source"]:
            # One baseline commit, no upstream refs: the agent can use git on the
            # tree it was handed and cannot read the future out of it.
            checks["no_archival_history"] = (
                p.get("git_commits") == "1" and p.get("git_remotes") == "0"
            )
            # Nothing left outside the baseline: every staged file is in it, so
            # every edit the agent makes reaches the diff.
            checks["baseline_complete"] = p.get("git_outside") == "0"
        # No build for the agent, over the network either: localization has no
        # PROJECT and so no endpoint, repair has one but the token that opens it
        # is not in the container yet.
        # The probe never presents a token, so a token-gated endpoint answers 403
        # whether or not the agent could open it -- which is why `build_tool`
        # above, not this, is what distinguishes repair from localization.
        checks["no_compile"] = p.get("compile_endpoint") == (
            "403" if config in ("repair", "repair") else "404"
        )
        checks["no_compile_token"] = p.get("compile_token_file") == "absent"
        checks["no_test_endpoint"] = p.get("test_endpoint") != "200"
        if want["run_tool"]:
            # The reproducer must still work where it is offered.
            checks["poc_reproduces"] = int(p.get("poc_frames", "0") or 0) > 0
        else:
            # ablation1: no script, and the sidecar refuses the endpoint (exit 2
            # is the 403 path; 127 is the missing script).
            checks["run_tool_denied"] = p.get("run_poc_exit") in ("2", "127")
        result["checks"] = checks
        result["failed"] = sorted(k for k, ok in checks.items() if not ok)
        result["status"] = "PASS" if not result["failed"] else "FAIL"
        return result
    except subprocess.TimeoutExpired:
        result["status"] = "TIMEOUT"
        return result
    finally:
        subprocess.run(
            [*compose, "down", "--volumes", "--remove-orphans"], cwd=env_dir, capture_output=True
        )
        Path(override).unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--datasets", type=Path, default=DATASET_ROOT,
                    help="parent of the per-config datasets (default: datasets/)")
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--agent-image", default="faultloc-agent:v1")
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args()

    results = []
    for config in EXPECT:
        if config == "repair":
            task_dir = args.datasets / REPAIR_DATASET / f"repair__{args.task_id}"
        else:
            task_dir = args.datasets / DATASET_NAMES[config] / f"faultloc__{args.task_id}"
        if not task_dir.exists():
            print(f"{config}: not generated, skipping")
            continue
        r = check_one(task_dir, config, args.agent_image)
        results.append(r)
        print(
            f"{r['status']:<11} {config:<11} "
            f"{','.join(r.get('failed', [])) or r.get('error', '')[:60]}"
        )

    passed = sum(1 for r in results if r["status"] == "PASS")
    print(f"\n{passed}/{len(results)} configs enforce their boundaries")
    if args.report:
        args.report.write_text(json.dumps(results, indent=2) + "\n")
    return 0 if results and passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
