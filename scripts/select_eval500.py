#!/usr/bin/env python3
"""Rebuild data/eval500_instance_list.json -- the locked 500-instance eval set.

WHY A NEW SET. FLBench's 900-instance list is heavily skewed by fault category:
Heap-buffer-overflow alone is 28%, the top four categories are 71%, and 12 of the
26 categories carry four instances or fewer. Measuring localization across fault
types on that distribution mostly measures performance on heap overflows.

THE RULE. Categories are FLBench's own (scripts/dataset_stats.py:crash_category --
the first whitespace-delimited token of crash_type), with two deviations:

  * UNKNOWN is split by access type into UNKNOWN-READ / UNKNOWN-WRITE. "UNKNOWN"
    is ASAN failing to attribute the access rather than a fault type, and leaving
    it whole hands a non-category an 18% share. The access direction is still a
    real signature, so the instances are kept rather than dropped.
  * Three instances whose crash_type is a diagnostic message rather than a fault
    are dropped -- see DROP_CRASH_TYPES.

Balance is max-min (water-filling): take the largest cap k with
sum(min(count, k)) <= 500, keep every category whole below k, cap the rest, then
spend any remainder on whichever category is currently smallest. This is the
flattest 500 the pool admits -- it takes the top category from 28% to 11%.

CONSTRAINTS. All 100 ablation instances are included (they are a hard subset).
Within a category, ablation members are taken first and the remainder is filled
round-robin across projects, so no category ends up dominated by one codebase.
Fully deterministic -- no RNG, ties broken by (project, localId).

    uv run python scripts/select_eval500.py --flbench ~/codes/FLBench --check
"""

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

TARGET = 500

# crash_type values that are sanitizer diagnostics, not fault categories. Each
# would otherwise become its own singleton category via the first-token rule
# ("Bad", "Check", "Nested").
DROP_CRASH_TYPES = {
    "Bad parameters to --sanitizer-annotate-contiguous-container",
    "Check failed",
    "Nested bug in the same thread, aborting.",
}

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "eval500_instance_list.json"


def category(crash_type: str) -> str:
    """FLBench's first-token rule, with UNKNOWN split by access type."""
    if not crash_type or str(crash_type) == "nan":
        return "unknown"
    parts = str(crash_type).split()
    if parts[0] == "UNKNOWN":
        return f"UNKNOWN-{parts[1]}" if len(parts) > 1 else "UNKNOWN"
    return parts[0]


def water_fill(avail: dict[str, int], target: int) -> dict[str, int]:
    """Flattest allocation summing to `target` without exceeding availability."""
    cap = max(k for k in range(1, target + 1)
              if sum(min(v, k) for v in avail.values()) <= target)
    alloc = {k: min(v, cap) for k, v in avail.items()}
    # Spend the remainder on whichever category is smallest and still has room,
    # rather than handing every spare slot to the largest one.
    for _ in range(target - sum(alloc.values())):
        k = min((c for c in avail if alloc[c] < avail[c]),
                key=lambda c: (alloc[c], c), default=None)
        if k is None:
            break
        alloc[k] += 1
    return alloc


def select(flbench: Path, target: int = TARGET) -> tuple[list[int], dict]:
    eval_ids = json.loads((flbench / "data" / "eval_instance_list.json").read_text())
    ablation = set(json.loads(
        (flbench / "data" / "ablation_eval_instance_list.json").read_text()))
    if not ablation <= set(eval_ids):
        raise ValueError("ablation list is not a subset of the eval list")

    con = sqlite3.connect(flbench / "data" / "arvo.db")
    q = ",".join("?" * len(eval_ids))
    rows = con.execute(
        f"SELECT localId, project, crash_type FROM arvo WHERE localId IN ({q})",
        tuple(eval_ids),
    ).fetchall()
    con.close()

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for lid, project, crash_type in rows:
        if crash_type in DROP_CRASH_TYPES:
            continue
        by_cat[category(crash_type)].append({"id": lid, "project": project or "unknown"})

    alloc = water_fill({k: len(v) for k, v in by_cat.items()}, target)

    selected: list[int] = []
    for cat, members in sorted(by_cat.items()):
        must = sorted(r["id"] for r in members if r["id"] in ablation)
        # Ablation membership outranks the cap; widening here keeps the set valid
        # rather than silently dropping a pinned instance.
        alloc[cat] = max(alloc[cat], len(must))
        selected.extend(must)

        slots = alloc[cat] - len(must)
        pool: dict[str, list[int]] = defaultdict(list)
        for r in members:
            if r["id"] not in ablation:
                pool[r["project"]].append(r["id"])
        for ids in pool.values():
            ids.sort()
        taken = Counter(r["project"] for r in members if r["id"] in ablation)
        while slots > 0:
            order = sorted((p for p in pool if pool[p]), key=lambda p: (taken[p], p))
            if not order:
                break
            for p in order:
                if slots == 0:
                    break
                selected.append(pool[p].pop(0))
                taken[p] += 1
                slots -= 1

    selected = sorted(set(selected))
    stats = {
        "by_cat": by_cat,
        "ablation": ablation,
        "eval_ids": eval_ids,
        "selected_cats": Counter(
            cat for cat, members in by_cat.items()
            for r in members if r["id"] in set(selected)
        ),
    }
    return selected, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flbench", type=Path, default=Path.home() / "codes" / "FLBench")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true",
                    help="verify the committed list matches, do not write")
    args = ap.parse_args()

    selected, stats = select(args.flbench)
    by_cat, ablation = stats["by_cat"], stats["ablation"]
    sel = set(selected)

    total = sum(len(v) for v in by_cat.values())
    print(f"{'category':<32}{'avail':>6}{'pool %':>8}{'sel':>6}{'500 %':>8}")
    print("-" * 60)
    counts = Counter({k: len(v) for k, v in by_cat.items()})
    for cat, n in counts.most_common():
        s = sum(1 for r in by_cat[cat] if r["id"] in sel)
        print(f"{cat:<32}{n:>6}{100*n/total:>7.1f}%{s:>6}{100*s/len(selected):>7.1f}%")

    top = max(counts, key=lambda c: sum(1 for r in by_cat[c] if r["id"] in sel))
    top_n = sum(1 for r in by_cat[top] if r["id"] in sel)
    print(f"\nselected {len(selected)}   ablation {len(ablation & sel)}/{len(ablation)}"
          f"   categories {sum(1 for c in by_cat if any(r['id'] in sel for r in by_cat[c]))}"
          f"/{len(by_cat)}")
    print(f"max category share {100*top_n/len(selected):.1f}% ({top})")

    if len(selected) != TARGET:
        print(f"FAIL: expected {TARGET}, got {len(selected)}")
        return 1
    if not ablation <= sel:
        print(f"FAIL: {len(ablation - sel)} ablation instances missing")
        return 1

    if args.check:
        if not args.out.exists():
            print(f"FAIL: {args.out} does not exist")
            return 1
        committed = json.loads(args.out.read_text())
        same = committed == selected
        print(f"\n{'MATCH' if same else 'MISMATCH'}: {args.out}")
        return 0 if same else 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(selected, indent=0) + "\n")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
