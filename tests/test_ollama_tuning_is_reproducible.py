"""The Ollama tuning must live in the repo, not in one shell's memory.

N9. Every setting that makes local routing work — how long a model stays
resident, how many may be, how large the context is — was set by hand in whichever
shell started `ollama serve`. It is not persistent: Ollama.app discards the
launchctl environment, so the tuned setup requires a hand-run server, and the
tuning evaporates on the next restart with nothing recording what it was.

That is how `OLLAMA_CONTEXT_LENGTH=32768` survived unnoticed at ~70x the real
payload (measured worst case ~457 tokens), costing about 10GB of KV cache per
model on a 48GB machine.

KEEP_ALIVE is set from measurement, not preference. On 765 real gaps between this
user's prompts across 6 sessions (p50 6.4 min, p90 47 min):

    keep_alive   prompts finding a warm model   reload cost /100 prompts
       5 min                44%                        225s
      15 min                68%                        127s
      30 min                84%                         65s

There is no knee — it is a straight trade. 30 min buys 0.6s per prompt over 15 min
and holds 17GB a third longer, and at 30 min a background task on this machine was
killed for memory. 15 min takes two thirds of the benefit at meaningfully less
pressure.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "src/llm_router/hooks/start-ollama.sh"
SOURCE = SCRIPT.read_text()


@pytest.mark.parametrize("var", [
    "OLLAMA_KEEP_ALIVE", "OLLAMA_MAX_LOADED_MODELS", "OLLAMA_CONTEXT_LENGTH",
])
def test_the_script_sets_the_tuning(var):
    assert re.search(rf'export {var}=', SOURCE), (
        f"{var} is not set by the start script, so it lives only in whichever "
        "shell happened to launch the server and is lost on restart"
    )


def test_every_setting_is_overridable():
    for var in ("OLLAMA_KEEP_ALIVE", "OLLAMA_MAX_LOADED_MODELS", "OLLAMA_CONTEXT_LENGTH"):
        m = re.search(rf'export {var}="\$\{{([^}}]*)\}}"', SOURCE)
        assert m and ":-" in m.group(1), f"{var} is hardcoded; an operator cannot override it"


def test_the_context_length_is_not_back_to_32k():
    m = re.search(r'export OLLAMA_CONTEXT_LENGTH="\$\{[^:]*:-(\d+)\}"', SOURCE)
    assert m, "the context length default is not readable"
    assert int(m.group(1)) <= 16384, (
        f"context length back to {m.group(1)}. The real payload is ~457 tokens "
        "worst case; 32768 cost ~10GB of KV cache per model for nothing."
    )


def test_keep_alive_is_bounded():
    m = re.search(r'export OLLAMA_KEEP_ALIVE="\$\{[^:]*:-(\d+)([ms])\}"', SOURCE)
    assert m, "the keep-alive default is not readable"
    minutes = int(m.group(1)) * (1 if m.group(2) == "m" else 1 / 60)
    assert 5 <= minutes <= 30, (
        f"keep-alive {minutes:.0f}m is outside the measured range. Below 5 the "
        "reload tax dominates; above 30 a 17GB model sits idle long enough to "
        "have a background task killed for memory on this machine."
    )


def test_the_numbers_behind_the_choice_are_written_down():
    """A tuning constant without its measurement is a preference, and the next
    person cannot tell which."""
    assert "765 real gaps" in SOURCE or "44%" in SOURCE, (
        "the keep-alive trade is set with no record of what it was measured "
        "against"
    )
