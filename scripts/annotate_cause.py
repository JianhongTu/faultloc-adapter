#!/usr/bin/env python3
"""Generate a root-cause annotation for an instance from its developer patch.

One shot, no tools, no agent loop. Everything the model could need is placed in a
single prompt -- the developer patch, the sanitizer report, and the full text of
every source file the patch touches -- and exactly one completion is requested.

Pre-fix source comes from the pinned ARVO image, which is the definitive tree for
this pipeline: `gt_diff` line numbers are expressed in image coordinates, and the
image is what the repair agent edits. Upstream at `fix_commit^` is NOT equivalent
-- ARVO `-vul` images sit at the crash revision, which can predate `fix_commit^`,
so upstream blobs disagree with the image on a substantial minority of files and
the disagreement is silent: the model reads code at the hunk's line numbers that
is not the code at those line numbers in the agent's tree.
One shot also keeps the annotation reproducible, and keeps it from turning into a
second agent whose search behaviour would confound the arm it is meant to define.

Locations come from the developer patch (`gold_report`), unchanged. Only `cause`
is generated, so an arm built from this differs from `gold` in authorship alone.

WHY THIS EXISTS. `gold_report()` leaves `cause` empty on purpose: text summarising
a patch leaks the fix. Hand-writing it instead makes the arm unreproducible and
puts the leak boundary in one author's taste. This script makes the boundary an
explicit, checkable constraint in a prompt, and the arm derivable from the frozen
manifest.

WHAT IT GUARDS AGAINST. A model asked to summarise a diff drifts into describing
the *edit* ("clear the whole tree", "validate against the import count") rather
than the defect. `_review` flags that, along with annotations too long to be
comparable with the ~200-320 character reports the other arms carry.

    # every instance in the locked 500-instance evaluation set
    uv run python scripts/annotate_cause.py --out reports/gold_auto
    # or a subset of it
    uv run python scripts/annotate_cause.py --task-ids 42495936 --out reports/gold_auto

Reads OPENAI_API_KEY / OPENAI_BASE_URL from the environment (source .env first).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faultloc_adapter.manifest import pinned_image  # noqa: E402
from faultloc_adapter.repair import gold_report  # noqa: E402

DEFAULT_MODEL = "kimi"
# The locked evaluation set. Annotations are the `gold_auto` arm's cause, so the
# instance set they are generated for has to be the one being evaluated --
# see scripts/select_eval500.py for how it was chosen.
DEFAULT_INSTANCE_LIST = (
    Path(__file__).resolve().parents[1] / "data" / "eval500_instance_list.json"
)
# Bracketing the observed spread of real localization-agent summaries: n=84 across
# 5 runs x 17 instances, median 297 chars, p75 376, p90 476. Outside this band the
# annotation is not comparable with the reports the other arms carry.
TARGET_CHARS = (150, 480)
# One file's worth of context is cheap; a whole project is not. Files are sent
# whole up to this budget, then dropped with a note rather than silently cut.
FILE_BUDGET_CHARS = 400_000
CRASH_OUTPUT_LINES = 120

SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {
            "type": "string",
            "description": (
                "ONE sentence naming the faulty logic and why it is wrong, around "
                "300 characters. Describe the defect, not the repair."
            ),
        }
    },
    "required": ["root_cause"],
    "additionalProperties": False,
}

INSTRUCTIONS = """\
You are annotating a known security defect for a benchmark. You are given the \
developer's fix, the sanitizer report, and the full source of every file the fix \
touches.

Your output must satisfy the same requirement the benchmark places on a fault-\
localization agent, which is:

    "Write a one-sentence summary describing what root cause you identified and \
    where."

So: ONE sentence, plain prose, naming the faulty logic and where it lives (file, \
function, and line numbers if useful). Aim for about 300 characters.

