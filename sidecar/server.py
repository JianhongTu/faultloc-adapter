"""PoC sidecar: serves the reproducer, its input, and -- for repair -- the build.

One sidecar for both task families. The agent container holds no ARVO content of
its own and reaches everything here over HTTP, which is what makes withholding a
resource structural rather than prompt-only.

Endpoints:

    GET  /health
    GET  /poc-file    403 under ablation2
    POST /poc         403 under ablation1
    POST /compile     404 unless PROJECT is set (repair only)
                      403 without the verifier's X-Compile-Token

The PoC execution path is vendored from FLBench's eval job (k8s/eval-job.yaml,
sidecar service) so localization measures what the reference measured. Re-vendor
from FLBench and re-apply the divergences below if it ever changes upstream.

DIVERGENCE 1 -- /poc-file is gated on EVAL_CONFIG. Upstream gates only /poc
(ablation1), leaving /poc-file open, so an ablation2 agent -- which is supposed to
have no PoC input -- can fetch the exact bytes over the network. Verified: 2648
bytes retrieved on instance 42470093. Withholding a file from the filesystem is
not withholding it when a network path serves the same bytes.

DIVERGENCE 2 -- /compile, and one server rather than two. The repair task needs a
build endpoint the localization task must not have, and it used to live in a
second file that copied this one's PoC path. The copy is what let the two
workspaces drift apart: repair silently lost /poc-file, so its agent could run the
reproducer but not read it, while the diagnosis agent could do both. Gating one
implementation is checkable; keeping two in step was not.

WHY /compile SYNCS RATHER THAN SHARES A MOUNT. Mounting the volume straight onto
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
import ctypes, os, subprocess, json
from http.server import HTTPServer, BaseHTTPRequestHandler

POC_SEARCH_PATHS = ["/tmp/poc", "/poc", "/tmp/crash", "/crash"]

# Where the agent's tree is mounted here, and the ARVO tree `arvo compile` builds.
SHARED = "/shared"
PROJECT = os.environ.get("PROJECT", "")
BUILD_TREE = "/src/" + PROJECT

# Repair sets PROJECT; localization does not. It is the one switch: it enables
# /compile and nothing else, so a localization task cannot build even by reaching
# the endpoint directly.
REPAIR = bool(PROJECT)

# Shared with the verifier through tests/compile_token, which Harbor uploads only
# at verify time -- so the agent, which can reach this port and is meant to, has
# no way to present it while it is running. Empty means no one may compile: a task
# generated without the token fails closed rather than opening the build to the
# agent. See repair.py, COMPILE_TOKEN_HEADER.
COMPILE_TOKEN = os.environ.get("COMPILE_TOKEN", "")

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

    Retrying on this signature cannot corrupt a result: a deterministic outcome
    reproduces on every attempt and is returned unchanged, so the retry only ever
    re-rolls an outcome that was nondeterministic to begin with. It matters most
    for repair, which is scored on whether the PoC still crashes: one unretried
    startup crash reads as "the fix did not work" and costs the agent a success.
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
    line. Measured on 42470668 (open62541, msan): 11/40 runs segfault with ASLR
    on, 0/40 with it off. The personality bit is inherited across fork/exec, so
    setting it once here covers every PoC run.

    Requires `seccomp=unconfined` on this service; under Docker's default profile
    the syscall returns EPERM. Best-effort: a failure only restores the old flake.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.personality(ADDR_NO_RANDOMIZE) == -1:
            print("warn: personality(ADDR_NO_RANDOMIZE) failed, msan may be flaky")
    except Exception as e:
        print("warn: could not disable ASLR: %s" % e)

def _find_poc_file():
    for p in POC_SEARCH_PATHS:
        if os.path.isfile(p):
            return p
    # fall back: search common dirs for a file named 'poc'
    for root, _, files in os.walk("/tmp"):
        for f in files:
            if f == "poc":
                return os.path.join(root, f)
    return None

def _run(cmd, timeout, cwd=None):
    """Run a shell command, returning (exit_code, combined output)."""
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

    The wipe makes the invariant exact rather than approximate: the tree that gets
    built is byte-for-byte the tree the agent has, with nothing left over from the
    image or an earlier call.

    It is also the only thing that makes a second compile possible at all. OSS-Fuzz
    build scripts are written to run once in a fresh image and are not all
    idempotent -- miniz's does a bare `mkdir build` and fails with "File exists" on
    the second call. One compile per trial is the current design, so nothing
    depends on that today, but any retry or return to an iterating agent would hit
    it immediately. Full rebuilds cost 3-161s across the frozen instances, which is
    not worth trading correctness for.
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
        elif self.path == "/poc-file":
            # ablation2 withholds the PoC input; see module docstring.
            if os.environ.get("EVAL_CONFIG", "main") == "ablation2":
                self.send_response(403); self.end_headers(); return
            poc_path = _find_poc_file()
            if poc_path is None:
                self.send_response(404); self.end_headers(); return
            data = open(poc_path, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/poc":
            # Block execution for PoC-only config: agent may inspect
            # the binary but must not use our tool to run it.
            if os.environ.get("EVAL_CONFIG", "main") == "ablation1":
                self.send_response(403); self.end_headers(); return
            rc, out = _run_poc(int(os.environ.get("TIMEOUT", "120")))
            self._json({"exit_code": rc, "output": out})
        elif self.path == "/compile" and REPAIR:
            if not COMPILE_TOKEN or self.headers.get("X-Compile-Token") != COMPILE_TOKEN:
                self.send_response(403); self.end_headers(); return
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
        else:
            self.send_response(404); self.end_headers()

if REPAIR:
    # The ARVO images set WORKDIR to the project directory and _sync_tree deletes
    # it, so a child inheriting that cwd would start in a directory that no longer
    # exists and die on getcwd. Localization never syncs and keeps the image's
    # WORKDIR, which is the cwd the reference ran the reproducer from.
    os.chdir("/")
_disable_aslr()
HTTPServer(("", 8080), Handler).serve_forever()
