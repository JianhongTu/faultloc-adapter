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

THE ONE DIVERGENCE FROM FLBENCH: `full_path`. Upstream compares only the final
filename component, so `include/dav1d/picture.h` and `src/picture.h` are the same
file and a prediction naming the wrong one at the right line scores 1.0. Three
ARVO instances carry exactly that collision (42523221, 42477855, 42477322).
`full_path=True` compares whole directory paths instead. The default is False,
which is the upstream behaviour byte for byte, so scripts/scorer_parity.py still
reproduces the published numbers exactly and the parity columns stay meaningful.
Callers opt in per metric family; see score.py and repair_score.py.

WHAT IT DOES NOT DO IS COMPARE PATHS LITERALLY. The two sides are rooted
differently far more often than they collide: prompt.md asks for project-relative
paths, but an agent looking at /src/aom writes `src/aom/av1/common/x.c` where the
diff header says `av1/common/x.c`. Measured over the 10508 archived FLBench
predictions, literal equality changes 730 of them and 699 of those are that
rooting difference -- it would have traded a defect affecting 3 instances for a
false negative affecting 7% of all predictions. So a predicted path resolves to a
GT path when one is a suffix of the other at a component boundary, which is
rooting-invariant, and only when the match is UNAMBIGUOUS: a path that suffixes
two GT files names neither, which is the collision case and still scores zero.
"""

import posixpath

from .types import Hunk, Span


def _norm(path: str) -> str:
    """Collapse a path to its canonical project-relative form.

    `./src/x.c`, `src//x.c` and `/src/x.c` all name the same file and must not
    miss each other merely because the diff header and the agent wrote them
    differently.
    """
    return posixpath.normpath(path).lstrip("/")


def _alias(spans: list[Span], hunks: list[Hunk]) -> dict[str, str]:
    """Map each predicted path onto the GT path it unambiguously names.

    Suffix at a component boundary, in either direction: the prediction may be
    rooted deeper than the diff header (`src/aom/av1/x.c` vs `av1/x.c`) or
    shallower. Two candidates means the prediction does not identify a file --
    that is `picture.h` against both `src/picture.h` and `include/dav1d/picture.h`
    -- so it is left unresolved and matches nothing.
    """
    gt = {_norm(h.file) for h in hunks}
    alias: dict[str, str] = {}
    for span in spans:
        pred = _norm(span.file)
        if pred in gt or pred in alias:
            continue
        hits = [g for g in gt if pred.endswith("/" + g) or g.endswith("/" + pred)]
        if len(hits) == 1:
            alias[pred] = hits[0]
    return alias


def _key(path: str, full_path: bool, alias: dict[str, str] | None = None) -> str:
    """The identity a path is compared under: whole path, or filename only."""
    if not full_path:
        return path.rsplit("/", 1)[-1]
    norm = _norm(path)
    return alias.get(norm, norm) if alias else norm


def _expand_spans(
    spans: list[Span], full_path: bool, alias: dict[str, str] | None = None
) -> set[tuple[str, int]]:
    """Expand a list of spans to a flat set of (file, line) points."""
    result: set[tuple[str, int]] = set()
    for span in spans:
        for line in range(span.line_start, span.line_end):
            result.add((_key(span.file, full_path, alias), line))
    return result


def _expand_hunks(hunks: list[Hunk], full_path: bool) -> set[tuple[str, int]]:
    """Flatten GT hunks to a set of (file, line) points."""
    return {(_key(hunk.file, full_path), line) for hunk in hunks for line in hunk.lines}


def evaluate_sample(
    spans: list[Span], hunks: list[Hunk], full_path: bool = False
) -> dict[str, float]:
    """Compute all metrics for a single benchmark sample.

    Args:
        spans: Agent's predicted suspicious spans for this sample.
        hunks: Ground-truth hunks derived from the patch (via parse_diff).
        full_path: Compare whole directory paths instead of bare filenames,
            rooting-invariantly. False is the FLBench behaviour; see the module
            docstring.

    Returns:
        Dict with keys: file_recall, hunk_recall, line_recall,
        line_precision, line_f1, iou.
    """
    if not hunks:
        raise ValueError("Ground truth must contain at least one hunk.")

    alias = _alias(spans, hunks) if full_path else None
    pred_lines = _expand_spans(spans, full_path, alias)
    gt_lines = _expand_hunks(hunks, full_path)

    # --- File recall ---
    gt_files = {_key(h.file, full_path) for h in hunks}
    pred_files = {_key(s.file, full_path, alias) for s in spans}
    file_recall = len(gt_files & pred_files) / len(gt_files)

    # --- Hunk recall (macro-averaged binary hit/miss) ---
    hunk_hits = [
        float(bool({(_key(h.file, full_path), l) for l in h.lines} & pred_lines))
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
    full_path: bool = False,
) -> dict[str, float]:
    """Macro-average evaluate_sample across all benchmark samples.

    Args:
        samples: List of (spans, hunks) pairs, one per benchmark sample.
        full_path: Passed through to evaluate_sample.

    Returns:
        Dict with the same keys as evaluate_sample, macro-averaged.
    """
    if not samples:
        return {}
    results = [evaluate_sample(spans, hunks, full_path) for spans, hunks in samples]
    keys = results[0].keys()
    return {k: sum(r[k] for r in results) / len(results) for k in keys}