One additional constraint applies to you and not to those agents, because unlike \
them you can see the developer's fix: describe the DEFECT, never the repair. Do not \
say what to add, remove, change, rename or reorder, and do not restate the edit. \
Someone who has not seen the fix should be able to find the defect from your \
sentence but still have to decide for themselves how to repair it.
"""


# Phrasing that describes an edit rather than a defect.
_REMEDY = re.compile(
    r"\b(the fix|fixed by|is fixed|patch(?:ed)? (?:by|adds)|should (?:be )?(?:add|remove|call|use|check)"
    r"|add(?:s|ing)? a |remov(?:e|es|ing) the |replac(?:e|es|ing) |chang(?:e|es|ing) .{0,20}\bto\b"
    r"|instead of |must be (?:add|chang|replac))",
    re.I,
)


def _touched_files(gt_diff: str) -> list[str]:
    return sorted({m.group(1) for m in re.finditer(r"^\+\+\+ b/(.+)$", gt_diff, re.M)})


def _read_from_image(manifest: dict, rel_paths: list[str]) -> dict[str, str]:
    """Pre-fix text of the touched files, read out of the pinned ARVO image.

    The image is the tree `gt_diff` is expressed against and the tree the repair
    agent edits, so this is the only source whose line numbers agree with the
    report the annotation ships with. A file that is not where we expect is
    skipped rather than guessed at, and the caller notes the gap in the prompt.

    Reading NOTHING is a different matter and raises. Source-grounded annotations
    measured 5/5 correct against ground truth where diff-only measured 4/5, so
    silently falling back to diff-only is a downgrade to the worse mode -- and it
    is invisible in the output, which still prints OK. This is not hypothetical:
    an unauthenticated Docker Hub pull limit produced exactly that on a whole
    batch. Failing here makes the instance retryable under --resume instead.
    """
    image = pinned_image(manifest)
    project = manifest["project"]
    out: dict[str, str] = {}
    why: list[str] = []
    for rel in rel_paths:
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "sh", image,
             "-c", f"cat /src/{project}/{rel}"],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode == 0 and r.stdout.strip():
            out[rel] = r.stdout
        else:
            err = (r.stderr or "").strip().splitlines()
            why.append(err[-1] if err else f"exit {r.returncode}, no output")
    if rel_paths and not out:
        raise RuntimeError(
            f"read 0 of {len(rel_paths)} source files from {image}: "
            f"{why[0] if why else 'unknown'}"
        )
    return out


def _build_prompt(manifest: dict, files: dict[str, str]) -> str:
    crash = "\n".join(manifest["crash_output"].splitlines()[:CRASH_OUTPUT_LINES])
    parts = [
        f"Project: {manifest['project']}   Crash: {manifest['crash_type']}   "
        f"Sanitizer: {manifest['sanitizer']}",
        "",
        "## Sanitizer report", "```", crash, "```", "",
        "## Developer fix", "```diff", manifest["gt_diff"], "```", "",
    ]
    spent = 0
    missing = [f for f in _touched_files(manifest["gt_diff"]) if f not in files]
    for rel, text in files.items():
        if spent + len(text) > FILE_BUDGET_CHARS:
            missing.append(rel)
            continue
        spent += len(text)
        parts += [f"## Source (pre-fix): {rel}", "```c", text, "```", ""]
    if missing:
        parts += [f"(source not included for: {', '.join(sorted(set(missing)))})", ""]
    return "\n".join(parts)


def _complete(prompt: str, model: str, base_url: str, api_key: str) -> str:
    """One chat completion, structured if the gateway honours it, else JSON mode."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": INSTRUCTIONS},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        # No max_tokens on purpose. kimi is a reasoning model and this is a
        # one-shot annotation whose quality is the whole point -- capping the
        # budget truncates the reasoning, and the failure mode is silent
        # (`content: null` with finish_reason "stop", not an error).
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "root_cause", "schema": SCHEMA, "strict": True},
        },
    }

    def post(payload):
        # The gateway times out sporadically and independently of prompt size, so
        # a transient failure must not end a 300-instance batch. Retried with
        # backoff; a genuine error still surfaces after the last attempt.
        last = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(
                    f"{base_url}/chat/completions",
                    data=json.dumps(payload).encode(),
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=600) as r:
                    return json.load(r)
            except Exception as e:
                last = e
                if attempt < 3:
                    time.sleep(5 * 2 ** attempt)
        raise last

    try:
        resp = post(body)
    except Exception:
        body["response_format"] = {"type": "json_object"}
        resp = post(body)

    choice = resp["choices"][0]
    usage = resp.get("usage") or {}
    text = (choice["message"].get("content") or "").strip()
    if not text:
        raise RuntimeError(
            f"empty completion (finish_reason={choice.get('finish_reason')}, "
            f"reasoning={len(choice['message'].get('reasoning') or '')} chars, "
            f"usage={usage})"
        )
    cause = None
    try:
        value = json.loads(text)["root_cause"]
        cause = value.strip() if isinstance(value, str) else None
    except Exception:
        # Some gateways ignore response_format entirely and return prose.
        m = re.search(r'"root_cause"\s*:\s*"(.+?)"\s*[},]', text, re.S)
        cause = (m.group(1) if m else text).strip()
    # A cause that still looks like JSON is a parse failure wearing the answer's
    # clothes: `{"root_cause": null}` survives both branches above and would be
    # rendered verbatim into the agent's prompt as the root-cause hint.
    if not cause or cause.lstrip().startswith(("{", "[")):
        raise RuntimeError(f"could not extract root_cause from response: {text[:200]!r}")
    return cause, usage, len(choice["message"].get("reasoning") or "")


