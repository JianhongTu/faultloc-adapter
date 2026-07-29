# faultloc-adapter

Converts [FLBench](https://github.com/JianhongTu/FLBench) fault-localization instances
into reproducible [Harbor](https://github.com/harbor-framework/harbor) tasks. Standalone,
so FLBench never has to fork or modify Harbor.

Two task families are generated from the same frozen instances:

- **localization** (`faultloc__<id>-<config>`) — the agent inspects a vulnerable C/C++
  source tree plus crash evidence and writes `prediction.json`, a list of suspicious
  spans, scored against ground truth from the cleaned developer patch. No build.
- **repair** (`repair__<id>-<condition>`) — the agent diagnoses *and fixes* the
  vulnerability in **one attempt with no build tool**, then the verifier rebuilds from
  the agent's own tree and re-runs the reproducer. Measures whether an external
  root-cause report beats the same agent's self-diagnosis.

**Status:** all gates passing. Localization — scorer parity (10,508/10,508 archived
predictions re-score identically on all six metrics), config boundaries (4/4), controls
(`oracle` 1.0 / `nop` 0.0), environment sweep (17/17 instances), live agents under Harbor
producing valid predictions. Repair — controls on every instance (17/17 repair-eligible),
condition boundaries (17/17), compile profile (17/17 build clean, 3–161 s). Each is
reproducible via the `scripts/` entry named below. Remaining: implementation-agent
calibration.

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

# 2. Unpack the frozen instance data (500 manifests; no DB or FLBench needed after this)
tar -xzf data/manifests.tar.gz

# 2b. Or re-freeze from a FLBench checkout, then repack (manifests/ itself is gitignored)
uv run python -m faultloc_adapter.freeze --flbench ~/codes/FLBench --task-ids 42470093
tar -czf data/manifests.tar.gz --sort=name --mtime='UTC 2020-01-01' \
    --owner=0 --group=0 --numeric-owner manifests/

# 3. Generate tasks (omit --configs for all four)
uv run faultloc-adapter --task-ids 42470093 --configs main

# 4. Verify the controls
uv run harbor run -p datasets/faultloc-adapter/faultloc__42470093-main -a oracle
uv run harbor run -p datasets/faultloc-adapter/faultloc__42470093-main -a nop
```

`oracle` must score 1.0 and `nop` 0.0 with `prediction_missing = 1`. Anything else is a
defect in the task, not the agent. Generation is deterministic and self-contained — same
manifest, byte-identical task directory, no host paths.

Repair tasks come from a second generator and have their own controls:

```bash
uv run faultloc-repair --task-ids 42508282            # -> repair__42508282-{self,gold}
uv run harbor run -p datasets/faultloc-adapter/repair__42508282-gold -a oracle
uv run python scripts/repair_controls.py --tasks datasets/faultloc-adapter   # every instance
uv run python scripts/repair_boundaries.py --tasks datasets/faultloc-adapter
```

`scripts/repair_controls.py` is instance **selection**, not just a gate: an instance whose
`oracle` does not build and suppress the PoC through the staged path cannot be scored and
belongs out of the subset. Run it before spending anything on agents.
`scripts/repair_profile.py` measures build wall-clock, which is what bounds a repair trial.

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

One localization task per `(instance, config)` for the four FLBench configs `main`,
`ablation1`, `ablation2`, `sanity`, and one repair task per `(instance, condition)` for
`self` and `gold`. Both families use the same two compose services:

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

Repair adds a `/compile` endpoint (`sidecar/repair_server.py`) that only the **verifier**
calls. The agent has `run_poc.sh` for evidence and no build tool at all: it reads,
diagnoses, edits, and exits, and the build happens afterwards. Fix probability comes from
repeated rollouts (`n_attempts`), which keeps the measurement on whether the agent
understood the defect rather than on how long it iterated against a pass/fail oracle.
`run_poc.sh` runs the pre-existing binary and never reflects the agent's edits — the prompt
says so explicitly, since silently letting an agent believe otherwise would be misleading.

Before building, the sidecar replaces `/src/<project>` with the agent's tree, so what
compiles is exactly what the agent wrote and build output never touches the tmpfs volume.
The patch is captured as `git diff` against a `harbor-baseline` tag written over the staged
tree before the agent starts, which catches committed, staged, unstaged and newly created
files in one diff.

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

Repair tasks emit their own contract, decomposed the same way:

```json
{"reward": 1.0, "verified": 1, "repair_ok": 1, "patch_present": 1, "compiled": 1,
 "poc_suppressed": 1, "at_location": 1, "report_iou": 1.0, "report_hunk_recall": 1.0}
```

`repair_ok` is the plain build-and-suppress result and is emitted for **both** conditions.
`verified` additionally requires attribution under an assisted condition, so the two arms
score under different predicates — **compute raw repair uplift from `repair_ok`, not from
`reward`**, or the assisted arm is penalised by a criterion the baseline never faced.
Attribution is `report_hunk_recall > 0`: at least one hunk of the agent's patch sits at a
reported location. It is threshold-free on purpose, and every continuous metric is stored,
so a calibrated cutoff can replace it later without re-running anything. Under `self` there
is no report, so `at_location` and every `report_*` metric are **-1**, meaning not
applicable — filter on that rather than reading them as zeros.

`gold` is a ceiling, not a forecast: its locations are derived from the developer patch, so
an agent that fixes where the developer fixed is attributable almost by construction.

With one attempt and no build tool, a patch that is right in substance but does not compile
scores zero. That is intended — but **report fix rate conditional on `compiled` alongside
the unconditional rate**, or build noise is silently folded into the diagnosis result.

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
- **The wipe in `sidecar/repair_server.py:_sync_tree`.** It is what makes the built tree
  byte-for-byte the agent's tree, and the only thing that makes a *second* compile possible:
  OSS-Fuzz build scripts run once in a fresh image and are not all idempotent — miniz's does
  a bare `mkdir build` and fails with "File exists" on the second call. One compile per
  trial means nothing depends on that today, but any retry, or any return to an iterating
  agent, hits it immediately.

**Never set `vm.mmap_rnd_bits` host-wide.** It is not namespaced, so it changes every other
workload on the machine, and at 28 the msan failure cannot occur — any test of the two
mitigations above would pass for the wrong reason. Verify changes to them with the host at
its default of 32.

Generated datasets, Harbor jobs, and trial artifacts are excluded from version control.
