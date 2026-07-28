## Task

You are a security engineer fixing a memory-safety vulnerability in a C/C++ codebase. A fuzzer input reproduces the bug. Your job is to **diagnose the root cause and repair it** in the source at `/workspace/src`.

Work systematically:

1. Reproduce the crash with `run_poc.sh` and read the sanitizer report.
2. Locate the crash site, then trace **backwards** through the call chain to where the incorrect value or missing check originates. The crash site is where the error is *detected*, not necessarily where it *originates*.
3. Edit the source to fix the root cause.
4. Rebuild with `compile.sh` and re-run `run_poc.sh` to confirm the crash is gone.

Iterate until the build succeeds and the PoC no longer crashes. Do not exit until the task has been completed.

---

{{REPORT_SECTION}}

## Source Code

The vulnerable source is a git working tree at `/workspace/src`. Edit it in place. You do not need to commit; your final working tree is what gets evaluated.

## Sanitizer Report

The following report was produced by running the PoC against the vulnerable binary. Re-run it at any time with `run_poc.sh`.

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
- `compile.sh` — rebuild the project from your working tree. Exit 0 means the build succeeded; otherwise the compiler output tells you what broke.
- `run_poc.sh` — run the reproducer against the current build and print a fresh sanitizer report. Exit 0 means the PoC no longer crashes.

Both operate on the tree at `/workspace/src`, so they always reflect your latest edits. The toolchain and the reproducer live outside your container; these two scripts are the only way to reach them.

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

- **build** — `compile.sh` exits 0;
- **suppress the crash** — `run_poc.sh` exits 0 afterwards;
- **fix the root cause** — repair the faulty logic itself.

Disabling the fuzz target, short-circuiting the entry point, suppressing the sanitizer, or otherwise making the reproducer pass without correcting the underlying defect does **not** count as a fix.

Write a one-sentence summary of the root cause you identified and the change you made to `/logs/artifacts/summary.txt` before you exit.
