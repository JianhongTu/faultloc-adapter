#!/usr/bin/env python3
"""Freeze data/alternative-patch-eval50.json -- the 50-instance alternative-patch sample.

The alternative-patch study asks whether valid repair locations outside the
developer patch change the benchmark's model ranking. That is a bounded
sensitivity analysis on 10% of the locked eval500 set, so the subset has to be
defensible as representative of the whole before any patch is generated from it.

STRATA. Sanitizer x developer-patch topology, six cells, all non-empty:

    asan/multi 172  asan/single 190  msan/multi 23
    msan/single 38  ubsan/multi 48   ubsan/single 29

Both axes are chosen because they are what an alternative repair has to survive.
The sanitizer decides what counts as suppression and how the failure is observed;
the topology decides how much room there is to move -- a single-hunk patch names
one place the developer thought was wrong, and finding a second one is a stronger
claim than it is for a patch already spread across several. Sampling either axis
away would let the funnel in section 10 be read as a property of memory safety
when it was a property of, say, ubsan.

Topology is the number of `@@` blocks in the developer diff, not the number of
edit runs. git merges edits within a few context lines into one block, so the two
differ often (see repair.gold_report) -- but the block count is what "the
developer changed one place" means to a reader, and the run count is an artifact
of diff formatting.

ALLOCATION. Largest-remainder: floor each stratum's proportional quota, then hand
the 3 leftover slots to the largest fractional parts, ties broken by stratum name.
Proportional rather than the flattest allocation eval500 itself used -- that set
was being built to measure across fault categories, so flattening was the point;
this one is a subset that has to stand in for eval500 as it actually is.

SELECTION. Within a stratum, instances are ordered by sha256("<seed>:<local_id>")
and the first k taken. A seeded shuffle would do as well today, but the hash order
is stable across Python versions, platforms and languages, and it nests: raising
the sample to 75 later keeps these 50 and adds 25, so the first result is never
invalidated by extending the search.

Nothing here reads a prediction, a score or a model output. Section 3 requires the
sample to be frozen before any of that exists, and the only inputs are manifests/.

    uv run python scripts/select_repair_eval50.py            # write
    uv run python scripts/select_repair_eval50.py --check    # verify committed
"""

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from faultloc_adapter.anchoring import parse_diff_anchored  # noqa: E402

TARGET = 50
SEED = "20260731"
DEFAULT_OUT = ROOT / "data" / "alternative-patch-eval50.json"


def topology(gt_diff: str) -> str:
    return "single-hunk" if len(parse_diff_anchored(gt_diff)) == 1 else "multi-hunk"


def order_key(local_id: int) -> str:
    return hashlib.sha256(f"{SEED}:{local_id}".encode()).hexdigest()


def allocate(avail: dict[str, int], target: int) -> dict[str, int]:
    """Largest-remainder apportionment of `target` across strata."""
    total = sum(avail.values())
    quotas = {s: n * target / total for s, n in avail.items()}
    alloc = {s: int(q) for s, q in quotas.items()}
    # Ties on the fractional part are broken by name so the result does not depend
    # on dict ordering, which would make it depend on the manifest glob.
    ranked = sorted(alloc, key=lambda s: (-(quotas[s] - alloc[s]), s))
    for s in ranked[: target - sum(alloc.values())]:
        alloc[s] += 1
    # Cannot fire while target < total: ceil(n * target / total) <= n for n >= 1.
    assert all(alloc[s] <= avail[s] for s in alloc), "quota exceeds availability"
    return alloc


def select(manifest_dir: Path) -> tuple[list[int], dict]:
    strata: dict[str, list[dict]] = {}
    for path in sorted(manifest_dir.glob("*.json")):
        m = json.loads(path.read_text())
        stratum = f"{m['sanitizer']}|{topology(m['gt_diff'])}"
        strata.setdefault(stratum, []).append(
            {"local_id": m["local_id"], "project": m["project"]}
        )

    alloc = allocate({s: len(v) for s, v in strata.items()}, TARGET)

    chosen: list[dict] = []
    for stratum, members in sorted(strata.items()):
        members.sort(key=lambda r: order_key(r["local_id"]))
        for r in members[: alloc[stratum]]:
            chosen.append({**r, "stratum": stratum})

    chosen.sort(key=lambda r: r["local_id"])
    return [r["local_id"] for r in chosen], {
        "avail": {s: len(v) for s, v in strata.items()},
        "alloc": alloc,
        "chosen": chosen,
    }


def build_manifest(instance_ids: list[int], stats: dict) -> dict:
    """The published sample: ids plus everything section 3 says to publish with them."""
    return {
        "seed": SEED,
        "target": TARGET,
        "stratified_by": ["sanitizer", "developer_patch_topology"],
        "selection_rule": "sha256('<seed>:<local_id>') ascending within stratum",
        "allocation_rule": "largest remainder, ties by stratum name",
        "strata": {
            s: {"available": stats["avail"][s], "selected": stats["alloc"][s]}
            for s in sorted(stats["avail"])
        },
        "projects": dict(sorted(Counter(r["project"] for r in stats["chosen"]).items())),
        "instance_strata": {str(r["local_id"]): r["stratum"] for r in stats["chosen"]},
        "instance_ids": instance_ids,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--manifests", type=Path, default=ROOT / "manifests")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true",
                    help="verify the committed sample matches, do not write")
    args = ap.parse_args()

    instance_ids, stats = select(args.manifests)
    manifest = build_manifest(instance_ids, stats)

    pool = sum(stats["avail"].values())
    print(f"{'stratum':<22}{'avail':>6}{'pool %':>8}{'sel':>5}{'50 %':>7}")
    print("-" * 48)
    for s in sorted(stats["avail"]):
        n, k = stats["avail"][s], stats["alloc"][s]
        print(f"{s:<22}{n:>6}{100*n/pool:>7.1f}%{k:>5}{100*k/TARGET:>6.1f}%")

    projects = manifest["projects"]
    print(f"\nselected {len(instance_ids)} of {pool}"
          f"   projects {len(projects)}"
          f"   max per project {max(projects.values())}")

    if len(instance_ids) != TARGET:
        print(f"FAIL: expected {TARGET}, got {len(instance_ids)}")
        return 1

    if args.check:
        if not args.out.exists():
            print(f"FAIL: {args.out} does not exist")
            return 1
        same = json.loads(args.out.read_text()) == manifest
        print(f"\n{'MATCH' if same else 'MISMATCH'}: {args.out}")
        return 0 if same else 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
