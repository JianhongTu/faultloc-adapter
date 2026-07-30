## Task

You are a security engineer fixing a memory-safety vulnerability in a C/C++ codebase. A fuzzer input reproduces the bug. Your job is to **diagnose the root cause and repair it** in the source at `/workspace/src`.

You get **one attempt**. There is no build tool and no way to test your fix — the project is compiled and the reproducer re-run after you exit, and whatever is in your working tree at that moment is what gets evaluated. Reason your way to the fix rather than trying things.

Work systematically:

1. Read the sanitizer report and locate the crash site in the source.
2. Trace **backwards** through the call chain to where the incorrect value or missing check originates. The crash site is where the error is *detected*, not necessarily where it *originates*.
3. Edit the source to fix the root cause.
4. Re-read your change. It must compile as written — a syntax error or a wrong type costs you the attempt, and you will not get to see the compiler's complaint.

Do not exit until the task has been completed.

---

{{REPORT_SECTION}}

## Source Code

The vulnerable source is a git working tree at `/workspace/src`. Edit it in place. You do not need to commit; your final working tree is what gets evaluated.

## Proof of Concept

The fuzzer input that triggers the crash is at `/workspace/poc`. Inspect it with `xxd` or `hexdump`: its structure is evidence about which code path reaches the defect.

## Sanitizer Report

The following report was produced by running the PoC against the vulnerable binary. `run_poc.sh` re-runs it, but note that it executes the **pre-existing build** — it will keep reporting the same crash no matter what you edit, so it is a source of evidence, not a way to check your fix.

**Vulnerability metadata:**
- Crash type: {{CRASH_TYPE}}
- Sanitizer: {{SANITIZER}}
- Fuzz target: {{FUZZ_TARGET}}

```
{{SANITIZER_REPORT}}
```

---

## Available Tools

### Dedicated tools
- `run_poc.sh` — run the reproducer against the pre-existing build and print a fresh sanitizer report. Does not reflect your edits.
- `/workspace/poc` — the raw reproducer input, readable.

### Bash utilities
- `rg` — fast regex search across source files
- `ctags` — build a symbol index (`ctags -R .`), then search tags with `grep` or `rg`
- `cscope` — cross-reference: find all callers of a function, definitions, and symbol usages
- `xxd`, `hexdump` — hex dump binary files
- `objdump`, `readelf`, `nm` — inspect ELF structure, symbols, and disassembly
- `jq` — parse and query JSON

---

## What Counts as Success

Your patch must:

- **build** — the project compiles with your change;
- **suppress the crash** — the reproducer no longer trips the sanitizer;
- **fix the root cause** — repair the faulty logic itself.

Disabling the fuzz target, short-circuiting the entry point, suppressing the sanitizer, or otherwise making the reproducer pass without correcting the underlying defect does **not** count as a fix.

Write a one-sentence summary of the root cause you identified and the change you made to `/logs/artifacts/summary.txt` before you exit.
