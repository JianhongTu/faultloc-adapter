"""Scorer vendored from flbench.eval.

types.py and ground_truth.py are byte-identical copies of the FLBench originals.
Do not edit them: parity is meaningless if the metric is reimplemented. Re-vendor
from FLBench instead.

metrics.py carries exactly one divergence: the `full_path` argument, which fixes
a defect rather than restating the metric. Upstream compares bare filenames, so a
prediction can be credited for naming a different file that happens to share a
final component. The default is the upstream behaviour, and scripts/scorer_parity.py
replays the archived FLBench predictions through that default and still reproduces
all six stored metrics exactly -- which is the property the verbatim rule exists to
protect. Any further divergence needs the same treatment: an opt-in flag, a default
that keeps parity green, and a reason written down. See metrics.py's docstring.
"""

from .ground_truth import parse_diff
from .metrics import evaluate_benchmark, evaluate_sample
from .types import Hunk, Span

__all__ = ["Hunk", "Span", "parse_diff", "evaluate_sample", "evaluate_benchmark"]
