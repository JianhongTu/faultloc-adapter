"""Harbor verifier entrypoint: score prediction.json against the ground-truth patch.

Runs inside the separate verifier environment. All six FLBench metrics are
emitted as named Harbor rewards; `reward` mirrors `iou`, the headline metric.

A missing or malformed prediction is recorded distinctly from a scored zero
(`prediction_missing` / `prediction_invalid`), because the reference pipeline
uploads nothing in that case and collapsing the two would inflate the
denominator.
"""

import argparse
import json
import sys
from pathlib import Path

try:  # inside the verifier image the scorer sits next to this file
    from scorer import Span, evaluate_sample, parse_diff
except ImportError:  # running from the adapter package
    from .scorer import Span, evaluate_sample, parse_diff

METRIC_KEYS = ("iou", "hunk_recall", "file_recall", "line_recall", "line_precision", "line_f1")


def parse_prediction(raw: str) -> tuple[list[Span], int]:
    """Parse prediction JSON into spans, mirroring flbench.eval.score._parse_prediction.

    That function silently drops any span that is not a dict, has no file, has
    non-int bounds, or is zero-width (``line_end <= line_start``). The filter must
    match exactly: a dropped span still contributes its file to `file_recall` if
    kept, so keeping one inflates that metric relative to FLBench. Line-level
    metrics are unaffected either way, because ``range(ls, le)`` is empty.

    Returns (spans, dropped) so the drop count can be surfaced as a reward rather
    than lost silently.
    """
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("prediction must be a JSON array")
    spans, dropped = [], 0
    for item in data:
        if isinstance(item, dict):
            file = item.get("file", "")
            ls, le = item.get("line_start"), item.get("line_end")
            if file and isinstance(ls, int) and isinstance(le, int) and le > ls:
                spans.append(Span(str(file), ls, le))
                continue
        dropped += 1
    return spans, dropped


def load_prediction(path: Path) -> tuple[list[Span], int, str | None]:
    """Return (spans, dropped, failure); failure is None when the file parsed."""
    if not path.exists():
        return [], 0, "missing"
    try:
        spans, dropped = parse_prediction(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return [], 0, "invalid"
    return spans, dropped, None


def score(prediction_path: Path, ground_truth_path: Path) -> dict:
    hunks = parse_diff(ground_truth_path.read_text())
    if not hunks:
        raise ValueError(f"{ground_truth_path}: ground truth has no hunks")

    spans, dropped, failure = load_prediction(prediction_path)
    rewards = {
        "prediction_missing": int(failure == "missing"),
        "prediction_invalid": int(failure == "invalid"),
        "spans_dropped": dropped,
    }
    if failure is not None:
        rewards.update({key: 0.0 for key in METRIC_KEYS})
        rewards["hunk_hit"] = 0
    else:
        metrics = evaluate_sample(spans, hunks)
        rewards.update({key: metrics[key] for key in METRIC_KEYS})
        # Binary companion to the continuous headline reward, so pass@k stays
        # meaningful: 1 when at least one ground-truth hunk was covered.
        rewards["hunk_hit"] = int(metrics["hunk_recall"] > 0)
    return {"reward": rewards["iou"], **rewards}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prediction", type=Path, required=True)
    parser.add_argument("--ground-truth", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    rewards = score(args.prediction, args.ground_truth)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rewards, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
