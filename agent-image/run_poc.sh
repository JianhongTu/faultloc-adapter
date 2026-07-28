#!/bin/sh
# Run the proof-of-concept crash reproducer in the PoC sidecar.
# Exit 0 means the PoC did NOT crash; non-zero means it still crashes.
# Mirrors FLBench's /tools/run_poc.sh, which POSTed to the same endpoint.
exec python3 - "$@" <<'PY'
import json, sys, urllib.request
try:
    req = urllib.request.Request("http://poc:8080/poc", method="POST", data=b"")
    with urllib.request.urlopen(req, timeout=600) as resp:
        r = json.loads(resp.read())
except urllib.error.HTTPError as e:
    if e.code == 403:
        sys.stderr.write("run_poc.sh is not available in this configuration\n")
        sys.exit(2)
    raise
sys.stdout.write(r.get("output", ""))
sys.exit(r.get("exit_code", 1))
PY
