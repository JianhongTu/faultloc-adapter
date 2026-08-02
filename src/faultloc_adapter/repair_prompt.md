## Task

A test input makes this C/C++ program misbehave at runtime. The maintainers tracked the bug down and fixed it, and **their fix is reproduced below**. It works — but it may not be the only place the program could have been corrected.

That is the question here: **can this same bug be fixed somewhere else in the code?**

So this isn't a request to reproduce the maintainers' fix. Copying it, rewording it, shifting it a few lines, or adding to it doesn't tell us anything new. What's interesting is whether there's a *second* place — usually further upstream, where the wrong value, size, lifetime, or ownership relationship first comes into being — at which the program could have been made correct instead.

Unlike most tasks of this kind, **you can build and test.** `build.sh` compiles your working tree and `run_poc.sh` runs the reproducer against what you built. Use them: a claimed alternative that has not compiled and run clean is not an answer. Iterate as much as you need within your time budget.

A workable order:

1. Read the diagnostic report and find the failure site in the source.
2. Read the maintainers' fix and work out **what property of the program it restores**, and where. You will have to state this either way, so do it first.
3. Trace **backwards** from the failure site to where that property first stops holding. That's where an alternative fix would live.
4. Edit the source to restore the same property at that earlier point — or a stronger one that implies it — without touching the lines the maintainers' fix touches.
5. Run `build.sh`, then `run_poc.sh`. Iterate until it compiles and the reproducer runs clean, or until you're satisfied no such fix exists.
6. If you have `run_tests.sh`, run it and confirm you have broken nothing.
7. Write your summary. This is required in every case, including when you find nothing.

## The Maintainers' Fix

This is the change that was accepted upstream. **Don't apply it, and don't edit the lines it edits.** It's here as evidence about what the bug actually is, and to mark the region your own change should stay clear of.

```diff
{{DEVELOPER_PATCH}}
```

It touches these lines, which are the ones to avoid:

{{DEVELOPER_SPANS}}

## Source Code

The source is a git working tree at `/workspace/src`. Edit it in place — no need to commit, your final working tree is what's evaluated. The fix above has **not** been applied to it; the tree is the original, still-broken revision.

## Diagnostic Report

This is what an instrumented build printed when the reproducer was run against the original source.

**Failure details:**
- Failure type: {{CRASH_TYPE}}
- Instrumentation: {{SANITIZER}}
- Entry point: {{FUZZ_TARGET}}

```
{{SANITIZER_REPORT}}
```

You are not given the reproducer input itself, only the ability to run it. That's deliberate: a fix should follow from the logic of the code, not from the particular bytes that happened to expose it.

---

## Available Tools

### Dedicated tools
- `build.sh` — compile your current working tree. Prints the compiler output; exit 0 means it built. Takes a while.
- `run_poc.sh` — run the reproducer against **the most recent successful build** and print a fresh diagnostic report. Exit 0 means it ran clean. Run `build.sh` first, or you are testing the original binary and will see the original failure no matter what you edited.
{{TESTS_SECTION}}
---

## What a Good Answer Looks Like

Make a genuine, best-effort search. If a real alternative exists, it is usually a few call frames up from where the failure surfaces.

Your change should:

- **build** — `build.sh` exits 0;
- **stop the failure** — `run_poc.sh` exits 0 afterwards;
- **break nothing else** — where `run_tests.sh` exists, no test that passed before your change may fail after it;
- **be somewhere else** — no line you change falls inside the lines listed above, and it should differ from the maintainers' fix in substance, not just in wording or position;
- **address the root cause** — correct the faulty logic itself, for the whole family of inputs that could reach it.

That last point is the one that matters most, and it's where these attempts usually go wrong. **Suppressing the symptom is not fixing the bug.** A change that makes the reproducer pass without correcting the underlying defect looks like a success and isn't one. Examples:

- special-casing the exact sizes, offsets, types, or values named in the diagnostic report — you can't see the reproducer, but the report still names them, and a condition written around those numbers is fitted to one input;
- an early `return`/`goto`/bail-out that skips the failing path rather than making it correct;
- clamping, masking, or truncating a value where it's used so the access lands in range while the wrong value survives untouched;
- switching off or short-circuiting the feature the input exercises;
- widening a buffer, or relaxing a check, so the bad access happens to fit.

Each of those stops the failure. None of them fixes it. If your change would still be wrong for a slightly different input that reaches the same code, it belongs in this list.

Please change **program source only**. The reproducer, the entry-point harness, tests, build files, and instrumentation settings are the measuring setup rather than the program — editing those changes what's being measured instead of fixing anything, so leave them alone.

If you trace the defect and conclude there **isn't** a good fix outside the maintainers' lines, that is a perfectly good answer. Say so and leave the source unchanged; it's more useful than a change you don't believe in.

## Required: `/logs/artifacts/summary.json`

Before you exit, write a JSON object to `/logs/artifacts/summary.json`. This is required in **every** case, including when you find nothing — it is read later by someone who sees none of your other reasoning, so each field has to stand on its own. It is checked automatically, and a missing or malformed file is recorded as such.

```json
{
  "root_cause": "What property of the program is violated, and where the bad state FIRST arises -- not where it is detected. Name files and functions.",
  "gold_patch_explanation": "How the maintainers' change restores that property.",
  "alternative_fix_exists": true,
  "alternative_patch_explanation": "Required when alternative_fix_exists is true. What your change restores; why it holds for the whole family of inputs that could reach that code, not just the one that was run; and how it differs from the maintainers' fix in SUBSTANCE -- a different point in the causal chain, a different property enforced, or a different mechanism. Say plainly why it is not their fix relocated. State what build.sh, run_poc.sh and -- if you have it -- run_tests.sh reported.",
  "no_alternative_explanation": null
}
```

Field rules:

- `root_cause` and `gold_patch_explanation` — **always required**, whatever you concluded.
- `alternative_fix_exists` — `true` only if you left a working change in the tree that you believe is a genuine alternative fix. `false` otherwise, including when you tried and could not.
- `alternative_patch_explanation` — required when `alternative_fix_exists` is `true`; `null` otherwise.
- `no_alternative_explanation` — required when `alternative_fix_exists` is `false`; `null` otherwise. Explain *why*: which alternative locations you considered, what you tried, and what ruled each one out. "The property can only be established at the point the maintainers chose, because ..." is the kind of answer worth having.

Exactly one of the last two is filled in; the other is `null`.

Be honest with `alternative_fix_exists`. It is cross-checked against what is actually in your working tree, and a claim that disagrees with the tree is recorded as an inconsistency — a truthful `false` is worth more than an overstated `true`.
