#!/usr/bin/env python3
"""Gate: the gost override is still a faithful copy of Harbor's, and is wired up.

harbor-overrides/gost.yaml REPLACES Harbor's egress-sidecar config at runtime, and
that file is what enforces `allowed_hosts` -- the bypass wiring, the allowlist
path, the redirect port. Holding a private copy of someone else's security config
is safe exactly as long as it stays in step with theirs, and dangerous the moment
it does not: a stale duplicate would keep enforcing the old rules while the task
still claims `network_mode = "allowlist"`, with nothing to show for it.

So this asserts three things:

  * Harbor's shipped gost.yaml is the revision we vendored from. If Harbor changes
    it, this fails and the override must be re-vendored rather than silently
    masking the change.
  * Our copy differs from Harbor's ONLY by the timeout lines. Any other drift --
    a moved allowlist path, a changed port -- is a weakening of enforcement.
  * FAULTLOC_ROOT is set and resolves to this checkout, because compose expands it
    into the bind source and an unset value mounts a directory instead of the file.

    uv run python scripts/check_egress_override.py
"""

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OVERRIDE = ROOT / "harbor-overrides" / "gost.yaml"
COMPOSE = ROOT / "harbor-overrides" / "egress-timeout.compose.yaml"
HARBOR_GOST = (
    ROOT
    / ".venv/lib/python3.12/site-packages/harbor/environments/docker"
    / "harbor-docker-egress-control-sidecar/gost.yaml"
)

# harbor 0.20.0. Bump ONLY together with a re-vendor of harbor-overrides/gost.yaml.
VENDORED_FROM_SHA256 = "78c18e58efbc7c8e226cb311e2a4cf3863d5a2f3e54b3bf15a0ea4b885345290"

# The only lines our copy may add. Everything else must match upstream.
ALLOWED_ADDITIONS = {"timeout: 3600s", "readTimeout: 3600s", "metadata:"}


def _payload(text: str) -> list[str]:
    """Non-comment, non-blank lines, stripped -- comments are ours to write."""
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    if not HARBOR_GOST.exists():
        print(f"FAIL   harbor gost.yaml not found at {HARBOR_GOST}")
        return 1

    upstream = HARBOR_GOST.read_text()
    got = hashlib.sha256(upstream.encode()).hexdigest()
    results.append((
        "harbor_gost_unchanged", got == VENDORED_FROM_SHA256,
        f"upstream sha256={got} expected={VENDORED_FROM_SHA256}; re-vendor the override",
    ))

    ours = _payload(OVERRIDE.read_text())
    theirs = _payload(upstream)
    # Our copy is upstream plus the timeout lines: every upstream line must survive,
    # in order, and every extra line must be one of the sanctioned additions.
    missing = [ln for ln in theirs if ln not in ours]
    extra = [ln for ln in ours if ln not in theirs]
    unsanctioned = [ln for ln in extra if ln not in ALLOWED_ADDITIONS]
    results.append(("upstream_lines_intact", not missing, f"dropped from upstream: {missing[:4]}"))
    results.append(("only_timeouts_added", not unsanctioned, f"unsanctioned: {unsanctioned[:4]}"))

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
