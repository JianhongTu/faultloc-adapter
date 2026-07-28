"""Profile `arvo compile` per instance: the input to fast-subset selection.

A repair agent compiles in a loop, so build wall-clock, not PoC runtime, is what
bounds a trial. This measures it against the pristine ARVO image -- no Harbor, no
staging -- which is deliberately the cheapest possible signal: an instance that is
slow here is slow everywhere, and one that fails here can be dropped before
scripts/repair_controls.py spends a compose stack on it.

Also flags build scripts that fetch from the network. Those still build (the
sidecar's baseline is `public`), but they depend on a third-party host being up
during the experiment, which is a stability risk worth knowing about up front.

    python scripts/repair_profile.py --report profile.json
"""

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faultloc_adapter import manifest as manifest_mod  # noqa: E402

PROBE = r"""
d=/src/$PROJ
[ -d "$d" ] || { echo "no_src_dir=1"; exit 9; }
echo "fetches=$(grep -cE 'git clone|wget |curl |pip install|apt-get install' /src/build.sh 2>/dev/null || echo 0)"
echo "mb_before=$(du -sm "$d" | cut -f1)"
S=$(date +%s); arvo compile >/tmp/c.log 2>&1; rc=$?; E=$(date +%s)
echo "compile_rc=$rc"
echo "compile_sec=$((E-S))"
echo "mb_after=$(du -sm "$d" | cut -f1)"
"""


def profile_one(manifest: dict, timeout: int) -> dict:
    result = {
        "local_id": manifest["local_id"],
        "project": manifest["project"],
        "sanitizer": manifest["sanitizer"],
    }
    try:
        proc = subprocess.run(
            [
                "docker", "run", "--rm", "--security-opt", "seccomp=unconfined",
                "-e", f"PROJ={manifest['project']}",
                "--entrypoint", "sh", manifest_mod.pinned_image(manifest), "-lc", PROBE,
            ],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result["status"] = "TIMEOUT"
        return result

    for line in proc.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    result["status"] = "OK" if result.get("compile_rc") == "0" else "COMPILE_FAILED"
    if result["status"] == "COMPILE_FAILED":
        result["error"] = proc.stdout[-300:]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifests", type=Path, default=Path("manifests"))
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()

    manifests = [manifest_mod.load(p) for p in sorted(args.manifests.glob("*.json"))]
    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(profile_one, m, args.timeout) for m in manifests]
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            grew = int(r.get("mb_after", 0) or 0) - int(r.get("mb_before", 0) or 0)
            note = " fetches-at-build" if r.get("fetches", "0") != "0" else ""
            print(
                f"{r['status']:<15} {r['local_id']:<11} {r['project']:<14} "
                f"{r.get('compile_sec', '?'):>4}s  +{grew}MB{note}",
                flush=True,
            )

    results.sort(key=lambda r: int(r.get("compile_sec", 10**6) or 10**6))
    ok = [r for r in results if r["status"] == "OK"]
    print(f"\n{len(ok)}/{len(results)} compile clean")
    if ok:
        secs = [int(r["compile_sec"]) for r in ok]
        print(f"compile seconds: min {min(secs)}, median {sorted(secs)[len(secs) // 2]}, max {max(secs)}")
        print("fastest 5: " + ", ".join(f"{r['local_id']}({r['compile_sec']}s)" for r in ok[:5]))
    if args.report:
        args.report.write_text(json.dumps(results, indent=2) + "\n")
        print(f"report written to {args.report}")
    return 0 if ok and len(ok) == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
