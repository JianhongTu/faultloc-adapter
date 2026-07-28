"""PoC sidecar: serves the reproducer and its input to the agent container.

Vendored from FLBench's eval job (k8s/eval-job.yaml, sidecar service) so the PoC
execution path is the one the reference used. Do not rewrite it; re-vendor from
FLBench and re-apply the divergence below.

Endpoints: GET /health, GET /poc-file, POST /poc, POST /shutdown.

INTENTIONAL DIVERGENCE from the reference: /poc-file is gated on EVAL_CONFIG.
Upstream gates only /poc (ablation1), leaving /poc-file open -- so an ablation2
agent, which is supposed to have no PoC input, can fetch the exact bytes from the
sidecar over the network. Verified: 2648 bytes retrieved on instance 42470093.
Withholding a file from the filesystem is not withholding it when a network path
serves the same bytes.
"""
import ctypes, os, subprocess, json, threading
from http.server import HTTPServer, BaseHTTPRequestHandler

POC_SEARCH_PATHS = ["/tmp/poc", "/poc", "/tmp/crash", "/crash"]

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
    re-rolls an outcome that was nondeterministic to begin with.
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

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

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
        if self.path == "/shutdown":
            resp = b'{"exit_code": 0, "output": ""}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path == "/poc":
            # Block execution for PoC-only config: agent may inspect
            # the binary but must not use our tool to run it.
            config = os.environ.get("EVAL_CONFIG", "main")
            if config == "ablation1":
                self.send_response(403)
                self.end_headers()
                return
            try:
                timeout = int(os.environ.get("TIMEOUT", "120"))
                for attempt in range(POC_MAX_ATTEMPTS):
                    r = subprocess.run(
                        ["timeout", str(timeout), "arvo"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        timeout=timeout + 30,
                    )
                    r.stdout = r.stdout.decode(errors="replace")
                    if not _is_startup_crash(r.returncode, r.stdout):
                        break
                    # flush: the sidecar's stdout is a pipe, so buffered output would
                    # not reach `docker compose logs` until the process exits.
                    print("retry %d: target died during sanitizer init"
                          % (attempt + 1), flush=True)
            except subprocess.TimeoutExpired as e:
                r = type("r", (), {
                    "returncode": -1,
                    "stdout": (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
                })()
            except Exception as e:
                r = type("r", (), {"returncode": -1, "stdout": str(e)})()
            resp = json.dumps({
                "exit_code": r.returncode,
                "output": r.stdout,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_response(404); self.end_headers()

_disable_aslr()
HTTPServer(("", 8080), Handler).serve_forever()
