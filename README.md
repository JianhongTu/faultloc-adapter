# faultloc-adapter

`faultloc-adapter` evaluates whether a root-cause report helps a separate agent repair a
vulnerability. The benchmark uses a locked set of 900 validated FLBench
instances — FLBench's own eval list. Diagnosis models first produce locations and a causal explanation under the main
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

sha256sum -c data/eval900.tar.gz.sha256
rm -rf manifests
tar -xzf data/eval900.tar.gz

export FAULTLOC_ROOT="$PWD"          # see "Egress timeout override" below
uv run python scripts/check_egress_override.py
```

`manifests/` is an extraction target, not a source: the archive is the artifact and the
directory is rebuilt from it. Remove it first so what is on disk is exactly what the
archive holds. The superseded `data/eval500.tar.gz` is kept because it is the only copy
of the 500 gold reports that fed the retired report-driven stage; nothing reads them, and
they were never regenerable — their `cause` text was model-written, not derived.

`FAULTLOC_ROOT` is not optional and every `harbor run` must be invoked from the repo
root; both are checked by `check_egress_override.py`. Run it once per shell before any
batch — the failure it prevents costs a whole run, not a trial.

The archive contains exactly 900 manifests — the complete frozen input. They pin the
archival `n132/arvo:<id>-vul` image tags, so nothing here needs a FLBench checkout,
`arvo.db`, or network access to build a dataset.

The set is **FLBench's own 900-instance eval list, verbatim**, so a result here is
directly comparable with a published FLBench row. It replaced a balanced 500-instance
subset; `scripts/select_eval500.py` still documents how that was chosen and why FLBench's
distribution is skewed (heap-buffer-overflow alone is 28%), but being flatter than FLBench
is what made those numbers incomparable, so the skew is now inherited on purpose.

Changing the set again is `python -m faultloc_adapter.freeze --flbench <checkout>
--instance-list <ids.json>`, then re-archiving. Freezing is pure metadata — crash fields
from `arvo.db`, gold patch from `eval-patches/` — so it needs a FLBench checkout but no
network and no containers.

### 2. Generate the diagnosis datasets

```bash
uv run faultloc-adapter
```

This creates one 900-task Harbor dataset per condition:

| Dataset | Information available to the diagnosis model |
| --- | --- |
| `datasets/flbench-diagnosis-eval900-main` | Source, PoC file, sanitizer report, and PoC execution |
| `datasets/flbench-diagnosis-eval900-ablation-static-only` | Source and PoC file only |
| `datasets/flbench-diagnosis-eval900-ablation-no-poc-file` | Source, sanitizer report, and PoC execution |
| `datasets/flbench-diagnosis-eval900-sanity-no-source` | Everything except source |

### 3. Run each diagnosis model

Set the Harbor agent, model, and a filesystem-safe identifier for the actual served model.
Do not use a gateway alias as `DIAGNOSIS_ID`.

`scripts/diagnosis_config.py` pins the agent into the job config **together with the
kwargs that switch off its hosted web tools**, which are disabled by default and are not
something the operator has to remember. They have to travel with the agent because
hosted web search is not a shell tool: the model asks the vendor's backend to search and
the results arrive inside the ordinary API response, over the same host the allowlist
must permit for the model to run at all. No network policy can see it, and
`STRIP_ARCHIVAL_GIT` does not apply. Left on, a run searches for the crash symbols and
reads the developer patch it is about to be scored against — observed doing exactly that.

Run the generated config **without `-a`**: that flag discards the config's `agents:`
block wholesale, taking the denial with it. Passing the kwargs as `--ak` instead is worse
— Harbor keeps only the kwargs an agent declares and silently drops the rest, so the
codex flag handed to `claude-code` leaves web search on with nothing in the log to say so.

Concurrency is per-endpoint, not per-box: `configs/diagnosis-eval.yaml` pins 4 for
`claude-opus-5` and `gpt-5.6-sol`. `glm-4.7` on Zhipu needs `-n 2` on the command line.

```bash
export DIAGNOSIS_AGENT=codex
export DIAGNOSIS_MODEL=provider/model
export DIAGNOSIS_ID=provider-model-version

CONFIG=$(mktemp --suffix=.yaml)
uv run python scripts/diagnosis_config.py "$DIAGNOSIS_AGENT" "$DIAGNOSIS_MODEL" > "$CONFIG"

for DATASET in \
  flbench-diagnosis-eval900-main \
  flbench-diagnosis-eval900-ablation-static-only \
  flbench-diagnosis-eval900-ablation-no-poc-file \
  flbench-diagnosis-eval900-sanity-no-source
do
  uv run harbor run \
    -c "$CONFIG" \
    -p "datasets/$DATASET" \
    --job-name "$DATASET-$DIAGNOSIS_ID"
