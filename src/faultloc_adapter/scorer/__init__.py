"""Scorer vendored verbatim from flbench.eval.

types.py, ground_truth.py and metrics.py are byte-identical copies of the
FLBench originals. Do not edit them: parity is meaningless if the metric is
reimplemented. Re-vendor from FLBench instead.
"""

from .ground_truth import parse_diff
from .metrics import evaluate_benchmark, evaluate_sample
from .types import Hunk, Span

__all__ = ["Hunk", "Span", "parse_diff", "evaluate_sample", "evaluate_benchmark"]
