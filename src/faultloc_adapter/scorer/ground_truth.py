"""Extract ground-truth hunks from a unified diff (eval-patches format).

Ground-truth rule (Section 5.2 of eval_protocol.md):
  GT_lines = line numbers in the VULNERABLE (pre-patch) version that the patch
             deletes or modifies.

  For addition-only hunks (no deleted lines): GT = the last context line
  immediately before the insertion block, mapped to the original file.

All hunks default to is_primary=True because eval-patches are pre-cleaned
(noise hunks stripped by the clean agent). Pass primary_hunk_indices to
override specific hunks if manual annotation is available.
"""

import re
from typing import Optional

from .types import Hunk

# Matches: @@ -old_start[,old_count] +new_start[,new_count] @@
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")
# Matches: --- a/path  or  --- path
_OLD_FILE = re.compile(r"^--- (?:a/)?(.+)")
# Matches: +++ b/path  or  +++ path
_NEW_FILE = re.compile(r"^\+\+\+ (?:b/)?(.+)")


def parse_diff(
    diff_text: str,
    primary_hunk_indices: Optional[set[int]] = None,
) -> list[Hunk]:
    """Parse a unified diff string into a list of Hunks.

    Args:
        diff_text: Full text of a unified diff (git format or plain).
        primary_hunk_indices: 0-based indices of hunks that are primary root-cause
            locations. Defaults to all hunks being primary.

    Returns:
        List of Hunk objects, one per @@ section, in order of appearance.
        Hunks with no GT lines (e.g. new-file headers) are skipped.
    """
    hunks: list[Hunk] = []
    current_file: str = ""
    old_line: int = 0
    hunk_deleted: list[int] = []
    last_context_line: Optional[int] = None
    in_hunk = False

    def _flush_hunk() -> None:
        """Finalise the hunk being parsed and append to hunks."""
        if not in_hunk:
            return
        hunk_index = len(hunks)
        if primary_hunk_indices is not None:
            is_primary = hunk_index in primary_hunk_indices
        else:
            is_primary = True

        if hunk_deleted:
            hunks.append(Hunk(
                file=current_file,
                lines=frozenset(hunk_deleted),
                is_primary=is_primary,
                addition_only=False,
            ))
        elif last_context_line is not None:
            # Addition-only hunk: GT is the last context line before insertion.
            hunks.append(Hunk(
                file=current_file,
                lines=frozenset([last_context_line]),
                is_primary=is_primary,
                addition_only=True,
            ))
        # else: hunk had no content at all — skip.

    first_plus_seen = False

    for raw_line in diff_text.splitlines():
        # --- file header (git diff: "--- a/path") ---
        m = _OLD_FILE.match(raw_line)
        if m and not raw_line.startswith("--- /dev/null"):
            _flush_hunk()
            in_hunk = False
            first_plus_seen = False
            current_file = m.group(1)
            continue

        # +++ file header — skip (we use --- for the filename)
        if _NEW_FILE.match(raw_line):
            continue

        # @@ hunk header ---
        m = _HUNK_HEADER.match(raw_line)
        if m:
            _flush_hunk()
            old_line = int(m.group(1))
            hunk_deleted = []
            last_context_line = None
            first_plus_seen = False
            in_hunk = True
            continue

        if not in_hunk:
            continue

        if raw_line.startswith("-"):
            hunk_deleted.append(old_line)
            old_line += 1
        elif raw_line.startswith("+"):
            # Freeze last_context_line at the first + seen in this hunk.
            first_plus_seen = True
        else:
            # Context line (space-prefixed or bare); only track before first +.
            if not first_plus_seen:
                last_context_line = old_line
            old_line += 1

    _flush_hunk()
    return hunks
