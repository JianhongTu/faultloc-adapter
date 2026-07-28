# faultloc-adapter

Converts [FLBench](https://github.com/JianhongTu/FLBench) fault-localization instances
into reproducible [Harbor](https://github.com/harbor-framework/harbor) tasks. Standalone,
so FLBench never has to fork or modify Harbor.

The task is **root-cause localization**: the agent inspects a vulnerable C/C++ source tree
plus crash evidence and writes `prediction.json`, a list of suspicious spans, scored
against ground truth from the cleaned developer patch. No build, no repair.

**Status:** all gates passing — scorer parity (10,508/10,508 archived predictions re-score
identically on all six metrics), config boundaries (4/4), controls (`oracle` 1.0 / `nop`
0.0), environment sweep (17/17 instances), and live agents under Harbor producing valid
predictions. Each is reproducible via the `scripts/` entry named below. Remaining: runtime
profiling to pick a fast-debug subset.

## Quick start

Requirements: `uv`, Docker, and a FLBench checkout. Python 3.12 and Harbor `0.20.0` are
pinned by this repo and installed by `uv sync`.

Always run Harbor through the repo environment (`uv run harbor ...`). A different `harbor`
on `PATH` may be another version — an older one silently ignored `network_mode` and gave
agents full internet access.

```bash
uv sync

# 1. Build the agent image (once; the only image this repo builds)
docker build -t faultloc-agent:v1 agent-image/

# 2. Freeze an instance -> manifests/<id>.json (committed; no DB or network needed later)
uv run python -m faultloc_adapter.freeze --flbench ~/codes/FLBench --task-ids 42470093

# 3. Generate tasks (omit --configs for all four)
uv run faultloc-adapter --task-ids 42470093 --configs main

# 4. Verify the controls
uv run harbor run -p datasets/faultloc-adapter/faultloc__42470093-main -a oracle
uv run harbor run -p datasets/faultloc-adapter/faultloc__42470093-main -a nop
```

`oracle` must score 1.0 and `nop` 0.0 with `prediction_missing = 1`. Anything else is a
defect in the task, not the agent. Generation is deterministic and self-contained — same
manifest, byte-identical task directory, no host paths.

`agent-image/Dockerfile` pins its base by digest and both CLIs by version
(`claude-code@2.1.220`, `codex@0.145.0`). Before a calibration or main experiment, also
freeze the built image by digest (`docker images --no-trunc --format '{{.ID}}'
faultloc-agent:v1`) — the tag itself is mutable.

### Running a real agent

The agent phase is allowlisted and the allowlist is baked into `task.toml`, so a
self-hosted endpoint must be added **at generation time**:

```bash
uv run faultloc-adapter --task-ids 42470093 --configs main --overwrite \
  --allowed-hosts poc my-endpoint.example.com
```

Auth differs per agent, and only `codex` reads a file:

| agent | credential | how Harbor picks it up |
| --- | --- | --- |
| `codex` | API key | `OPENAI_API_KEY` (+ `OPENAI_BASE_URL` to retarget) |
| `codex` | ChatGPT subscription | `CODEX_AUTH_JSON_PATH=~/.codex/auth.json`, uploaded into the container |
| `claude-code` | API key | `ANTHROPIC_API_KEY` |
| `claude-code` | subscription | `CLAUDE_FORCE_OAUTH=1` + `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` |

`claude-code` has **no** file path — Harbor points `CLAUDE_CONFIG_DIR` at a fresh empty
directory and never copies `~/.claude/.credentials.json`.

## Task design

One task per `(instance, config)` for the four FLBench configs `main`, `ablation1`,
`ablation2`, `sanity`. Two compose services:

| Service | Image | Holds |
|---|---|---|
| `main` | `faultloc-agent:v1` | the agent; nothing from ARVO |
| `poc` | `n132/arvo:<id>-vul`, digest-pinned | `/src`, `/out`, `/tmp/poc`, `arvo`; also stages the source |

- **Why split.** Withholding must be structural, not prompt-only. The agent image holds no
  source, PoC or reproducer, so a resource is absent unless staging puts it there.
  `ablation2` needs the PoC runnable but not readable — impossible in one container, easy
  when the reproducer lives behind HTTP.
- **Staging lives in the long-lived sidecar**, not a one-shot service: the shared volume is
  tmpfs and exists only while a container mounts it. It stages into `/shared`, never
  `/src/<project>`, because Docker prepopulates an empty named volume from the image at the
  mount point — which would hand source to configs meant to withhold it.
- **PoC access** is four fixed endpoints (`/health`, `/poc-file`, `/poc`, `/shutdown`) from
  `sidecar/server.py`, written inline into the compose command. No build, no bind mount.
- **Verifier** runs in the agent's container (the scorer is stdlib-only). Harbor uploads
  `tests/` at verify time, after the agent stops, so ground truth never coexists with a
  live agent.
- **Scorer** — `src/faultloc_adapter/scorer/` is a verbatim copy of `flbench.eval`.
  **Do not edit it**; parity is meaningless if the metric is reimplemented. Re-vendor.

### Rewards

The verifier writes `/logs/verifier/reward.json`, so every metric survives as a named
Harbor reward:

```json
{"reward": 1.0, "iou": 1.0, "hunk_recall": 1.0, "hunk_hit": 1,
 "file_recall": 1.0, "line_recall": 1.0, "line_precision": 1.0, "line_f1": 1.0,
 "prediction_missing": 0, "prediction_invalid": 0}
```

`reward` is the headline key Harbor reads; it mirrors `iou`. `hunk_hit` is binary so
pass@k stays meaningful.

**Aggregation deliberately differs from the reference.** FLBench *excludes* instances with
no uploaded prediction; this verifier scores them `0` with `prediction_missing = 1`, so
Harbor's mean includes them. To reproduce historical FLBench aggregates, filter
`prediction_missing = 1`. A valid empty prediction (`[]`) is a genuine scored zero and must
**not** be filtered.

A scorer crash writes no reward file and errors the trial. An infrastructure failure must
never surface as a low score — nothing downstream could distinguish it from a bad agent.

## When running

- **Set `delete: false`** (or `harbor run --no-delete`). Harbor's default teardown runs
  `docker compose down --rmi local --volumes`, which deletes every image in the stack —
  including the digest-pinned ARVO base, a ~3 GB re-pull per trial. Only compose tasks
  reach that branch. A registry does not help; it turns deletion into a re-pull.
- **`unset OPENAI_BASE_URL` before any codex subscription run.** It is read from the
  ambient environment independently of `CODEX_AUTH_JSON_PATH`, so a stray `export` — or a
  sourced `.env` — sends a ChatGPT-authenticated run to the wrong backend without warning.
- **Model names are not portable across auth modes.** `gpt-5.3-codex` is rejected for
  ChatGPT accounts; `~/.codex/models_cache.json` lists what an account may use.
- **Declare environment variables in `environment/docker-compose.yaml`.**
  `[environment.env]` is silently ignored for compose environments.
- **Agents are limited to what `agent-image/` bakes in** (`claude-code`, `codex`).
  `[environment] no-network` applies during `agent.setup()`, so Harbor cannot install one
  at run time; both skip installation when a satisfying binary is present.

## Do not change

Each of these is load-bearing. Removing one reintroduces a failure that is silent — the
run completes and the numbers look plausible.

- **`security_opt: [seccomp=unconfined]` on `poc`.** Docker's default profile denies
  `personality` with EPERM, which turns `sidecar/server.py:_disable_aslr` into a no-op and
  makes msan instances intermittently produce no sanitizer report.
- **`sidecar/server.py:_is_startup_crash` and its retry.** It is the fallback for runtimes
  that ignore `security_opt`; Kubernetes needs `securityContext.seccompProfile: Unconfined`
  instead, and other backends may offer nothing equivalent.
- **`networks: [default]` on `poc`, and `[environment] network_mode = "public"`.** Harbor's
  egress control moves any service declaring neither `networks` nor `network_mode` into the
  egress sidecar's netns, where it loses its DNS name. Staging also fetches the PoC before
  any agent exists, which a `no-network` baseline blocks. The policy that constrains the
  agent is `[agent]`, applied during `agent.run()`.
- **The `src` volume's tmpfs `driver_opts`.** Plain `down` keeps named volumes; disk-backed
  it leaks a source tree per trial (~120 MB, unbounded). `size` is a ceiling, not an
  allocation.
- **`/workspace/.staged` written last, plus `main`'s healthcheck on it.** `up --wait`
  returns when the container is *running*, not when staging finishes; without the gate the
  agent starts against a half-populated `/workspace/src`.
- **`src/faultloc_adapter/scorer/`, a verbatim copy of `flbench.eval`.** Parity is
  meaningless if the metric is reimplemented. Re-vendor from FLBench instead of editing.

**Never set `vm.mmap_rnd_bits` host-wide.** It is not namespaced, so it changes every other
workload on the machine, and at 28 the msan failure cannot occur — any test of the two
mitigations above would pass for the wrong reason. Verify changes to them with the host at
its default of 32.

Generated datasets, Harbor jobs, and trial artifacts are excluded from version control.
