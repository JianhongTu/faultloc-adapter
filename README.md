# faultloc-adapter

`faultloc-adapter` evaluates whether a root-cause report helps a separate agent repair a
vulnerability. The benchmark uses a locked, category-balanced set of 500 validated FLBench
instances. Diagnosis models first produce locations and a causal explanation under the main
condition and three ablations; a fixed implementation agent (`qwen-coder` with
`qwen3-small`, served as `Qwen/Qwen3.6-27B`) then attempts each repair from either a
diagnosis model's report or a developer-patch-anchored gold report. Gold is the baseline and
empirical ceiling—not self-diagnosis—and the headline repair result requires both a
successful repair and a patch attributable to the supplied report.

## Quick start

### 1. Install and unpack the locked data

Requirements: Linux, Docker, and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
docker build -t faultloc-agent:v1 agent-image/

sha256sum -c data/eval500.tar.gz.sha256
rm -rf manifests reports/gold
tar -xzf data/eval500.tar.gz

export FAULTLOC_ROOT="$PWD"          # see "Egress timeout override" below
uv run python scripts/check_egress_override.py
```

`manifests/` and `reports/gold/` are extraction targets, not sources: the archive is the
artifact, and those directories are rebuilt from it. Remove them first so what is on disk
is exactly what the archive holds.

`FAULTLOC_ROOT` is not optional and every `harbor run` must be invoked from the repo
root; both are checked by `check_egress_override.py`. Run it once per shell before any
batch — the failure it prevents costs a whole run, not a trial.

The archive contains exactly 500 manifests and their 500 gold reports — the complete
frozen input. The manifests pin the archival `n132/arvo:<id>-vul` image tags, so nothing
here needs a FLBench checkout, `arvo.db`, or network access to build a dataset. The
instance set is locked; the tool that originally froze it was removed with it, and lives
in git history if the set ever has to change.

### 2. Generate the diagnosis datasets

```bash
uv run faultloc-adapter
```

This creates one 500-task Harbor dataset per condition:

| Dataset | Information available to the diagnosis model |
| --- | --- |
| `datasets/flbench-diagnosis-eval500-main` | Source, PoC file, sanitizer report, and PoC execution |
| `datasets/flbench-diagnosis-eval500-ablation-static-only` | Source and PoC file only |
| `datasets/flbench-diagnosis-eval500-ablation-no-poc-file` | Source, sanitizer report, and PoC execution |
| `datasets/flbench-diagnosis-eval500-sanity-no-source` | Everything except source |

### 3. Run each diagnosis model

Set the Harbor agent, model, and a filesystem-safe identifier for the actual served model.
Do not use a gateway alias as `DIAGNOSIS_ID`.

```bash
export DIAGNOSIS_AGENT=codex
export DIAGNOSIS_MODEL=provider/model
export DIAGNOSIS_ID=provider-model-version

for DATASET in \
  flbench-diagnosis-eval500-main \
  flbench-diagnosis-eval500-ablation-static-only \
  flbench-diagnosis-eval500-ablation-no-poc-file \
  flbench-diagnosis-eval500-sanity-no-source
do
  uv run harbor run \
    -p "datasets/$DATASET" \
    -a "$DIAGNOSIS_AGENT" \
    -m "$DIAGNOSIS_MODEL" \
    --no-delete \
    --job-name "$DATASET-$DIAGNOSIS_ID"
done
```

`--no-delete` is not optional. Harbor's default teardown runs `docker compose down
--rmi local --volumes`, which removes the archival ARVO base image, and the next
trial re-pulls roughly 3 GB. Over 4 datasets x 500 instances that dominates the
runtime. The repair commands below get the same setting from
`configs/main-eval.yaml` (`environment.delete: false`).

Each completed trial writes:

```text
artifacts/logs/artifacts/prediction.json
artifacts/logs/artifacts/summary.txt
```

For the repair experiment, freeze the main-condition output as one report per instance
under `reports/diagnosis-$DIAGNOSIS_ID/`. Each report has this schema:

```json
{
  "report_id": "provider-model-version-42470093",
  "instance_id": 42470093,
  "locations": [
    {"file": "src/example.c", "line_start": 10, "line_end": 12}
  ],
  "cause": "One sentence explaining the root cause and where it occurs."
}
```

`line_end` is exclusive. Preserve an empty `locations` list when the diagnosis model names
no location; the repair generator records that instance as not run and scores it as zero.

### 4. Generate and validate repair datasets

Generate the gold ceiling once:

```bash
uv run faultloc-repair --source gold --reports reports/gold

