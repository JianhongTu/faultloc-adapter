"""Environment parity sweep: verify generated tasks hold up across instances.

Config boundaries are covered separately by
scripts/config_boundaries.py on one instance; this sweep asks the orthogonal
question -- does the task shape work across projects, sanitizers, languages, and
ARVO base images?

For each instance it brings the compose stack up and asserts, from inside the
agent's container, that:

  * the reference layout is staged (/workspace/src, /workspace/poc);
  * the ground-truth line in the staged source is the line the patch deletes;
  * the PoC reproduces through the sidecar, with symbolization intact;
  * the ARVO filesystem is not reachable from the agent's container.

Harbor supplies `main.image` from `[environment] docker_image` via its own compose
override, so this script supplies an equivalent override to run compose directly.

    python scripts/env_sweep.py \\
        --tasks datasets/flbench-diagnosis-eval900-main --report sweep.json
"""

import argparse
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faultloc_adapter.anchoring import parse_diff_flbench  # noqa: E402

# ARVO records labels like "UNKNOWN WRITE" or "Check failed" that never appear
# verbatim in a SUMMARY line, so a mismatch is reported rather than failed.
SOFT_CHECKS = {"crash_type_matches"}

PROBE = r"""
echo "src_entries=$(ls /workspace/src 2>/dev/null | wc -l)"
echo "poc_bytes=$(stat -c%s /workspace/poc 2>/dev/null || echo 0)"
echo "gt_line=$(sed -n "${GT_LINE}p" "/workspace/src/${GT_FILE}" 2>/dev/null)"
# The ARVO filesystem belongs to the sidecar; none of it may be reachable here.
echo "arvo_bin=$(command -v arvo >/dev/null && echo LEAK || echo absent)"
echo "src_root=$([ -d /src ] && echo LEAK || echo absent)"
echo "out_dir=$([ -d /out ] && echo LEAK || echo absent)"
# msan instances used to crash without emitting a report on a fraction of runs
# (ASLR colliding with the shadow ranges; fixed in sidecar/server.py:_disable_aslr).
# Keep attempting 3 times and recording the count -- it is the regression signal:
# anything below 3/3 means the randomization bit is not being cleared.
ok=0
for attempt in 1 2 3; do
  run_poc.sh >/tmp/rp.log 2>&1; last=$?
  f=$(grep -cE '#[0-9]+ 0x[0-9a-f]+ in .+ /.+:[0-9]+' /tmp/rp.log)
  if [ "$f" -gt 0 ]; then ok=$((ok+1)); cp /tmp/rp.log /tmp/rp.good; fi
done
echo "poc_exit=$last"
echo "poc_reports_ok=$ok"
echo "poc_frames=$(grep -cE '#[0-9]+ 0x[0-9a-f]+ in .+ /.+:[0-9]+' /tmp/rp.good 2>/dev/null || echo 0)"
echo "summary=$(grep -m1 'SUMMARY:' /tmp/rp.good 2>/dev/null)"
"""


