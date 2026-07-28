"""Scorer parity gate: replay archived FLBench predictions through the vendored scorer.

The core migration gate (step 3 of doc/migration-parity-plan.md). FLBench's
`eval_runs` table stores every scored prediction together with the six metrics
`flbench.eval.score` computed for it. Re-scoring those same predictions with the
adapter's vendored scorer must reproduce all six values exactly.

Deterministic and offline: no agent, no LLM, no S3, no Docker.

    python scripts/scorer_parity.py --flbench ~/codes/FLBench
"""

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faultloc_adapter.score import parse_prediction  # noqa: E402
from faultloc_adapter.scorer import evaluate_sample, parse_diff  # noqa: E402

METRICS = ("file_recall", "hunk_recall", "line_recall", "line_precision", "line_f1", "iou")

# Stored metrics are SQLite REALs written by a separate process; compare within
# float noise rather than by identity, and report exact agreement separately.
TOLERANCE = 1e-9


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flbench", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None, help="Write the parity table as JSON")
    args = parser.parse_args()

    conn = sqlite3.connect(args.flbench / "data" / "arvo.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT local_id, run_id, config, prediction, {', '.join(METRICS)} "
        "FROM eval_runs WHERE prediction IS NOT NULL"
    ).fetchall()

    gt_cache: dict[int, list] = {}
    compared = 0
    exact = 0
    mismatches = []
    skipped = Counter()
    by_config = defaultdict(lambda: {"compared": 0, "mismatched": 0})
    worst = 0.0

    for row in rows:
        local_id = row["local_id"]
        if local_id not in gt_cache:
            diff_path = args.flbench / "eval-patches" / f"{local_id}.diff"
            # Same decoding as flbench.eval.score (score.py:61): some patches
            # are not valid UTF-8, and parity requires identical handling.
            gt_cache[local_id] = (
                parse_diff(diff_path.read_bytes().decode("utf-8", errors="replace"))
                if diff_path.exists()
                else None
            )
        hunks = gt_cache[local_id]
        if not hunks:
            skipped["no_ground_truth"] += 1
            continue

        try:
            spans, _dropped = parse_prediction(row["prediction"])
        except (json.JSONDecodeError, TypeError, ValueError):
            skipped["unparseable_prediction"] += 1
            continue

        ours = evaluate_sample(spans, hunks)
        compared += 1
        by_config[row["config"]]["compared"] += 1

        deltas = {m: abs(ours[m] - row[m]) for m in METRICS if row[m] is not None}
        if not deltas:
            skipped["no_stored_metrics"] += 1
            compared -= 1
            by_config[row["config"]]["compared"] -= 1
            continue

        worst = max(worst, max(deltas.values()))
        if all(d == 0.0 for d in deltas.values()):
            exact += 1
        if any(d > TOLERANCE for d in deltas.values()):
            by_config[row["config"]]["mismatched"] += 1
            mismatches.append(
                {
                    "local_id": local_id,
                    "run_id": row["run_id"],
                    "config": row["config"],
                    "offending": {
                        m: {"flbench": row[m], "harbor": ours[m], "delta": d}
                        for m, d in deltas.items()
                        if d > TOLERANCE
                    },
                }
            )

    print(f"compared         {compared}")
    print(f"exact agreement  {exact} ({100 * exact / compared:.4f}%)" if compared else "")
    print(f"mismatched       {len(mismatches)}")
    print(f"max |delta|      {worst:.3e}")
    for reason, count in sorted(skipped.items()):
        print(f"skipped ({reason}) {count}")
    print("\nby config:")
    for config in sorted(by_config):
        stats = by_config[config]
        print(f"  {config:<12} compared={stats['compared']:<6} mismatched={stats['mismatched']}")

    if mismatches:
        print("\nfirst mismatches:")
        for m in mismatches[:5]:
            print(f"  {m['local_id']} {m['run_id']} {m['config']}: {m['offending']}")

    if args.report:
        args.report.write_text(
            json.dumps(
                {
                    "compared": compared,
                    "exact": exact,
                    "mismatched": len(mismatches),
                    "max_abs_delta": worst,
                    "skipped": dict(skipped),
                    "by_config": {k: dict(v) for k, v in by_config.items()},
                    "mismatches": mismatches,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"\nreport written to {args.report}")

    verdict = "PASS" if not mismatches and compared else "FAIL"
    print(f"\nscorer parity: {verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