def _review(cause: str, manifest: dict) -> list[str]:
    """Sanity checks: grounded in the instance, not a restatement of the edit."""
    notes = []
    n = len(cause)
    if n < TARGET_CHARS[0]:
        notes.append(f"SHORT ({n} chars)")
    elif n > TARGET_CHARS[1]:
        notes.append(f"LONG ({n} chars)")
    if m := _REMEDY.search(cause):
        notes.append(f"REMEDY-PHRASING {m.group(0)!r}")
    added = [l[1:].strip() for l in manifest["gt_diff"].splitlines()
             if l.startswith("+") and not l.startswith("+++") and len(l.strip()) > 25]
    if any(a in cause for a in added):
        notes.append("QUOTES-PATCH")
    idents = {m.group(0) for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]{4,}", manifest["gt_diff"])}
    if not (idents & set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{4,}", cause))):
        notes.append("NO-SHARED-IDENTIFIER")
    return notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task-ids", nargs="+", default=None,
                    help="Instances to annotate (default: every id in --instance-list)")
    ap.add_argument("--instance-list", type=Path, default=DEFAULT_INSTANCE_LIST,
                    help=f"Locked evaluation set (default: {DEFAULT_INSTANCE_LIST.name}). "
                         "Ids outside it are refused: an annotation is the gold_auto "
                         "arm's cause, so annotating off-set instances is wasted spend.")
    ap.add_argument("--out", type=Path, required=True, help="e.g. reports/gold_auto")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--manifest-dir", type=Path, default=Path("manifests"))
    ap.add_argument("--no-source", action="store_true",
                    help="diff and sanitizer report only; skip reading files from the image")
    ap.add_argument("--overwrite", action="store_true",
                    help="re-annotate instances that already have a file in --out")
    ap.add_argument("--resume", action="store_true",
                    help="annotate only instances with no file in --out yet. A failed "
                         "instance writes nothing, so this picks up exactly the ones "
                         "that did not finish -- rerun until it reports 0 remaining.")
    ap.add_argument("--workers", type=int, default=2,
                    help="instances annotated concurrently (default: 2). Both slow "
                         "legs are I/O waits, so this is close to a linear speedup; "
                         "raise it only as far as the endpoint tolerates.")
    args = ap.parse_args()

    if args.workers < 1:
        print("--workers must be >= 1", file=sys.stderr)
        return 2

    base_url = (os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not (base_url and api_key):
        print("set OPENAI_BASE_URL and OPENAI_API_KEY (source .env)", file=sys.stderr)
        return 2

    locked = [str(i) for i in json.loads(args.instance_list.read_text())]
    if args.task_ids:
        off = [t for t in args.task_ids if t not in set(locked)]
        if off:
            print(f"{len(off)} id(s) not in {args.instance_list.name}: "
                  f"{', '.join(off[:10])}", file=sys.stderr)
            return 2
        task_ids = args.task_ids
    else:
        task_ids = locked
    if args.resume and args.overwrite:
        print("--resume and --overwrite are mutually exclusive", file=sys.stderr)
        return 2
    print(f"{len(task_ids)} instance(s) from {args.instance_list.name} "
          f"({len(locked)} locked)")

    prior_flagged: list[str] = []
    if args.resume:
        # Filter here rather than leaning on the per-instance skip: at 500
        # instances that would print several hundred skip lines and report a
        # remaining count of zero as if it had done the work.
        pending = [t for t in task_ids if not (args.out / f"{t}.json").exists()]
        print(f"resume: {len(task_ids) - len(pending)} already annotated, "
              f"{len(pending)} remaining")
        # A resume run must account for annotations earlier runs already flagged.
        # Counting only this run's notes would let the documented "rerun until 0
        # remaining" loop end in exit 0 while flagged causes sit on disk, ready to
        # ship into the gold_auto prompt.
        pending_set = set(pending)
        prior_flagged = []
        for t in (t for t in task_ids if t not in pending_set):
            try:
                if json.loads((args.out / f"{t}.json").read_text()).get("cause_notes"):
                    prior_flagged.append(t)
            except Exception:
                prior_flagged.append(t)  # unreadable is not "done"
        if prior_flagged:
            print(f"resume: {len(prior_flagged)} existing annotation(s) carry review "
                  f"flags and need attention: {', '.join(prior_flagged[:10])}")
        task_ids = pending
        if not task_ids:
            print("nothing to do")
            return 1 if prior_flagged else 0

    # Annotating needs a frozen manifest for gt_diff, crash_output and the image.
    # Say up front how many are missing rather than emitting one failure per
    # instance several hundred lines deep.
    absent = [t for t in task_ids if not (args.manifest_dir / f"{t}.json").exists()]
    if absent:
        print(f"WARNING: {len(absent)} of {len(task_ids)} have no manifest in "
              f"{args.manifest_dir} and will fail; freeze them first "
              f"(e.g. {absent[0]})")

    args.out.mkdir(parents=True, exist_ok=True)
    failed = len(prior_flagged)
    errors = []

    # One permit per concurrent annotation, held across both slow legs (the
    # per-file `docker run` reads and the completion) since they are one unit of
    # work. Redundant while the pool is capped at the same number -- it cannot
    # block today -- and kept as the explicit bound so raising the pool size does
    # not silently unbound the expensive section. Instances never share an image,
    # so the docker reads do not contend for a pull.
    slots = threading.Semaphore(args.workers)
    out_lock = threading.Lock()

    def emit(*lines: str) -> None:
        """Keep one instance's block together; threads interleave otherwise."""
        with out_lock:
            for line in lines:
                print(line)

    def annotate(tid: str):
        """Returns (flagged, error) -- error is None on success or skip."""
        dest = args.out / f"{tid}.json"
        if dest.exists() and not args.overwrite:
            emit(f"=== {tid}  skip (exists; --overwrite to redo)")
            return False, None
        with slots:
            try:
                manifest = json.loads((args.manifest_dir / f"{tid}.json").read_text())
                rels = _touched_files(manifest["gt_diff"])
                files = {} if args.no_source else _read_from_image(manifest, rels)
                prompt = _build_prompt(manifest, files)
                cause, usage, reasoning_chars = _complete(
                    prompt, args.model, base_url, api_key)
                notes = _review(cause, manifest)

                report = gold_report(manifest)
                # gold_report() ids every report `gold-<id>`, so without this the
                # hand-authored and model-written arms are indistinguishable in a
                # file that outlives the run that made it.
                report["report_id"] = f"{args.out.name}-{args.model}-{tid}"
                report["cause"] = cause
                report["cause_model"] = args.model
                # Provenance records what the model actually saw, not what was
                # asked for: files can be skipped when the image lays the source
                # out under a different root, and an arm's behaviour is later
                # attributed to this field. The `n/m files` stdout line is not
                # persisted; this is.
                report["cause_source"] = "diff+source" if files else "diff"
                report["cause_files"] = sorted(files)
                report["cause_usage"] = {**usage, "reasoning_chars": reasoning_chars}
                # Review notes travel with the annotation. The exit code is lost
                # as soon as the batch ends, and a rerun skips files that already
                # exist, so a flagged cause would otherwise ship into the agent
                # prompt with nothing on disk recording that it was flagged.
                report["cause_notes"] = notes
                # Atomic: --resume treats "file exists" as "finished", so a
                # truncated write from a kill or a full disk would be counted as
                # done and only surface much later as a JSONDecodeError at task
                # generation. os.replace is atomic within a filesystem.
                tmp = dest.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(report, indent=2) + "\n")
                os.replace(tmp, dest)

                emit(
                    f"=== {tid} {manifest['project']}  ({len(files)}/{len(rels)} files, "
                    f"prompt {len(prompt):,} chars)",
                    f"    {cause}",
                    f"    reasoning {reasoning_chars:,} chars, "
                    f"completion {usage.get('completion_tokens','?')} tok",
                    f"    -> {dest}   "
                    f"{'OK' if not notes else 'CHECK: ' + '; '.join(notes)}",
                )
                return bool(notes), None
            except Exception as e:
                # One bad instance must not cost the batch. Rerun the script to
                # fill gaps: completed instances are skipped unless --overwrite.
                emit(f"=== {tid}  FAILED {type(e).__name__}: {e}")
                return False, (tid, f"{type(e).__name__}: {e}")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for flagged, err in pool.map(annotate, task_ids):
            failed += flagged
            if err:
                errors.append(err)

    if errors:
        print(f"\n{len(errors)} failed: {', '.join(t for t, _ in errors)}")
    return 1 if (failed or errors) else 0


if __name__ == "__main__":
    sys.exit(main())
