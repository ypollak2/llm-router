"""The startup script must never download a model of its own choosing.

Regression for 2026-09-14. `start-ollama.sh` defaulted OLLAMA_MODEL to the
hardcoded `qwen3.5:latest` and ran `ollama pull` whenever it was absent. The user
removed that model; the script silently re-downloaded 17GB, put it back at the
head of the routing chain, and contaminated an n=200 benchmark that was running
at the time. The model name was also stale — `model_discovery` had long since
become the single answer to "which models are installed".

A pull is operator intent: it happens when LLM_ROUTER_OLLAMA_MODEL names a model,
or when --pull is passed. Never from a default.
"""
from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "src/llm_router/hooks/start-ollama.sh"
SOURCE = SCRIPT.read_text()


def test_no_hardcoded_model_default():
    default = re.search(r'OLLAMA_MODEL="\$\{LLM_ROUTER_OLLAMA_MODEL:-([^}]*)\}"', SOURCE)
    assert default, "the OLLAMA_MODEL default line is not in the expected shape"
    assert default.group(1) == "", (
        f"OLLAMA_MODEL defaults to {default.group(1)!r}; an unnamed model must mean "
        "'whatever is installed', never a name this script will download"
    )


def test_every_pull_is_guarded_by_an_operator_named_model():
    for line_no, line in enumerate(SOURCE.splitlines(), 1):
        if re.search(r"^\s*(if !\s*)?ollama pull", line):
            before = "\n".join(SOURCE.splitlines()[:line_no])
            assert '-z "${OLLAMA_MODEL}"' in before, (
                f"line {line_no} pulls a model with no preceding check that the "
                f"operator named one: {line.strip()}"
            )


def test_an_empty_model_with_something_installed_is_success_not_a_download():
    tail = SOURCE.split('if [[ -z "${OLLAMA_MODEL}" ]]; then', 1)[-1]
    branch = tail.split("fi", 1)[0]
    assert "installed_models" in branch and "exit 0" in branch, (
        "with no model named and models present, the script must exit 0, not pull"
    )
    assert "ollama pull" not in branch, "the no-model-named branch pulls"
