"""Core data types for the FLBench evaluation module."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Hunk:
    """One @@ section from a patch, expressed as original-file line numbers.

    lines: set of line numbers in the VULNERABLE (pre-patch) version that are
           the ground truth for this hunk.
           - Deletion/modification hunks: the deleted lines.
           - Addition-only hunks: the last context line before the insertion.
    addition_only: True when the hunk has no deleted lines (pure insertion).
    """

    file: str
    lines: frozenset[int]
    is_primary: bool = True
    addition_only: bool = False


@dataclass(frozen=True)
class Span:
    """One predicted suspicious region from the agent.

    The agent produces a list of Spans representing all locations it considers
    suspicious. A confident agent emits fewer, tighter spans; a less confident
    agent may include more locations.

    file:       path relative to the project root, matching the diff header
                convention (e.g. "src/vm.c").
    line_start: first line of the span, 1-based inclusive.
    line_end:   one past the last line, i.e. the span covers [line_start, line_end).
    """

    file: str
    line_start: int
    line_end: int
