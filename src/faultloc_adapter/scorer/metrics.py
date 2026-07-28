"""Evaluation metrics for span-based fault localization predictions.

The agent produces a set of suspicious spans {file, line_start, line_end}.
Ground truth is a list of Hunks (sets of lines in the vulnerable version).

Metrics (all in [0, 1], higher is better):

  file_recall     — fraction of GT files covered by predicted spans
  hunk_recall     — fraction of GT hunks where ≥1 GT line falls in a predicted
                    span; macro-averaged across hunks
  line_recall     — |GT_lines ∩ pred_lines| / |GT_lines|
  line_precision  — |GT_lines ∩ pred_lines| / |pred_lines|
  line_f1         — harmonic mean of line_recall and line_precision
  iou             — |GT_lines ∩ pred_lines| / |GT_lines ∪ pred_lines|
"""

from .types import Hunk, Span


def _fname(path: str) -> str:
    """Return only the filename component of a path."""
    return path.rsplit("/", 1)[-1]


def _expand_spans(spans: list[Span]) -> set[tuple[str, int]]:
    """Expand a list of spans to a flat set of (filename, line) points."""
    result: set[tuple[str, int]] = set()
    for span in spans:
        for line in range(span.line_start, span.line_end):
            result.add((_fname(span.file), line))
    return result


def _expand_hunks(hunks: list[Hunk]) -> set[tuple[str, int]]:
    """Flatten GT hunks to a set of (filename, line) points."""
    return {(_fname(hunk.file), line) for hunk in hunks for line in hunk.lines}


def evaluate_sample(spans: list[Span], hunks: list[Hunk]) -> dict[str, float]:
    """Compute all metrics for a single benchmark sample.

    Args:
        spans: Agent's predicted suspicious spans for this sample.
        hunks: Ground-truth hunks derived from the patch (via parse_diff).

    Returns:
        Dict with keys: file_recall, hunk_recall, line_recall,
        line_precision, line_f1, iou.
    """
    if not hunks:
        raise ValueError("Ground truth must contain at least one hunk.")

    pred_lines = _expand_spans(spans)
    gt_lines = _expand_hunks(hunks)

    # --- File recall ---
    gt_files = {_fname(h.file) for h in hunks}
    pred_files = {_fname(s.file) for s in spans}
    file_recall = len(gt_files & pred_files) / len(gt_files)

    # --- Hunk recall (macro-averaged binary hit/miss) ---
    hunk_hits = [
        float(bool({(_fname(h.file), l) for l in h.lines} & pred_lines))
        for h in hunks
    ]
    hunk_recall = sum(hunk_hits) / len(hunk_hits)

    # --- Line-level metrics ---
    intersection = len(gt_lines & pred_lines)

    line_recall = intersection / len(gt_lines) if gt_lines else 0.0
    line_precision = intersection / len(pred_lines) if pred_lines else 0.0
    line_f1 = (
        2 * line_precision * line_recall / (line_precision + line_recall)
        if (line_precision + line_recall) > 0
        else 0.0
    )

    union = len(gt_lines | pred_lines)
    iou = intersection / union if union > 0 else 0.0

    return {
        "file_recall": file_recall,
        "hunk_recall": hunk_recall,
        "line_recall": line_recall,
        "line_precision": line_precision,
        "line_f1": line_f1,
        "iou": iou,
    }


def evaluate_benchmark(
    samples: list[tuple[list[Span], list[Hunk]]],
) -> dict[str, float]:
    """Macro-average evaluate_sample across all benchmark samples.

    Args:
        samples: List of (spans, hunks) pairs, one per benchmark sample.

    Returns:
        Dict with the same keys as evaluate_sample, macro-averaged.
    """
    if not samples:
        return {}
    results = [evaluate_sample(spans, hunks) for spans, hunks in samples]
    keys = results[0].keys()
    return {k: sum(r[k] for r in results) / len(results) for k in keys}
