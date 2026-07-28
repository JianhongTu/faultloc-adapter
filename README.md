# faultloc-adapter

`faultloc-adapter` converts [FLBench](https://github.com/JianhongTu/FLBench)
fault-localization instances into reproducible
[Harbor](https://github.com/harbor-framework/harbor) tasks. It is maintained as a
standalone adapter so FLBench does not need to fork or modify the Harbor framework.

The benchmark task is **root-cause localization**: an agent inspects a vulnerable C/C++
source tree plus crash evidence and writes `prediction.json`, a list of suspicious spans.
It is scored against ground truth extracted from the cleaned developer patch. The agent
never builds anything and never produces a repair.

## Status

Gates in [`doc/migration-parity-plan.md`](doc/migration-parity-plan.md):

- **Scorer parity — passing.** 10,508/10,508 archived FLBench predictions re-score to
  identical values on all six metrics (`doc/scorer-parity-report.json`). Offline, so it
  is unaffected by environment changes.
- **Config boundaries — passing.** 4/4 configs withhold what they claim, verified from
  inside the agent container on both the filesystem and the network
  (`scripts/config_boundaries.py`).
- **Controls — passing.** `oracle` 1.0 and `nop` 0.0 with `prediction_missing = 1`,
  across all four configs on instance 42470093.
- **Environment sweep — passing.** 17/17 instances across 17 projects, 3 sanitizers and
  both languages (`doc/env-sweep-report.json`).

Fixed: `run_poc.sh` used to be unreliable on msan -- 4 of 5 msan instances produced a
symbolized report on only 1-2 of 3 attempts, versus 0 of 12 for asan and ubsan. The cause
was ASLR, not the adapter: MSan reserves fixed shadow ranges at startup, and on hosts with
`vm.mmap_rnd_bits=32` (Ubuntu's 6.8 kernel default) the loader intermittently lands a
mapping inside one, killing the target with SIGSEGV before libFuzzer prints its first
line. Measured on 42470668: 11/40 runs segfault with ASLR on, 0/40 with it off, same
container. The sidecar now clears the randomization bit at startup and every PoC run
inherits it; all five msan instances went 3/3 in the sweep and 20/20 under a dedicated
100-run replay. See `sidecar/server.py:_disable_aslr`.

Remaining: runtime profiling to pick the fast-debug subset.

## Task design

Each task is one `(instance, config)` pair, for the four FLBench configs `main`,
`ablation1`, `ablation2`, and `sanity`.

Three compose services, mirroring FLBench's eval job:

| Service | Image | Holds |
|---|---|---|
| `main` | `faultloc-agent:v1` (`agent-image/`) | the agent; nothing from ARVO |
| `poc` | `n132/arvo:<id>-vul`, pinned by digest | `/src`, `/out`, `/tmp/poc`, `arvo`; also stages the source |

- **Why split.** Withholding must be structural. The agent image contains no source, PoC
  or reproducer, so a resource is absent unless staging puts it there. Running the agent
  *inside* the ARVO image cannot express this: `ablation2` needs the PoC runnable but not
  readable, which one container cannot do.
- **Source staging lives in the sidecar, not a one-shot service.** The shared volume is
  tmpfs, and a tmpfs exists only while a container has it mounted — a write-then-exit
  service loses its work the instant it exits. `poc` is up for the whole trial, so it
  holds the mount. It stages into `/shared`, never `/src/<project>`, because Docker
  prepopulates an empty named volume from the image at the mount point and that would
  hand the source to configs meant to withhold it.
- **PoC access** — the agent reaches the reproducer only through four fixed endpoints
  (`/health`, `/poc-file`, `/poc`, `/shutdown`) served by `sidecar/server.py`, written
  inline into the compose command. No build, no bind mount, no registry.
- **Verifier** — runs in the agent's container. The scorer is 378 lines of stdlib Python
  and the agent image ships `python3`, so a separate verifier image only added a per-task
  build. Harbor uploads `tests/` at verify time, after the agent stops, so the ground
  truth never coexists with a live agent.
- **Scorer** — `src/faultloc_adapter/scorer/` is a verbatim copy of `flbench.eval`'s
  `types.py`, `ground_truth.py`, and `metrics.py`. **Do not edit it**; parity is
  meaningless if the metric is reimplemented. Re-vendor from FLBench instead.

### Rewards

The verifier writes `/logs/verifier/reward.json`, so all metrics survive as named Harbor
rewards rather than a single scalar:

```json
{"reward": 1.0, "iou": 1.0, "hunk_recall": 1.0, "hunk_hit": 1,
 "file_recall": 1.0, "line_recall": 1.0, "line_precision": 1.0, "line_f1": 1.0,
 "prediction_missing": 0, "prediction_invalid": 0}
```

`reward` is the headline key Harbor's telemetry and leaderboards read; it mirrors `iou`.
`hunk_hit` is binary, so pass@k stays meaningful despite a continuous headline reward.

**Aggregation differs from the reference, deliberately.** FLBench's scorer *excludes*
instances with no uploaded prediction; this verifier records them as reward `0` with
`prediction_missing = 1`, so Harbor's default mean includes them. The denominators are
therefore not interchangeable. To reproduce historical FLBench localization aggregates,
filter out `prediction_missing = 1` rows. A valid empty prediction (`[]`) is a genuine
scored zero and must **not** be filtered. `prediction_invalid = 1` marks malformed
output, which the reference also scored as empty.

An unexpected scorer crash is treated as an infrastructure failure, not a score: the
verifier writes no reward file and Harbor errors the trial. That keeps infrastructure
failures out of reported performance, as `doc/migration-parity-plan.md` requires.

## Usage

Requirements: `uv`, Docker, and a FLBench checkout. Everything else is pinned by this
repo — Python 3.12 (`.python-version`) and Harbor `0.20.0` (a dev dependency), both
installed into `.venv` by:

```bash
uv sync
```

Run Harbor through the repo's environment so the pinned version is the one that
executes: `uv run harbor ...` or `.venv/bin/harbor ...`. A globally installed `harbor`
on `PATH` may be a different version — an older one silently ignored this repo's
`network_mode` settings and ran agents with full internet access.

**1. Build the agent image** (once — it is the only image this repo builds):

```bash
docker build -t faultloc-agent:v1 agent-image/
```

`agent-image/Dockerfile` pins its base by digest and both agent CLIs by version
(`claude-code@2.1.220`, `codex@0.145.0`, Node 22.23.1), so the harness cannot drift
between runs and confound a measured difference between conditions. **Before a
calibration or main experiment, freeze the built image by digest too** and record it
alongside the results — the local tag `faultloc-agent:v1` is mutable:

```bash
docker images --no-trunc --format '{{.ID}}' faultloc-agent:v1
```

**2. Freeze instances.** A manifest is the single frozen input for one instance — image
digest, crash metadata, the pre-captured sanitizer report, and the ground-truth patch —
so task generation is reproducible without FLBench's database or the network. Manifests
are committed.

```bash
uv run python -m faultloc_adapter.freeze \
  --flbench ~/codes/FLBench --task-ids 42470093
```

**3. Generate tasks.**

```bash
uv run faultloc-adapter \
  --task-ids 42470093 \
  --configs main \
  --output-dir datasets/faultloc-adapter
```

Omit `--configs` for all four. Generation is deterministic: the same manifest yields the
same task directory, and a task is self-contained — no host paths, no build context.

**4. Run.**

```bash
uv run harbor run -p datasets/faultloc-adapter/faultloc__42470093-main -a oracle
uv run harbor run -p datasets/faultloc-adapter/faultloc__42470093-main -a nop
```

`oracle` must score 1.0 and `nop` must score 0.0 with `prediction_missing = 1`. Treat any
other outcome as a defect in the task, not in the agent.

### Running a real agent

The agent phase is allowlisted, so the model endpoint must be reachable by name.
`DEFAULT_ALLOWED_HOSTS` covers the providers Harbor's own agents dial; a self-hosted or
proxied endpoint needs its host added **at generation time**, since the allowlist is
baked into `task.toml`:

```bash
uv run faultloc-adapter --task-ids 42470093 --configs main --overwrite \
  --allowed-hosts poc my-endpoint.example.com
```

Auth differs per agent, and only one of them reads a file:

| agent | credential | how Harbor picks it up |
| --- | --- | --- |
| `codex` | API key | `OPENAI_API_KEY` (+ `OPENAI_BASE_URL` to retarget) |
| `codex` | ChatGPT subscription | `CODEX_AUTH_JSON_PATH=~/.codex/auth.json`, uploaded into the container |
| `claude-code` | API key | `ANTHROPIC_API_KEY` |
| `claude-code` | subscription | `CLAUDE_FORCE_OAUTH=1` + `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` |

`claude-code` has **no** file path: Harbor points `CLAUDE_CONFIG_DIR` at a fresh empty
directory in the container and never copies `~/.claude/.credentials.json`.

**`OPENAI_BASE_URL` overrides subscription auth.** It is read from the ambient
environment independently of `CODEX_AUTH_JSON_PATH`, so a stray `export` — or a sourced
`.env` — silently sends a ChatGPT-authenticated run to the wrong backend. `unset` it
before any subscription run.

Model names are not interchangeable across auth modes: `gpt-5.3-codex` is rejected for
ChatGPT accounts (`400: not supported when using Codex with a ChatGPT account`). Query
what an account may use with `~/.codex/models_cache.json`.

## Gotchas

Learned the hard way; each cost a failed run.

- **`up --wait` returns when the container is *running*, not when its command
  finishes.** Staging copies the source tree, so without a readiness gate the agent can
  start against a half-populated `/workspace/src` — silently, scoring like a bad agent.
  Staging therefore writes `/workspace/.staged` last, and `main` declares a healthcheck on
  it. Small projects hide this; it surfaced only on the multi-project sweep.
- **`[environment.env]` is silently ignored for compose environments**
  (`harbor/environments/base.py`). Environment variables, `PATH` included, must be
  declared in `environment/docker-compose.yaml`.
- **Harbor's default teardown deletes every image in a compose stack.** With
  `delete: true` (the default) it runs `docker compose down --rmi local --volumes`,
  which removed both `faultloc-agent:v1` and the digest-pinned `n132/arvo@sha256:...`
  base in a measured test — a ~3 GB re-pull per trial. Only compose tasks reach that
  branch, so single-container tasks never show the problem. Use `delete: false`
  (`run_faultloc-adapter.yaml`) or `uv run harbor run --no-delete`: that falls through
  to a plain `docker compose down`, which still removes containers and networks and
  simply keeps images. A registry does not fix this; it only converts deletion into a
  re-pull. Note the corollary: plain `down` also *keeps named volumes*, which is why
  the shared source volume is tmpfs-backed (see below).
- **Plain `down` keeps named volumes, so the shared source volume is tmpfs-backed.**
  A disk-backed volume would leak a full source tree per trial (~120 MB each,
  unbounded — measured 2.3 GB across 21 test trials). Declaring it
  `driver_opts: {type: tmpfs, device: tmpfs, o: size=4g}` keeps it shareable between
  `poc` and `main` while the content lives in RAM; teardown unmounts it and
  the memory is released. Verified: leftover volume content 0 bytes, host volume disk
  unchanged across a run. `size` is a ceiling, not an allocation.
- **Harbor's egress control collapses compose services into one network namespace.**
  Any service that declares neither `networks` nor `network_mode` is moved into the
  egress sidecar's netns and loses its compose DNS name, so `main` could not resolve
  `poc` (curl exit 6). The generated `poc` therefore declares `networks: [default]`;
  only `main` is egress-policed. Relatedly, `[environment]`
  must stay `public` — staging fetches the PoC from the sidecar before any agent
  exists, and a `no-network` baseline blocks that (curl exit 52). The policy that
  matters is `[agent]`, which applies during `agent.run()`.
- **Agent choice is constrained to what the agent image bakes in** (`claude_code`,
  `codex`). `[environment] network_mode = "no-network"` applies during `agent.setup()`,
  so Harbor cannot install an agent at run time; those two skip installation when a
  satisfying binary is already present.
- **`docker compose up` only builds when the image is missing.** Editing a task's
  embedded sidecar and re-running silently reuses the old container image unless
  `--build` is passed. The inline-server design avoids this entirely — there is no
  sidecar image to cache — but it applies to any image a task builds.

Generated datasets, Harbor jobs, and trial artifacts are excluded from version control.
