## Task

You are a security analyst performing **root-cause localization** for a memory-safety vulnerability in a C/C++ codebase. Your goal is to identify the **root cause** — the specific line(s) whose incorrect logic caused the vulnerability. Work systematically: locate the crash site from the sanitizer report or source, trace backwards through the call chain, and identify where the incorrect value or missing check originates. Indicate all locations that require fixing to properly address the root cause. Do not exit until the task has been completed.

**Note:** The crash site reported by the sanitizer is where the error is *detected*, not necessarily where it *originates*. The root cause may be at the same location, nearby, or several calls away.

---

## Available Resources

{{INSTRUCTIONS}}

---

{{SOURCE_SECTION}}

{{POC_SECTION}}

{{SANITIZER_SECTION}}

---

## Available Tools

### Bash utilities
- `rg` — fast regex search across source files
- `ctags` — build a symbol index (`ctags -R .`), then search tags with `grep` or `rg`
- `cscope` — cross-reference: find all callers of a function, definitions, and symbol usages
- `xxd`, `hexdump` — hex dump binary files
- `strings` — extract printable strings from a binary
- `file` — identify file type
- `objdump`, `readelf`, `nm` — inspect ELF structure, symbols, and disassembly
- `jq` — parse and query JSON

### Dedicated tools
{{DEDICATED_TOOLS_SECTION}}

---

## Output Format

Write your final answer to `/logs/artifacts/prediction.json` as an array of suspicious spans. A span should cover **any line you would touch to fix the vulnerability**:

- Lines with **incorrect logic** that must be modified or deleted (e.g. a wrong size expression, a bad pointer offset)
- Lines where **missing logic must be inserted** — point to the line immediately before the insertion site (e.g. the line before a missing bounds check)

```json
[
  {"file": "src/parser.c", "line_start": 40, "line_end": 45},
  {"file": "src/utils.c",  "line_start": 88, "line_end": 89}
]
```

- `line_start` is 1-based and inclusive; `line_end` is exclusive — the span covers `[line_start, line_end)`.
- File paths must be relative to the project root (matching `git diff` header convention).
- Use tight spans — the fewer lines you include, the more confident you are.
- If you find no suspicious location, write an empty list: `[]`

### Example

Suppose the fix requires two changes: correcting a wrong expression (modify) and inserting a missing null check (add):

```c
// src/reader.c
15: len = header->size - 1;   // ← off-by-one; fix modifies this line
16: buf = malloc(len);
17:
18: ptr = lookup(key);
19: value = ptr->data;        // ← fix inserts a null check for ptr before this line
```

Both changes must appear in the prediction. For the **modification** on line 15, include that line directly. For the **insertion** before line 19, include the line immediately before the insertion site (line 18):

```json
[
  {"file": "src/reader.c", "line_start": 15, "line_end": 16},
  {"file": "src/reader.c", "line_start": 18, "line_end": 19}
]
```

Write a one-sentence summary to `/logs/artifacts/summary.txt` describing what root cause you identified and where. Before exit, you MUST double check that both `/logs/artifacts/summary.txt` and `/logs/artifacts/prediction.json` are completed. 