uv run python scripts/repair_boundaries.py \
  --tasks datasets/flbench-repair-eval500-gold
uv run python scripts/check_anchoring.py \
  --tasks datasets/flbench-repair-eval500-gold
```

Then generate one repair dataset for each fixed diagnosis report set:

```bash
SOURCE="diagnosis-$DIAGNOSIS_ID"

uv run faultloc-repair \
  --source "$SOURCE" \
  --reports "reports/$SOURCE"

uv run python scripts/repair_boundaries.py \
  --tasks "datasets/flbench-repair-eval500-$SOURCE"
```

The boundary check must pass before running the implementation agent. It verifies that a
diagnosis dataset differs from gold only in the supplied report.

### 5. Run the fixed implementation agent

`configs/main-eval.yaml` pins the implementation agent, `qwen3-small`, Qwen Code version,
timeouts, retries, concurrency, and three independent attempts per instance. Configure its
OpenAI-compatible endpoint in `.env`:

```bash
OPENAI_API_KEY=...
OPENAI_BASE_URL=...
```

Load the environment, inspect the resolved configuration, and run gold:

```bash
set -a
. .env
set +a

uv run harbor run \
  -c configs/main-eval.yaml \
  -p datasets/flbench-repair-eval500-gold \
  --print-config

uv run harbor run \
  -c configs/main-eval.yaml \
  -p datasets/flbench-repair-eval500-gold \
  --job-name repair-gold
```

Run the same fixed configuration for every diagnosis report set:

```bash
uv run harbor run \
  -c configs/main-eval.yaml \
  -p "datasets/flbench-repair-eval500-$SOURCE" \
  --job-name "repair-$SOURCE"
```

Harbor writes results and the resolved task/configuration lock under `jobs/<job-name>/`.
Generated datasets and jobs are disposable execution artifacts; the locked manifests,
gold reports, and each frozen diagnosis report set are the experiment inputs.

The repair verifier reports:

- `repair_ok`: the patch exists, compiles, and suppresses the PoC.
- `at_location`: at least one repair-patch hunk overlaps a reported location.
- `verified` and `reward`: `repair_ok AND at_location`.

Compare each diagnosis report set with the gold ceiling using `verified`; retain
`repair_ok` to distinguish unattributed repairs from unsuccessful repairs.

## Egress timeout override

Harbor enforces `network_mode = "allowlist"` by injecting a sidecar that captures the
agent's network namespace and relays every TCP connection through a `gost` transparent
proxy. `gost` closes a relayed connection after roughly 15 seconds of no data, and neither
Harbor nor `gost` documents this: `NetworkPolicy` exposes only `network_mode` and
`allowed_hosts`, with no timeout setting.

That is fatal here. The repair verifier POSTs `/compile`, which runs `arvo compile` for
minutes and returns nothing until it finishes, so the proxy hangs up first. The first real
agent run failed 5/5 with `InfrastructureError: POST /compile: Remote end closed connection
without response` — the agents had produced valid patches, and nothing scored. `POST /poc`
is exposed the same way for any reproducer slower than 15s, including localization's
`run_poc.sh`, where the failure would read as "no crash" instead of erroring.

Measured through the sidecar, a response after 10s arrives and 20s/30s/60s are all killed
at exactly 15s; the same requests bypassing the sidecar succeed at 60s.

`harbor-overrides/gost.yaml` is Harbor's config with the timeouts raised, mounted over the
sidecar's copy through `environment.extra_docker_compose` — a supported surface, so this is
configuration rather than a patched install. Verified after the override: 10s, 20s, 60s and
120s all return 200, while a non-allowlisted host is still blocked.

Two requirements, both enforced by `scripts/check_egress_override.py`:

- `FAULTLOC_ROOT` must be exported. Compose expands it into the bind source; unset, Docker
  creates a directory at the mount point and the sidecar never becomes healthy.
- `harbor run` must be invoked from the repo root. Harbor does not expand `${VAR}` in a
  config file, so the path to the override is relative to the working directory.

The override **replaces** a security-critical file: the bypass wiring, the allowlist path
and the redirect port in it are what actually enforce `allowed_hosts`. It is therefore
byte-identical to upstream apart from the timeout lines, and the gate pins the upstream
SHA-256 it was vendored from. If Harbor changes its copy the gate fails; re-vendor from the
new file and re-apply only the timeouts rather than leaving a stale duplicate in force.

This is a workaround, not the end state. Making `/compile` and `/poc` asynchronous — return
a job id, poll for completion — would keep every request far inside any proxy timeout and
remove the dependency on an undocumented default entirely.