done
```

The generated config is `configs/diagnosis-eval.yaml` plus one `agents:` block, so
everything below still applies and there is no second copy to keep in step.

`configs/diagnosis-eval.yaml` is not optional. It carries the egress timeout
override, without which any reproducer slower than gost's cut-off is hung up on
mid-run: `run_poc.sh` catches only `HTTPError`, so the resulting
`RemoteDisconnected` escapes as a traceback with exit 1 — which its own contract
reads as *still crashes*. PoC execution is then unavailable for those instances,
on that condition. See "Egress timeout override"
below. It also pins one attempt,
concurrency, the retry allowlist and `environment.delete: false`; that last one
keeps Harbor's teardown from removing the archival ARVO base and re-pulling
roughly 3 GB per trial, which over 4 datasets x 900 instances would dominate the
runtime. Everything that varies between diagnosis models stays on the command
line.

Each completed trial writes:

```text
artifacts/logs/artifacts/prediction.json
artifacts/logs/artifacts/summary.txt
```

`line_end` is exclusive in `prediction.json`, matching the vendored scorer.

These are stage 1's result. Nothing downstream reads them: the family that consumed
reports is retired (step 4), and the tools that turned raw output into `reports/<source>/`
went with it.

### 4. Run the repair stage

The report-driven repair family — one dataset per report source, scored on whether the
patch landed at the reported location — is **retired**. It is in git history; the CLI no
longer offers `--source` or `--reports`, and the config that pinned its implementation
agent has been removed.

Its successor is the study below, which asks a different question of the same instances:
not whether a report helps an agent fix the bug, but whether the developer's location is
the only place the bug can be fixed. See **Alternative-patch study**.

## Alternative-patch study

A bounded sensitivity analysis, separate from the two-stage experiment above. The repair
and localization results both treat the accepted developer patch as the only correct
location; this asks whether that assumption changes the ranking. Two strong agents are
asked to repair the same defect *outside* the developer-patch spans, and a candidate is
accepted only if it is mechanically executable and lands somewhere else.

The sample is 50 of the locked 500 — 10%, proportionally stratified by sanitizer and by
developer-patch topology (single- versus multi-hunk), ordered within a stratum by
`sha256("20260731:<local_id>")`. It is frozen in `data/alternative-patch-eval50.json` and
was selected before any patch was generated:

```bash
uv run python scripts/select_repair_eval50.py --check
```

The ordering nests, so raising the target keeps the instances already run.

```bash
uv run faultloc-repair
```

One dataset, `datasets/flbench-repair-eval50`, run once per generating agent:

```bash
uv run harbor run -c configs/repair-codex.yaml \
  -p datasets/flbench-repair-eval50 --job-name repair-codex

uv run harbor run -c configs/repair-claude.yaml \
  -p datasets/flbench-repair-eval50 --job-name repair-claude
```

**Do not `. .env` before these.** That file carries `OPENAI_BASE_URL` for the repair
stage's self-hosted gateway, and Harbor's Codex adapter forwards `OPENAI_BASE_URL`
whenever it is set — including under `auth.json` authentication — so a sourced `.env`
silently aims Codex at an endpoint that does not serve its model. Codex defaults to
`OPENAI_API_KEY`; to use a ChatGPT subscription instead, set `CODEX_FORCE_AUTH_JSON=1`
with `~/.codex/auth.json` in place, and leave `OPENAI_BASE_URL` unset either way. Claude
Code needs `ANTHROPIC_API_KEY`, or `CLAUDE_CODE_OAUTH_TOKEN` with `CLAUDE_FORCE_OAUTH=1`.

Before either arm, run the preconditions: the oracle applies the developer patch on the
same path a candidate takes, so it proves the instance builds and the PoC is suppressible
without spending an API call.

```bash
export FAULTLOC_ROOT="$PWD"
uv run harbor run -c configs/repair-codex.yaml \
  -p datasets/flbench-repair-eval50 -a oracle \
  -i 'repair__<id>' --job-name repair-precondition
