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

import os
import pathlib
import sys

import yaml

# Keyed by Harbor agent name: the complete kwargs for that agent.
#
# EVERY key here must appear in that agent's CLI_FLAGS, because Harbor keeps only
# the kwargs an agent declares and drops the rest without a word. The two groups:
#
#   * the web-tool denial -- off by default, see the module docstring;
#   * reasoning effort, spelled `reasoning_effort` for both agents that have it
#     but rendered differently (claude-code `--effort`, codex
#     `-c model_reasoning_effort=`). opencode declares NO effort flag -- only
#     `variant` -- so passing one there would be silently discarded and read as
#     an effort setting that was never applied. Its arm therefore has none.
AGENT_KWARGS = {
    "codex": {"reasoning_effort": "high", "web_search": "disabled"},
    "claude-code": {"reasoning_effort": "high", "disallowed_tools": "WebSearch,WebFetch"},
    "opencode": {},
}

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_BASE = _ROOT / "configs" / "diagnosis-eval.yaml"


def _zhipu_provider(model_id: str) -> dict:
    """opencode config registering Zhipu as an OpenAI-compatible provider.

    The provider MUST be named `custom` and carry `npm: @ai-sdk/openai-compatible`.
    opencode's built-in `openai` provider speaks the Responses API, which Zhipu
    does not implement -- it answers /chat/completions and 404s /responses. The
    `npm` field selects the adapter, it does not trigger an install: opencode
    bundles it (verified offline, with the package absent from the filesystem).

    Credentials go in the config rather than the environment because Harbor only
    forwards provider env vars for provider names it knows, and `custom` is not
    one. They are read from the environment HERE so the key never lands in a
    tracked file -- keep the generated config out of the repo.
    """
    try:
        base_url, api_key = os.environ["ZHIPU_BASE_URL"], os.environ["ZHIPU_API_KEY"]
    except KeyError as exc:
        raise SystemExit(f"opencode arm needs {exc.args[0]} in the environment (see .env)")
    # `models` is registered explicitly rather than left to Harbor's
    # auto-generated entry: this is the exact block that was smoke-tested against
    # Zhipu end to end, and it is also the shape FLBench's own entrypoint writes.
    return {
        "provider": {
            "custom": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"apiKey": api_key, "baseURL": base_url},
                "models": {model_id: {"name": model_id}},
            }
        }
    }


def build(agent: str, model: str) -> dict:
    if agent not in AGENT_KWARGS:
        raise SystemExit(
            f"unknown agent {agent!r}. Add it to AGENT_KWARGS with the kwargs that turn "
            f"off its hosted web tools and set its reasoning effort, checking each "
            f"against that agent's CLI_FLAGS. Known: {', '.join(sorted(AGENT_KWARGS))}"
        )
    kwargs = dict(AGENT_KWARGS[agent])
    if agent == "opencode" and model.startswith("custom/"):
        kwargs["opencode_config"] = _zhipu_provider(model.split("/", 1)[1])
    config = yaml.safe_load(_BASE.read_text())
    config["agents"] = [{"name": agent, "model_name": model, "kwargs": kwargs}]
    return config


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(
            "usage: diagnosis_config.py <agent> <model>\n"
            f"       agents: {', '.join(sorted(AGENT_KWARGS))}"
        )
    config = build(sys.argv[1], sys.argv[2])
    yaml.safe_dump(config, sys.stdout, sort_keys=False)


if __name__ == "__main__":
    main()
