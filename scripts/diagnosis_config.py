"""Emit a stage-1 job config with the agent -- and its web-tool denial -- pinned.

`configs/diagnosis-eval.yaml` deliberately carries no `agents:` block: the agent
and model vary per condition and belong on the command line. But the denial that
keeps a run honest is a property of the AGENT, not of the operator's memory, and
`harbor run -a <name>` discards the config's `agents:` block wholesale
(harbor/cli/jobs.py:1258) -- so a kwarg pinned in the canonical config is thrown
away by the very flag used to select the agent. Passing it as `--ak` instead is
worse: harbor keeps only the kwargs an agent declares in CLI_FLAGS and silently
drops the rest (installed/base.py:352-357, BaseAgent.__init__ absorbs the
remainder via **kwargs, and parse_kwargs validates nothing). Hand it
`--ak web_search=disabled` while running claude-code and the run starts clean,
finishes clean, and has had web search on the whole time.

This script closes that by deriving the denial from the agent name and writing
both into one config, so selecting the agent IS selecting its denial. The body
still comes from configs/diagnosis-eval.yaml; nothing is duplicated.

    python scripts/diagnosis_config.py codex gpt-5.6-sol > /tmp/job.yaml
    uv run harbor run -c /tmp/job.yaml -p datasets/... --job-name ...
                      # NOTE: no -a. Passing it would drop the agents block again.

Why each denial, and why not more: hosted web search is not a shell tool. The
model asks the vendor's backend to search and the results arrive inside the
ordinary API response, over the same host the task allowlist must permit for the
model to run at all -- so no network policy can see it, and STRIP_ARCHIVAL_GIT
does not apply. Everything else an agent might reach for is client-side and dies
at the allowlist already.
"""

import pathlib
import sys

import yaml

# Keyed by Harbor agent name. Each value is the kwargs that switch off every
# vendor-side web tool that agent exposes; a `{}` means the agent has none and
# its client-side fetching is already dead at the task allowlist.
DENY = {
    "codex": {"web_search": "disabled"},
    "claude-code": {"disallowed_tools": "WebSearch,WebFetch"},
    "opencode": {},
}

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_BASE = _ROOT / "configs" / "diagnosis-eval.yaml"


def build(agent: str, model: str, reasoning_effort: str = "high") -> dict:
    if agent not in DENY:
        raise SystemExit(
            f"unknown agent {agent!r}. Add it to DENY with the kwargs that turn "
            f"off its hosted web tools, or {{}} if it has none. "
            f"Known: {', '.join(sorted(DENY))}"
        )
    config = yaml.safe_load(_BASE.read_text())
    kwargs = {"reasoning_effort": reasoning_effort, **DENY[agent]}
    config["agents"] = [{"name": agent, "model_name": model, "kwargs": kwargs}]
    return config


def main() -> None:
    if len(sys.argv) not in (3, 4):
        raise SystemExit(
            "usage: diagnosis_config.py <agent> <model> [reasoning_effort]\n"
            f"       agents: {', '.join(sorted(DENY))}"
        )
    config = build(sys.argv[1], sys.argv[2], *sys.argv[3:4])
    yaml.safe_dump(config, sys.stdout, sort_keys=False)


if __name__ == "__main__":
    main()
