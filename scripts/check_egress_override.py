#!/usr/bin/env python3
"""Gate: the egress overrides are still faithful copies of Harbor's, and are wired up.

harbor-overrides/gost.yaml and harbor-overrides/network-policy REPLACE Harbor's
egress-sidecar config and its nftables generator at runtime, and those two files
are what enforce `allowed_hosts` -- the bypass wiring, the allowlist path, the
redirect port, the filter chain. Holding a private copy of someone else's security
config is safe exactly as long as it stays in step with theirs, and dangerous the
moment it does not: a stale duplicate would keep enforcing the old rules while the
task still claims `network_mode = "allowlist"`, with nothing to show for it.

So this asserts, for each override:

  * Harbor's shipped file is the revision we vendored from. If Harbor changes it,
    this fails and the override must be re-vendored rather than silently masking
    the change.
  * Our copy differs from Harbor's ONLY by the sanctioned lines -- the raised
    timeouts in gost.yaml, the DNS accept in network-policy. Any other drift -- a
    moved allowlist path, a changed port -- is a weakening of enforcement.

and, once:

  * FAULTLOC_ROOT is set and resolves to this checkout, because compose expands it
    into the bind source and an unset value mounts a directory instead of the file.
  * network-policy is executable, because the bind mount carries the host file's
    mode and the sidecar entrypoint invokes it by name.

    uv run python scripts/check_egress_override.py
"""

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "harbor-overrides" / "egress-timeout.compose.yaml"
SIDECAR = (
    ROOT
    / ".venv/lib/python3.12/site-packages/harbor/environments/docker"
    / "harbor-docker-egress-control-sidecar"
)

# harbor 0.20.0. A sha256 is bumped ONLY together with a re-vendor of its override.
# `additions` are the only lines our copy may add; everything else must match.
OVERRIDES = {
    "gost.yaml": {
        "ours": ROOT / "harbor-overrides" / "gost.yaml",
        "theirs": SIDECAR / "gost.yaml",
        "sha256": "78c18e58efbc7c8e226cb311e2a4cf3863d5a2f3e54b3bf15a0ea4b885345290",
        "additions": {"timeout: 3600s", "readTimeout: 3600s", "metadata:"},
    },
    "network-policy": {
        "ours": ROOT / "harbor-overrides" / "network-policy",
        "theirs": SIDECAR / "bin" / "network-policy",
        "sha256": "1b3b726b21233a28d652e4b693afadeb81c4479b3072b5f2f22fb559a8348120",
        "additions": {"meta l4proto udp udp dport 53 accept"},
    },
}


def _payload(text: str) -> list[str]:
    """Non-comment, non-blank lines, stripped -- comments are ours to write."""
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    for name, spec in OVERRIDES.items():
        if not spec["theirs"].exists():
            print(f"FAIL   harbor {name} not found at {spec['theirs']}")
            return 1

        upstream = spec["theirs"].read_text()
        got = hashlib.sha256(upstream.encode()).hexdigest()
        results.append((
            f"harbor_{name}_unchanged", got == spec["sha256"],
            f"upstream sha256={got} expected={spec['sha256']}; re-vendor the override",
        ))

        ours = _payload(spec["ours"].read_text())
        theirs = _payload(upstream)
        # Our copy is upstream plus the sanctioned lines: every upstream line must
        # survive, and every extra line must be one of the sanctioned additions.
        missing = [ln for ln in theirs if ln not in ours]
        unsanctioned = [ln for ln in ours if ln not in theirs and ln not in spec["additions"]]
        results.append((
            f"{name}_upstream_intact", not missing, f"dropped from upstream: {missing[:4]}",
        ))
        results.append((
            f"{name}_only_sanctioned_additions", not unsanctioned,
            f"unsanctioned: {unsanctioned[:4]}",
        ))

    policy = OVERRIDES["network-policy"]["ours"]
    results.append((
        "network_policy_executable", policy.stat().st_mode & 0o111 != 0,
        f"chmod +x {policy}  (the bind mount carries this mode into the sidecar)",
    ))

    root_env = os.environ.get("FAULTLOC_ROOT", "")
    ok_root = bool(root_env) and Path(root_env).resolve() == ROOT
    results.append((
        "faultloc_root_set", ok_root,
        f"FAULTLOC_ROOT={root_env!r}; must be {ROOT}  (export FAULTLOC_ROOT=\"$PWD\")",
    ))

    results.append(("compose_override_present", COMPOSE.exists(), f"missing {COMPOSE}"))

    failed = 0
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}   {name}{'' if ok else '  ' + detail}")
        failed += not ok
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
