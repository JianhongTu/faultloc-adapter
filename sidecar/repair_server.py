"""Repair sidecar: builds and runs the agent's edited source tree.

Companion to sidecar/server.py, which serves the localization task. That file is
vendored verbatim from FLBench and must not grow task-specific endpoints, so the
repair task gets its own server. The PoC execution path below -- ASLR handling,
the startup-crash retry, the /poc response shape -- is deliberately identical;
keep the two in step when either changes.

Endpoints: GET /health, POST /compile, POST /poc, POST /shutdown.

/compile syncs the agent's tree onto the ARVO build tree and runs `arvo compile`.
The agent never sees this filesystem; it reaches both endpoints over HTTP.

WHY A SYNC RATHER THAN A SHARED MOUNT. Mounting the volume straight onto
/src/<project> also works -- Docker prepopulates the empty volume from the image,
verified on 42508282 -- but `arvo compile` builds in-tree (miniz: 2.1MB of source
becomes 46MB), and that volume is tmpfs, so every build would sit in RAM and every
`git diff` in the agent's tree would be buried in object files. Syncing instead
keeps build output in the container's own filesystem and the agent's tree pristine.
The invariant the shared mount was there to provide is preserved: what gets
compiled is whatever is in the agent's tree at the moment of the call, and the
verifier compiles through this same endpoint, so it cannot diverge from what the
agent built.
"""
import ctypes, os, subprocess, json, threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# Where the agent's tree is mounted here, and the ARVO tree `arvo compile` builds.
SHARED = "/shared"
BUILD_TREE = "/src/" + os.environ.get("PROJECT", "")

ADDR_NO_RANDOMIZE = 0x0040000

# Attempts for the startup-crash signature below. The crash is independent per run,
# so at the worst rate measured (~27%) five attempts leave a ~0.14% residual.
POC_MAX_ATTEMPTS = 5

# A run that reached the target prints one of these. The `arvo` wrapper is a shell
# script, so its own "Segmentation fault" notice still lands in the captured output --
# emptiness is not the signature, absence of a sanitizer report is.
_SANITIZER_MARKERS = (
    "SUMMARY:", "ERROR: AddressSanitizer", "WARNING: MemorySanitizer",
    "ERROR: libFuzzer", "runtime error:", "Running: ",
)

def _is_startup_crash(returncode, output):
    """True for the MSan/ASLR init crash, false for every real crash.

    The target dies of SIGSEGV before libFuzzer prints its first line, so the run
    yields no sanitizer output at all. A genuine crash -- including a genuine SEGV,
    which several instances have -- always emits a report first.

    This matters more here than in localization: a repair is scored on whether the
    PoC still crashes, so one unretried startup crash reads as "the fix did not
    work" and silently costs the agent a success.
    """
    # 139 when `timeout` reports the signal as 128+11; -11 if reaped directly.
    if returncode not in (-11, 139):
        return False
    return not any(m in output for m in _SANITIZER_MARKERS)

def _disable_aslr():
    """Turn off address-space randomization for this process and its children.

    MSan reserves fixed shadow ranges at startup (shadow-2 at 0x10000000000 etc).
    On hosts with vm.mmap_rnd_bits=32 -- the default on Ubuntu's 6.8 kernel -- the
    loader intermittently places a mapping inside one of those ranges, and the
    target dies of SIGSEGV during MSan init, before libFuzzer prints its first
    line. The personality bit is inherited across fork/exec, so setting it once
    here covers every PoC run.

    Requires `seccomp=unconfined` on this service; under Docker's default profile
    the syscall returns EPERM. Best-effort: a failure only restores the old flake.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.personality(ADDR_NO_RANDOMIZE) == -1:
            print("warn: personality(ADDR_NO_RANDOMIZE) failed, msan may be flaky")
    except Exception as e:
        print("warn: could not disable ASLR: %s" % e)

def _run(cmd, timeout, cwd="/"):
    """Run a shell command, returning (exit_code, combined output).

    cwd defaults to / rather than being inherited: the ARVO images set WORKDIR to
    the project directory, and _sync_tree deletes it. A child inheriting that cwd
    starts in a directory that no longer exists and dies on getcwd.
    """
    try:
        r = subprocess.run(
            ["timeout", str(timeout), "sh", "-c", cmd], cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout + 30,
        )
        return r.returncode, r.stdout.decode(errors="replace")
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""
        return -1, (out.decode(errors="replace") if isinstance(out, bytes) else str(out))
    except Exception as e:
        return -1, str(e)

def _sync_tree():
    """Replace the build tree with the agent's tree, from scratch.

    The wipe is not an optimization to remove later -- it is what makes repeat
    compiles work. Keeping prior build output around looks like a free incremental
    build, but oss-fuzz build scripts are written to run once in a fresh image and
    are not all idempotent: miniz's does a bare `mkdir build` and fails on the
    second call with "File exists". The agent compiles in a loop, so that lands on
    every call after the first, and the verifier's own compile is never the first.

    Wiping also makes the invariant exact rather than approximate: the tree that
    gets built is byte-for-byte the tree the agent has, with no residue from an
    earlier attempt. Full rebuilds cost 3-52s across the frozen instances
    (scripts/repair_profile.py), which is not worth trading correctness for.
    """
    return _run(
        "rm -rf %s && mkdir -p %s && tar -C %s -cf - . | tar -C %s -xf -"
        % (BUILD_TREE, BUILD_TREE, SHARED, BUILD_TREE),
        timeout=300,
    )

def _run_poc(timeout):
    """Run the reproducer, retrying only the sanitizer-init crash signature."""
    for attempt in range(POC_MAX_ATTEMPTS):
        rc, out = _run("arvo", timeout)
        if not _is_startup_crash(rc, out):
            return rc, out
        # flush: the sidecar's stdout is a pipe, so buffered output would not
        # reach `docker compose logs` until the process exits.
        print("retry %d: target died during sanitizer init" % (attempt + 1), flush=True)
    return rc, out

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.end_headers()
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/shutdown":
            self._json({"exit_code": 0, "output": ""})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        elif self.path == "/compile":
            rc, out = _sync_tree()
            if rc != 0:
                # A sync failure is infrastructure, not a failed build: report it
                # distinctly so the verifier can error the trial instead of
                # recording a compile failure the agent did not cause.
                self._json({"exit_code": rc, "output": out, "sync_failed": True})
                return
            # From the project directory, matching the ARVO image's WORKDIR: some
            # build scripts are relative to it.
            rc, out = _run(
                "arvo compile", int(os.environ.get("COMPILE_TIMEOUT", "1800")), cwd=BUILD_TREE
            )
            self._json({"exit_code": rc, "output": out, "sync_failed": False})
        elif self.path == "/poc":
            rc, out = _run_poc(int(os.environ.get("TIMEOUT", "120")))
            self._json({"exit_code": rc, "output": out})
        else:
            self.send_response(404); self.end_headers()

_disable_aslr()
HTTPServer(("", 8080), Handler).serve_forever()
