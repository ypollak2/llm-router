"""The semantic layer has to be reachable from a real request, not only a CLI.

An independent review of this branch found that `modes.apply` — the function
that attaches a pack — had no caller outside its own unit test. Everything was
built, tested and togglable, and none of it could ever run during an actual
routed call. "Ships dark" and "is not wired in" produce identical behaviour
today and completely different behaviour the moment somebody sets an arm: the
first turns on, the second stays silent and looks like the feature failing.

That is a specific, recurring failure in this codebase. It is the same shape as
the dead AST branch in `context_prep` — gated on an argument nobody passed, so
it reported the same value as a legitimate empty result — and the same shape as
OKF reaching 2 of 7 execution paths. Each time, nothing failed and nothing
logged.

So the wiring gets a test, and the test asserts the switch has an effect
end-to-end rather than that a call site exists.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from llm_router.context_injection import inject


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from llm_router.semantic import indexer as ix

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "reconciler.py").write_text(
        "def reconcile_invoice(a, b):\n    return a == b\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True,
                   capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True, timeout=30)
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(repo))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    ix.index(root=repo)
    return repo


def test_the_choke_point_reaches_the_semantic_layer(project, monkeypatch):
    """Turning it on has an effect on what a routed prompt carries."""
    prompt = "fix reconcile_invoice in reconciler.py"

    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_SOURCE", "off")
    off = inject(prompt, root=str(project))

    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_SOURCE", "on")
    on = inject(prompt, root=str(project))

    assert on != off, (
        "selecting `on` changed nothing, so the semantic layer is not reachable "
        "from the choke point and could never run during a real call"
    )
    assert "reconcile_invoice" in on


def test_the_default_is_unchanged_behaviour(project, monkeypatch):
    """Wiring it in must not turn it on."""
    for name in ("SOURCE", "HISTORY", "INTERVENTION"):
        monkeypatch.delenv(f"LLM_ROUTER_SEMANTIC_{name}", raising=False)
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_ARM", raising=False)
    prompt = "fix reconcile_invoice in reconciler.py"

    assert "<repository_evidence>" not in inject(prompt, root=str(project)), (
        "the semantic layer attached itself with no switch set; it has not been "
        "measured and defaulting it on skips the experiment it exists for"
    )


def test_shadow_is_byte_identical_through_the_choke_point(project, monkeypatch):
    """The guarantee has to survive the integration, not just the unit."""
    prompt = "fix reconcile_invoice in reconciler.py"

    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_SOURCE", "off")
    off = inject(prompt, root=str(project))

    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_SOURCE", "shadow")
    shadow = inject(prompt, root=str(project))

    assert shadow == off, (
        "shadow changed the prompt once wired through the choke point, so every "
        "comparison against off is confounded in the real path even though the "
        "unit test passes"
    )


def test_a_broken_semantic_layer_cannot_break_a_routed_call(project, monkeypatch):
    """Fail-open, like every other source this module composes."""
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_SOURCE", "on")

    def _explode(*a, **kw):
        raise RuntimeError("index is on fire")

    from llm_router.semantic import modes
    monkeypatch.setattr(modes, "apply", _explode)

    prompt = "fix reconcile_invoice in reconciler.py"
    assert inject(prompt, root=str(project)) is not None
    assert prompt in inject(prompt, root=str(project))


def test_an_invalid_arm_does_not_take_down_the_request(project, monkeypatch):
    """`modes.current()` raises on a bad arm — correct there, fatal here.

    The CLI should refuse a typo'd arm loudly. A routed call should not die of
    one, because a misconfigured environment variable is not a reason to stop
    answering.
    """
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_ARM", "NOPE")
    prompt = "fix reconcile_invoice in reconciler.py"
    assert prompt in inject(prompt, root=str(project))
