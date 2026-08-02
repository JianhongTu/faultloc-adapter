"""PoC sidecar for both task families: the agent holds no ARVO content and
reaches everything here over HTTP, so withholding is structural.

    GET  /health
    GET  /poc-file    403 under ablation2
    POST /poc         403 under ablation1
    POST /compile     404 without PROJECT (repair only), 403 without the token
    POST /test        404 without TEST_SCRIPT (repair only), 403 without the token

Vendored from FLBench's eval job; /poc-file gating, /compile and /test are ours.
/compile syncs the agent's tree rather than mounting it: arvo builds in-tree and
the volume is tmpfs. See src/faultloc_adapter/adapter.py, compose().
"""
import ctypes, os, subprocess, json
from http.server import HTTPServer, BaseHTTPRequestHandler

POC_SEARCH_PATHS = ["/tmp/poc", "/poc", "/tmp/crash", "/crash"]

SHARED = "/shared"
PROJECT = os.environ.get("PROJECT", "")
BUILD_TREE = "/src/" + PROJECT

REPAIR = bool(PROJECT)

COMPILE_TOKEN = os.environ.get("COMPILE_TOKEN", "")

TEST_SCRIPT = os.environ.get("TEST_SCRIPT", "")
REGRESSION = bool(TEST_SCRIPT) and os.path.isfile(TEST_SCRIPT)

ADDR_NO_RANDOMIZE = 0x0040000

POC_MAX_ATTEMPTS = 5

_SANITIZER_MARKERS = (
    "SUMMARY:", "ERROR: AddressSanitizer", "WARNING: MemorySanitizer",
    "ERROR: libFuzzer", "runtime error:", "Running: ",
)

def _is_startup_crash(returncode, output):
    if returncode not in (-11, 139):
        return False
    return not any(m in output for m in _SANITIZER_MARKERS)

def _disable_aslr():
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
    for root, _, files in os.walk("/tmp"):
        for f in files:
            if f == "poc":
                return os.path.join(root, f)
    return None

def _run(cmd, timeout, cwd=None):
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
    return _run(
        "rm -rf %s && mkdir -p %s && tar -C %s -cf - . | tar -C %s -xf -"
        % (BUILD_TREE, BUILD_TREE, SHARED, BUILD_TREE),
        timeout=300,
    )

def _run_poc(timeout):
    for attempt in range(POC_MAX_ATTEMPTS):
        rc, out = _run("arvo", timeout)
        if not _is_startup_crash(rc, out):
            return rc, out
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
            if os.environ.get("EVAL_CONFIG", "main") == "ablation1":
                self.send_response(403); self.end_headers(); return
            rc, out = _run_poc(int(os.environ.get("TIMEOUT", "120")))
            self._json({"exit_code": rc, "output": out})
        elif self.path == "/compile" and REPAIR:
            if not COMPILE_TOKEN or self.headers.get("X-Compile-Token") != COMPILE_TOKEN:
                self.send_response(403); self.end_headers(); return
            rc, out = _sync_tree()
            if rc != 0:
                self._json({"exit_code": rc, "output": out, "sync_failed": True})
                return
            rc, out = _run(
                "arvo compile", int(os.environ.get("COMPILE_TIMEOUT", "1800")), cwd=BUILD_TREE
            )
            self._json({"exit_code": rc, "output": out, "sync_failed": False})
        elif self.path == "/test" and REGRESSION:
            if not COMPILE_TOKEN or self.headers.get("X-Compile-Token") != COMPILE_TOKEN:
                self.send_response(403); self.end_headers(); return
            rc, out = _sync_tree()
            if rc != 0:
                self._json({"exit_code": rc, "output": out, "sync_failed": True})
                return
            rc, out = _run(
                TEST_SCRIPT, int(os.environ.get("TEST_TIMEOUT", "1800")), cwd=BUILD_TREE
            )
            self._json({"exit_code": rc, "output": out, "sync_failed": False})
        else:
            self.send_response(404); self.end_headers()

if REGRESSION and not REPAIR:
    raise SystemExit("TEST_SCRIPT is set but PROJECT is not; refusing to start")

if REPAIR:
    os.chdir("/")
_disable_aslr()
HTTPServer(("", 8080), Handler).serve_forever()
