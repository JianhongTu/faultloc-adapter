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
import os, subprocess, json, threading
from http.server import HTTPServer, BaseHTTPRequestHandler

POC_SEARCH_PATHS = ["/tmp/poc", "/poc", "/tmp/crash", "/crash"]

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
                r = subprocess.run(
                    ["timeout", str(timeout), "arvo"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    timeout=timeout + 30,
                )
                r.stdout = r.stdout.decode(errors="replace")
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

HTTPServer(("", 8080), Handler).serve_forever()
