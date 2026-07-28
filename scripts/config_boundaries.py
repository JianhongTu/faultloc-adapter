"""Config boundary gate: assert each config withholds what it claims to.

An earlier version of this check grepped
the generated files, which could only ever confirm what staging *writes* -- the
leak was everything staging never touched, so the check could not fail. This one
brings the environment up and interrogates the agent's container directly.

    python scripts/config_boundaries.py --tasks datasets/faultloc-adapter --task-id 42470093
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# What the agent's container must and must not expose, per config.
EXPECT = {
    "main": {"source": True, "poc_file": True, "run_tool": True},
    "ablation1": {"source": True, "poc_file": True, "run_tool": False},
    "ablation2": {"source": True, "poc_file": False, "run_tool": True},
    "sanity": {"source": False, "poc_file": True, "run_tool": True},
}

PROBE = r"""
echo "src_entries=$(ls /workspace/src 2>/dev/null | wc -l)"
echo "poc_file=$([ -s /workspace/poc ] && echo yes || echo no)"
echo "run_tool=$(command -v run_poc.sh >/dev/null && echo yes || echo no)"
# The ARVO filesystem must not be reachable from the agent's container at all.
echo "arvo_bin=$(command -v arvo >/dev/null && echo LEAK || echo absent)"
echo "src_root=$([ -d /src ] && echo LEAK || echo absent)"
echo "out_dir=$([ -d /out ] && echo LEAK || echo absent)"
echo "gt_leak=$([ -e /tests/gt.diff ] && echo LEAK || echo absent)"
# Withholding must hold over the network too, not just on disk: the sidecar
# serves the same bytes to anything that can reach it.
echo "poc_endpoint=$(curl -s -o /tmp/pf.bin -w '%{http_code}' http://poc:8080/poc-file)"
echo "poc_endpoint_bytes=$(wc -c < /tmp/pf.bin 2>/dev/null || echo 0)"
echo "run_endpoint=$(curl -s -X POST -o /dev/null -w '%{http_code}' http://poc:8080/poc)"
run_poc.sh >/tmp/rp.log 2>&1; echo "run_poc_exit=$?"
echo "poc_frames=$(grep -cE '#[0-9]+ 0x[0-9a-f]+ in .+ /.+:[0-9]+' /tmp/rp.log 2>/dev/null || echo 0)"
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
        probe = subprocess.run(
            [*compose, "exec", "-T", "main", "sh", "-c", PROBE],
            cwd=env_dir, capture_output=True, text=True, timeout=600,
        )
        p = parse(probe.stdout)
        result["probe"] = p
        want = EXPECT[config]

        checks = {
            "source": (int(p.get("src_entries", "0") or 0) > 0) == want["source"],
            "poc_file": (p.get("poc_file") == "yes") == want["poc_file"],
            "run_tool": (p.get("run_tool") == "yes") == want["run_tool"],
            # A withheld resource must be unreachable, not merely absent.
            "poc_endpoint": (p.get("poc_endpoint") == "200") == want["poc_file"],
            "run_endpoint": (p.get("run_endpoint") == "200") == want["run_tool"],
            # Universal: the ARVO filesystem is confined to the sidecar.
            "no_arvo_bin": p.get("arvo_bin") == "absent",
            "no_src_root": p.get("src_root") == "absent",
            "no_out_dir": p.get("out_dir") == "absent",
            "no_gt_leak": p.get("gt_leak") == "absent",
        }
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
    ap.add_argument("--tasks", type=Path, required=True)
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--agent-image", default="faultloc-agent:v1")
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args()

    results = []
    for config in EXPECT:
        task_dir = args.tasks / f"faultloc__{args.task_id}-{config}"
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
