"""Repair controls across every frozen instance: which ones are repair-eligible.

An instance is only usable for the repair experiment if the developer patch,
applied to the tree the agent is actually handed, builds and suppresses the PoC.
That is not a property of the instance -- it is a property of the whole staged
path, including `git clean -fdx` at staging and the sidecar's tree sync -- so it
has to be measured through the generated task, not against the bare ARVO image.

Two controls per instance:

  oracle: apply solution/solve.sh, then verify. Must give reward 1.
  nop:    verify an untouched tree.        Must give reward 0, patch_present 0.

Run against the `gold` dataset: repair datasets differ only by the report, which
neither control reads, so eligibility measured on gold holds for every member of
the set. Run this before spending anything on agents, and treat it as instance
selection rather than a pass/fail gate: an instance whose oracle fails is one the
harness cannot score, and belongs out of the subset.

    python scripts/repair_controls.py \\
        --tasks datasets/flbench-repair-eval500-gold --report repair.json
"""

import argparse
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def compose_cmd(env_dir: Path, project: str, override: str) -> list[str]:
    return [
        "docker", "compose", "-p", project,
        "-f", str(env_dir / "docker-compose.yaml"), "-f", override,
    ]


def run_control(task_dir: Path, local_id: str, agent_image: str, keep: bool) -> dict:
    env_dir = task_dir / "environment"
    project = f"rc{local_id}"
    container = f"{project}-main-1"
    result = {"local_id": local_id}

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        json.dump({"services": {"main": {"image": agent_image}}}, fh)
        override = fh.name
    compose = compose_cmd(env_dir, project, override)

    def in_container(script: str, timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", "exec", container, "bash", "-lc", script],
            capture_output=True, text=True, timeout=timeout,
        )

    try:
        up = subprocess.run([*compose, "up", "-d", "--wait"], capture_output=True, text=True)
        if up.returncode != 0:
            result["status"] = "ENV_FAILED"
            result["error"] = (up.stderr or up.stdout)[-300:]
            return result

        subprocess.run(
            ["docker", "cp", str(task_dir / "tests"), f"{container}:/tests"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["docker", "cp", str(task_dir / "solution" / "solve.sh"), f"{container}:/solve.sh"],
            capture_output=True, check=True,
        )

        # nop first: it must see an untouched tree, so it has to run before the
        # oracle patches one. It skips the build entirely (nothing changed), so it
        # costs a couple of seconds.
        nop = in_container("bash /tests/test.sh", 600)
        result["nop"] = _reward(nop)

        patched = in_container("bash /solve.sh", 300)
        if patched.returncode != 0:
            result["status"] = "PATCH_FAILED"
            result["error"] = (patched.stderr or patched.stdout)[-300:]
            return result

        oracle = in_container("bash /tests/test.sh", 3600)
        result["oracle"] = _reward(oracle)

        checks = {
            "nop_zero": result["nop"].get("reward") == 0.0,
            "nop_no_patch": result["nop"].get("patch_present") == 0,
            "oracle_compiles": result["oracle"].get("compiled") == 1,
            "oracle_suppresses": result["oracle"].get("poc_suppressed") == 1,
            "oracle_attributable": result["oracle"].get("at_location") == 1,
            "oracle_one": result["oracle"].get("reward") == 1.0,
        }
        result["checks"] = checks
        result["failed"] = sorted(k for k, ok in checks.items() if not ok)
        result["status"] = "PASS" if not result["failed"] else "FAIL"
        return result
    except subprocess.TimeoutExpired:
        result["status"] = "TIMEOUT"
        return result
    except Exception as e:
        # One instance must not take the batch down. This runs 500 instances
        # through multi-minute build-and-verify cycles under a thread pool, and an
        # exception escaping the worker is re-raised by future.result() only after
        # every other container has finished -- so the script dies at the end
        # having written no --report and thrown away every result it did collect.
        # `docker cp` with check=True is the live one: a task directory missing
        # solve.sh raises CalledProcessError, which the TimeoutExpired clause
        # above does not catch.
        result["status"] = "ERROR"
        result["error"] = f"{type(e).__name__}: {e}"[-300:]
        return result
    finally:
        if not keep:
            subprocess.run(
                [*compose, "down", "--volumes", "--remove-orphans"], capture_output=True
            )
        Path(override).unlink(missing_ok=True)


def _reward(proc: subprocess.CompletedProcess) -> dict:
    """Parse the reward JSON the verifier prints; {} when it wrote none.

    An empty dict is meaningful: the verifier raises rather than scoring when the
    failure is infrastructure, so no reward file is exactly what a broken sidecar
    should produce.
    """
    text = proc.stdout.strip()
    start = text.find("{")
    if start < 0:
        return {"error": (proc.stderr or text)[-300:]}
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        return {"error": text[-300:]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--manifests", type=Path, default=Path("manifests"))
    parser.add_argument("--agent-image", default="faultloc-agent:v1")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()

    pending = []
    for manifest_path in sorted(args.manifests.glob("*.json")):
        local_id = manifest_path.stem
        task_dir = args.tasks / f"repair__{local_id}"
        if not task_dir.exists():
            print(f"{local_id}: no generated repair task, skipping")
            continue
        pending.append((task_dir, local_id))

    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(run_control, task_dir, local_id, args.agent_image, args.keep)
            for task_dir, local_id in pending
        ]
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            detail = ",".join(r.get("failed", [])) or r.get("error", "")[:70]
            print(f"{r['status']:<12} {r['local_id']:<11} {detail}", flush=True)

    results.sort(key=lambda r: str(r["local_id"]))
    passed = sum(1 for r in results if r["status"] == "PASS")
    print(f"\n{passed}/{len(results)} repair-eligible")
    if args.report:
        args.report.write_text(json.dumps(results, indent=2) + "\n")
        print(f"report written to {args.report}")
    return 0 if results and passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
