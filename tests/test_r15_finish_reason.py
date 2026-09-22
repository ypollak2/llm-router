"""R15 — a censored or truncated answer was recorded as a win.

`LLMResponse` had no `finish_reason` field. `gateway._finish_reason` read it
with `getattr(result, "finish_reason", None)` and fell through to the literal
`"stop"` on every response ever returned — while its own docstring claimed the
value was "derived rather than asserted, so a truncated answer is not reported
as a complete one". There was nothing to derive from.

Why it matters beyond the wire format: a `content_filter` stop returns the
PARTIAL text generated before the filter fired. That text is typically long and
fluent, so it passes `grounding.response_is_usable` — the predicate the routing
bandit uses as its success signal. Every censored generation therefore
reinforced the model that produced it, and the reward `success_rate/avg_cost`
preferred whichever model gets censored most cheaply.

The asymmetry below is the part worth keeping: a REPORTED non-stop reason is a
failure, an ABSENT one is unknown. Ollama and the CLI-backed providers report no
stop reason at all, and treating their silence as failure would hand every local
model a permanent penalty on no evidence — the mirror image of the bug.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from llm_router import failopen
from llm_router.router import _FAILED_FINISH_REASONS, _finish_reason_is_failure
from llm_router.types import LLMResponse


@dataclass
class _Resp:
    finish_reason: str = ""
    provider: str = "openai"
    model: str = "gpt-4o-mini"


def test_llmresponse_carries_a_finish_reason_at_all():
    """The field whose absence made every other check vacuous."""
    r = LLMResponse(
        content="x", model="m", input_tokens=1, output_tokens=1,
        cost_usd=0.0, latency_ms=1.0, provider="p",
    )
    assert hasattr(r, "finish_reason")
    assert r.finish_reason == "", (
        "the default must be EMPTY, not 'stop'. A backend that reports nothing "
        "has not told us the generation finished cleanly, and defaulting to "
        "'stop' asserts exactly that on no evidence."
    )


@pytest.mark.parametrize("reason", sorted(_FAILED_FINISH_REASONS))
def test_a_reported_failure_reason_is_a_failure(reason, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    failopen.clear()
    assert _finish_reason_is_failure(_Resp(finish_reason=reason)) is True

    failopen.reset_cache()
    code = f"CHZ-FINISH-{reason.upper().replace('_', '-')}"
    assert failopen.snapshot().by_code.get(code) == 1, (
        "the reason was excluded from the success signal but recorded nowhere, "
        "so an operator cannot tell a model that is being censored from one "
        "that is simply losing on cost"
    )


@pytest.mark.parametrize("reason", ["", "stop", "tool_calls"])
def test_anything_else_is_not_treated_as_failure(reason):
    """Empty especially. Ollama reports no reason and must not be penalised."""
    assert _finish_reason_is_failure(_Resp(finish_reason=reason)) is False


def test_the_censored_text_would_otherwise_have_passed_as_a_success():
    """The check on the premise: prove the old signal accepted this.

    Without this, the fix is asserted rather than demonstrated — and the whole
    argument rests on the claim that filtered output looks usable.
    """
    from llm_router.grounding import response_is_usable

    partial = (
        "Sure. To configure the service you first open the settings file and "
        "locate the network section, where the relevant option is"
    )
    assert response_is_usable(partial), (
        "premise broken: this fixture no longer passes the usability check, so "
        "it no longer demonstrates that a content_filter stop was recorded as "
        "a win. Pick text that does."
    )
    assert _finish_reason_is_failure(_Resp(finish_reason="content_filter"))


def test_the_provider_layer_normalises_and_reports_the_reason():
    from llm_router.providers import _normalise_finish_reason

    class _Choice:
        def __init__(self, fr):
            self.finish_reason = fr

    class _R:
        def __init__(self, fr):
            self.choices = [_Choice(fr)]

    assert _normalise_finish_reason(_R("content_filter")) == "content_filter"
    assert _normalise_finish_reason(_R("max_tokens")) == "length"
    assert _normalise_finish_reason(_R("end_turn")) == "stop"
    assert _normalise_finish_reason(_R(None)) == "", "absent must stay absent"
    assert _normalise_finish_reason(object()) == "", "a malformed response must not raise"


def test_the_success_expression_consults_the_reason():
    """AST on the call site, not a substring of the file.

    A-10: the same assertion written as `"_finish_reason_is_failure" in
    getsource(...)` passes with the name in a comment and the branch deleted.
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src/llm_router/router.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    # Find every `success=<expr>` keyword and check at least one consults it.
    consulting = [
        kw for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "success"
        and "_finish_reason_is_failure" in ast.unparse(kw.value)
    ]
    assert consulting, (
        "no `success=` argument in router.py consults _finish_reason_is_failure. "
        "The predicate exists and nothing asks it, which is how a censored "
        "generation goes back to counting as a win."
    )