def parse_probe(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def gt_expectation(manifest: dict) -> tuple[str, int, str]:
    """Return (file, line, expected source text) for the patch's first GT line."""
    diff = manifest["gt_diff"]
    # Un-widened: this reads the anchor line's source text out of the image, and
    # the anchor is min(hunk.lines) under either rule.
    hunk = parse_diff_flbench(diff)[0]
    line = min(hunk.lines)
    expected = ""
    if not hunk.addition_only:
        # The first '-' line of the first hunk is the source text at that line.
        for raw in diff.splitlines():
            if raw.startswith("-") and not raw.startswith("---"):
                expected = raw[1:]
                break
    return hunk.file, line, expected


def sweep_one(task_dir: Path, manifest: dict, agent_image: str, keep: bool) -> dict:
    gt_file, gt_line, expected_text = gt_expectation(manifest)
    env_dir = task_dir / "environment"
    project = f"sweep{manifest['local_id']}"
    result = {
        "local_id": manifest["local_id"],
        "project": manifest["project"],
        "sanitizer": manifest["sanitizer"],
        "language": manifest["language"],
        "gt_file": gt_file,
        "gt_line": gt_line,
    }

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        json.dump({"services": {"main": {"image": agent_image}}}, fh)
        override = fh.name

    compose = ["docker", "compose", "-p", project, "-f", "docker-compose.yaml", "-f", override]
    try:
        up = subprocess.run(
            [*compose, "up", "-d", "--wait"], cwd=env_dir, capture_output=True, text=True
        )
        if up.returncode != 0:
            result["status"] = "ENV_FAILED"
            result["error"] = (up.stderr or up.stdout)[-400:]
            return result

        probe = subprocess.run(
            [
                *compose, "exec", "-T",
                "-e", f"GT_FILE={gt_file}", "-e", f"GT_LINE={gt_line}",
                "main", "sh", "-c", PROBE,
            ],
            cwd=env_dir, capture_output=True, text=True, timeout=900,
        )
        p = parse_probe(probe.stdout)
        result["probe"] = p

        crash_words = (manifest.get("crash_type") or "").split()
        crash_word = crash_words[0].lower() if crash_words else ""
        checks = {
            "source_staged": int(p.get("src_entries", "0") or 0) > 0,
            "poc_staged": int(p.get("poc_bytes", "0") or 0) > 0,
            "gt_line_matches": (
                p.get("gt_line", "").strip() == expected_text.strip()
                if expected_text.strip()
                else True
            ),
            "no_arvo_bin": p.get("arvo_bin") == "absent",
            "no_src_root": p.get("src_root") == "absent",
            "no_out_dir": p.get("out_dir") == "absent",
            "poc_reproduces": int(p.get("poc_reports_ok", "0") or 0) > 0,
            "symbolized": int(p.get("poc_frames", "0") or 0) > 0,
            "crash_type_matches": bool(crash_word)
            and crash_word in p.get("summary", "").lower(),
        }
        result["checks"] = checks
        result["failed"] = sorted(k for k, ok in checks.items() if not ok and k not in SOFT_CHECKS)
        result["warned"] = sorted(k for k, ok in checks.items() if not ok and k in SOFT_CHECKS)
        attempts_ok = int(p.get("poc_reports_ok", "0") or 0)
        if 0 < attempts_ok < 3:
            result["flaky_poc"] = f"{attempts_ok}/3 runs produced a report"
        result["status"] = "PASS" if not result["failed"] else "FAIL"
        result["expected_gt_text"] = expected_text.strip()
        return result
    except subprocess.TimeoutExpired:
        result["status"] = "TIMEOUT"
        return result
    finally:
        # Tear down before removing the override file: `docker compose -f <gone>`
        # fails, which would silently leak the containers.
        if not keep:
            subprocess.run(
                [*compose, "down", "--volumes", "--remove-orphans"],
                cwd=env_dir, capture_output=True,
            )
        Path(override).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--manifests", type=Path, default=Path("manifests"))
    # Only labels the report: --tasks already selects the config, since each
    # one is its own dataset.
    parser.add_argument("--config", default="main")
    parser.add_argument("--agent-image", default="faultloc-agent:v1")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--keep", action="store_true", help="Leave containers running")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Instances swept in parallel; each is an independent compose project",
    )
    args = parser.parse_args()

    pending = []
    for manifest_path in sorted(args.manifests.glob("*.json")):
        manifest = json.loads(manifest_path.read_text())
        task_dir = args.tasks / f"faultloc__{manifest['local_id']}"
        if not task_dir.exists():
            print(f"{manifest['local_id']}: no generated task, skipping")
            continue
        pending.append((task_dir, manifest))

    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(sweep_one, task_dir, manifest, args.agent_image, args.keep): manifest
            for task_dir, manifest in pending
        }
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            detail = ",".join(r.get("failed", [])) or r.get("error", "")[:60]
            if r.get("flaky_poc"):
                detail = (detail + " " if detail else "") + f"flaky_poc({r['flaky_poc']})"
            if r.get("warned"):
                detail = (detail + " " if detail else "") + "warn:" + ",".join(r["warned"])
            print(
                f"{r['status']:<11} {r['local_id']:<11} {r['project']:<14} "
                f"{r['sanitizer']:<6} {r['language']:<4} {detail}",
                flush=True,
            )

    results.sort(key=lambda r: str(r["local_id"]))
    passed = sum(1 for r in results if r["status"] == "PASS")
    print(f"\n{passed}/{len(results)} passed")
    if args.report:
        args.report.write_text(json.dumps(results, indent=2) + "\n")
        print(f"report written to {args.report}")
    return 0 if results and passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
