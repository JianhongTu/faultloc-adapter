# faultloc-adapter

Converts [FLBench](https://github.com/JianhongTu/FLBench) fault-localization instances
into reproducible [Harbor](https://github.com/harbor-framework/harbor) tasks. Standalone,
so FLBench never has to fork or modify Harbor.

The task is **root-cause localization**: the agent inspects a vulnerable C/C++ source tree
plus crash evidence and writes `prediction.json`, a list of suspicious spans, scored
against ground truth from the cleaned developer patch. No build, no repair.

**Status:** all gates passing — scorer parity (10,508/10,508 archived predictions re-score
identically on all six metrics), config boundaries (4/4), controls (`oracle` 1.0 / `nop`
0.0), environment sweep (17/17 instances, every one 3/3 on the PoC path), and live agents
under Harbor producing valid predictions. Each is reproducible via the `scripts/` entry
named below. Remaining: runtime profiling to pick a fast-debug subset.

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

## Notes

Each of these cost a failed run.

- **msan PoC flake — fixed in two layers; keep both.** MSan reserves fixed shadow ranges at
  startup. On hosts with `vm.mmap_rnd_bits=32` (Ubuntu 6.8 default; formerly 28) the loader
  intermittently lands a mapping inside one and the target dies of SIGSEGV *during MSan
  init*, before libFuzzer's first line — measured 11/40 runs on 42470668, 0/40 with ASLR
  off. The sysctl is not namespaced, so a container cannot set its own; **do not** set it
  host-wide, which changes every other workload and makes the bug unreproducible.
  *Layer 1:* `sidecar/server.py:_disable_aslr` calls `personality(ADDR_NO_RANDOMIZE)` once
  at startup (inherited by every child). This needs `security_opt: [seccomp=unconfined]` on
  `poc` — **load-bearing, not hardening**: Docker's default profile denies `personality`
  with EPERM, making the call a silent no-op (20/20 with the line, 17/20 without).
  *Layer 2:* `_is_startup_crash` retries the run, because Layer 1 depends on the runtime
  honoring `security_opt` — Kubernetes needs `securityContext.seccompProfile: Unconfined`
  and other backends may ignore it, degrading **silently**. The signature is *absence of a
  sanitizer report* on a signal death, not empty output (`arvo` is a shell script; its own
  "Segmentation fault" notice lands in the captured output). Retrying cannot corrupt a
  result: a deterministic outcome reproduces and is returned unchanged. Verified at
  `mmap_rnd_bits=32`, 30 calls each: retry-only 30/30 with 7 retries fired; retry +
  personality 30/30 with 0. **Re-verify any change with the host at 32** — at 28 the bug
  cannot occur and the test passes for the wrong reason.
- **`OPENAI_BASE_URL` overrides subscription auth.** Read from the ambient environment
  independently of `CODEX_AUTH_JSON_PATH`, so a stray `export` — or a sourced `.env` —
  silently sends a ChatGPT-authenticated run to the wrong backend. `unset` it first. Model
  names are not portable across auth modes either: `gpt-5.3-codex` is rejected for ChatGPT
  accounts; see `~/.codex/models_cache.json` for what an account may use.
- **`up --wait` returns when the container is *running*, not when its command finishes.**
  Without a gate the agent starts against a half-populated `/workspace/src` and silently
  scores like a bad agent. Staging writes `/workspace/.staged` last and `main` healthchecks
  on it. Small projects hide this; it surfaced only on the multi-project sweep.
- **Harbor's default teardown deletes every image in a compose stack.** `delete: true` runs
  `docker compose down --rmi local --volumes`, which removed both `faultloc-agent:v1` and
  the digest-pinned ARVO base — a ~3 GB re-pull per trial. Only compose tasks reach that
  branch. Use `delete: false` or `--no-delete`. A registry does not fix this; it converts
  deletion into a re-pull.
- **Plain `down` keeps named volumes**, so the shared source volume is tmpfs-backed
  (`driver_opts: {type: tmpfs, ...}`). Disk-backed leaked ~120 MB per trial, unbounded
  (measured 2.3 GB over 21 trials). `size` is a ceiling, not an allocation.
- **Harbor's egress control collapses compose services into one network namespace.** A
  service declaring neither `networks` nor `network_mode` moves into the egress sidecar's
  netns and loses its DNS name, so `main` could not resolve `poc` (curl exit 6). `poc`
  therefore declares `networks: [default]`. Relatedly `[environment]` must stay `public` —
  staging fetches the PoC before any agent exists, and `no-network` blocks it (curl exit
  52). The policy that matters is `[agent]`, applied during `agent.run()`.
- **`[environment.env]` is silently ignored for compose environments.** Declare variables,
  `PATH` included, in `environment/docker-compose.yaml`.
- **Agent choice is constrained to what the agent image bakes in** (`claude_code`,
  `codex`), since `[environment] no-network` applies during `agent.setup()`. Both skip
  installation when a satisfying binary is present.
- **`docker compose up` only builds when the image is missing** — editing an embedded
  sidecar and re-running silently reuses the cached image unless `--build` is passed.

Generated datasets, Harbor jobs, and trial artifacts are excluded from version control.