```

Both agents get byte-identical tasks by construction — the generator identity lives in the
job name, not in the dataset, because the later audit reads the candidates anonymously.
Neither config pins a CLI version: both agents are baked into `agent-image/` at exact
versions and Harbor skips installation when a satisfying binary is present, so the image is
the pin and a second one here could only disagree with it.

The environment is the localization stage's, with the agent half replaced: the reproducer
is withheld as a *file* while remaining runnable, and the agent gets `build.sh` and
`run_tests.sh`. `adapter.compose()` builds both, so the sidecar half cannot drift between
them, and `scripts/config_boundaries.py` carries a `repair` entry asserting no
localization config has either tool. The developer patch reaches the agent through the
prompt rather than through the container.

### Regression suites

Building and suppressing the reproducer is not evidence of a repair. The 3-instance smoke
produced three candidates that passed every mechanical gate, and one of them broke
libxml2's own suite — 3158 checks clean before the patch, 2 errors after. So each instance
runs the project's own tests.

**Each suite is written by hand, and there is no way around that.** ARVO images carry the
source and a fuzzer build, never a configured test build: the tests are present but
nothing can run them until someone writes the configure-and-build recipe for that project.
Detection cannot substitute — a probe across all 50 trees finds a test declaration in 25 of
them, and aom is in the other 25 despite running 4081 tests. OSS-Fuzz (Chronos
`run_tests.sh`) and e2e-cyber-bench (`test.sh`) both hand-write it per project, and neither
attempts detection.

Generation therefore writes a **placeholder** to `data/testset/<id>/test.sh` for any
instance without a frozen suite, and says so:

```
regression suites: 0 authored and frozen, 50 PLACEHOLDER, of 50 generated task(s)

  DO NOT LAUNCH. 50 instance(s) have no test suite.
```

The placeholder exits non-zero, always. Exiting 0 would make an unauthored instance look
like a project whose tests all pass, which is the one reading that must never happen — the
verifier reads this exit code, so an unauthored dataset rejects every candidate rather than
quietly accepting them ungated.

**The contract is "exit 0 when the result matches the unpatched tree", not "the suite is
green."** Two of the three instances measured by hand are red before anything is patched —
open62541 fails 2 of 31, aom 2 of 4081 — so a green-suite rule would reject the developer's
own patch. The author encodes the expected result in the script; the verifier reads one
number, and `tests.log` keeps the suite's full output for whoever reads a rejection.

The script is self-contained: nothing outside it records which tests exist or how they
behaved, because a second copy of that knowledge is a second thing to keep in step. A
script counts as authored when it is not byte-identical to the placeholder — there is no
status to set and no record to keep in sync. `faultloc-repair` prints
`test suites: N authored, M PLACEHOLDER`, and `task.toml` records `regression_tested` per
instance so the two halves are never pooled.

### Outcomes

Each trial is labelled with one mechanical outcome, emitted one-hot into `reward.json` so a
job's mean is the funnel directly:

| Outcome | Meaning |
| --- | --- |
| `no_patch` | the agent reported no alternative exists, or left nothing in the tree |
| `build_failed` | changed the source, but it does not compile |
| `poc_failed` | compiles, reproducer still crashes |
| `regression_detected` | the test script exited non-zero — see below |
| `executable_candidate` | compiles, suppresses the PoC, test script exits 0 |

`reward` is 1 only for `executable_candidate`. Structured evidence — every changed file,
which lines collided, the suite's exit code — goes to
`artifacts/logs/artifacts/mechanical.json` alongside `patch.diff`, `changed_spans.json`,
`compile.log`, `poc.log` and `tests.log`.

**`regression_detected` has two causes and they are not the same result.** The candidate
broke something, OR no test script has been written for that instance yet and the
placeholder failed as designed. Check `regression_tested` in `task.toml` before reading
this row: `false` means the instance was never authored and every candidate on it lands
here regardless of what it did. `tests.log` shows which, and an unauthored instance says
so in as many words.

Otherwise `regression_detected` is reported separately and never folded into a generic
rejection. It is a distinct way to reach "no accepted reference" — alongside no candidate
found, a candidate rejected on substance, and a candidate rejected for want of evidence —
and averaging them together would report a search failure that did not happen.

`executable_candidate` is **not** the claim that an alternative repair exists. It
establishes only that the patch is executable and elsewhere; whether it repairs the root
cause rather than suppressing the symptom is a separate audit that reads these artifacts.

Each task's `solution/solve.sh` applies the developer patch. It is the per-instance
precondition check — read `compiled` and `poc_suppressed` from it, not `reward`. Its
`gold_overlap` is 1 by construction, since the patch sits on its own spans.

**Overlap with the developer's patch is measured, not gated.** `gold_overlap` and the
`gold_*` metrics record how far a candidate sits from the developer's lines, and the audit
decides whether that distance makes it a different repair. An exact line intersection
answers a narrower question: anchoring puts an insertion on two candidate lines, so a fix
one line off the developer's edit collides while a fix ten lines away that reimplements the
same idea does not.

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
`run_poc.sh`. There the same `RemoteDisconnected` escapes uncaught — `run_poc.sh` handles
only `HTTPError` — and exits 1, which its own contract reads as "still crashes".
`POST /test` is the longest of the three and depends on the same override: a suite is
bounded at 1800s in the sidecar, well inside the raised 3600s but two orders of magnitude
outside the default.

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
