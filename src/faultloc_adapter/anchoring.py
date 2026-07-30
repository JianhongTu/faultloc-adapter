"""The locked ground-truth anchoring rule. Every caller must go through here.

A pure insertion has no lines in the vulnerable version -- the lines it adds do
not exist in the file being scored. The vendored scorer resolves this by
anchoring the hunk to the last context line *before* the insertion, which is not
the defective line but the line the defect follows. Scoring a correct answer
against that anchor produces off-by-one false negatives.

THE RULE: an insertion sits *between* two lines and touches both, so its ground
truth is {L, L+1}, not {L}.

Precedent: Rezaalipour & Furia, EMSE'24 (arXiv 2305.19834 s4.2) -- "an add at l
is actually a modification between two other locations; therefore, the location
that immediately precedes l should also be part of the ground truth". This is
narrower than their published convention, which additionally skips blank/comment
lines and restricts the pair to the enclosing scope. 42% of ground-truth hunks
across the frozen set are addition-only, so this is not an edge case.

WHY IT LIVES HERE AND NOT IN THE SCORER. src/faultloc_adapter/scorer/ is a
verbatim copy of flbench.eval and must stay byte-identical -- parity with FLBench
is the only reason it exists. Widening is therefore a caller-side transform
applied to parse_diff's output.

WHICH STAGE APPLIES IT. The two stages deliberately differ, and getting this
backwards is the easiest way to break the benchmark:

  * Localization (score.py, adapter.py) keeps the UN-widened FLBench rule as its
    headline via parse_diff_flbench, and emits the widened values alongside under
    `gap_`. prompt.md tells the agent to name the line before an insertion, so
    prediction and ground truth already share a convention; widening the headline
    would break FLBench parity and penalise an agent for following instructions.
  * Repair (repair_score.py, repair.py) uses parse_diff_anchored as its headline.
    The agent writes a patch, not spans, so no convention can be disclosed to it,
    and its insertions are scored against report spans built from developer
    deletions. That mismatch is a real false negative, and the rule fixes it.

Widening only one side of a comparison is a silent no-op, and widening twice is
idempotent -- so a mistake here does not raise, it just collapses the parity
column into the headline and the numbers still look plausible. That is why every
caller goes through this module and why scripts/check_anchoring.py asserts each
stage's headline rule behaviourally rather than checking imports.
"""

try:  # inside a verifier image the scorer sits next to this file
    from scorer import Hunk, parse_diff as _parse_diff
    from scorer.ground_truth import _HUNK_HEADER, _NEW_FILE, _OLD_FILE
except ImportError:  # running from the adapter package
    from .scorer import Hunk, parse_diff as _parse_diff
    from .scorer.ground_truth import _HUNK_HEADER, _NEW_FILE, _OLD_FILE


def parse_diff_flbench(diff_text: str) -> list[Hunk]:
    """The un-widened FLBench rule, for parity columns only.

    Never use this to build ground truth -- it exists so a scorer can emit the
    published-FLBench value alongside the locked one, and so the delta between
    them measures the anchoring bias. Routed through here rather than importing
    parse_diff directly so scripts/check_anchoring.py can keep enforcing a single
    entry point.
    """
    return _parse_diff(diff_text)


def _widenable_anchors(diff_text: str) -> set[tuple[str, int]]:
    """(file, anchor) for addition-only hunks whose anchor+1 provably exists.

    An addition-only hunk anchors on the last context line before the insertion,
    L. Widening to {L, L+1} is only meaningful when L+1 is a real line of the
    vulnerable file, and the hunk itself proves that exactly when it carries
    context AFTER the insertion: the first such line sits at old line L+1. A hunk
    with no trailing context is an insertion at EOF, where L is already the last
    line -- widening there names a line that does not exist, puts it in the
    prompt, and caps recall and IoU below 1.0 for a perfect answer.

    This mirrors the hunk walk in scorer/ground_truth.py:parse_diff rather than
    reusing it because Hunk carries no trailing-context flag, and the vendored
    scorer is a verbatim copy that must not gain one.
    """
    ok: set[tuple[str, int]] = set()
    current_file = ""
    old_line = 0
    deleted = False
    anchor: int | None = None
    seen_plus = False
    trailing_context = False
    in_hunk = False

    def flush() -> None:
        if in_hunk and not deleted and anchor is not None and trailing_context:
            ok.add((current_file, anchor))

    for raw in diff_text.splitlines():
        m = _OLD_FILE.match(raw)
        if m and not raw.startswith("--- /dev/null"):
            flush()
            in_hunk = False
            current_file = m.group(1)
            continue
        if _NEW_FILE.match(raw):
            continue
        m = _HUNK_HEADER.match(raw)
        if m:
            flush()
            old_line = int(m.group(1))
            deleted = False
            anchor = None
            seen_plus = False
            trailing_context = False
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw.startswith("-"):
            deleted = True
            old_line += 1
        elif raw.startswith("+"):
            seen_plus = True
        else:
            if seen_plus:
                trailing_context = True
            else:
                anchor = old_line
            old_line += 1
    flush()
    return ok


def parse_diff_anchored(diff_text: str) -> list[Hunk]:
    """parse_diff, with addition-only hunks anchored to the gap they occupy.

    Deletion and modification hunks are returned unchanged: they already carry
    concrete pre-fix lines and have no anchoring ambiguity. Addition-only hunks
    at EOF are also returned unchanged -- see `_widenable_anchors`.
    """
    widenable = _widenable_anchors(diff_text)
    return [
        Hunk(
            file=h.file,
            lines=frozenset(h.lines | {min(h.lines) + 1}),
            is_primary=h.is_primary,
            addition_only=h.addition_only,
        )
        if h.addition_only and h.lines and (h.file, min(h.lines)) in widenable
        else h
        for h in _parse_diff(diff_text)
    ]
