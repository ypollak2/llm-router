"""Stage 0 — the two defects that actually suppressed routing, and the one that
made the evidence unreadable.

Context. Sustained routing fell from 31-39% to ~2% of real prompts after the 13.0.0
upstream sync (5 successes / 246 prompts, 2026-09-04..09-10). Two candidate causes
were identified from `~/.llm-router/auto-route-debug.log`:

  * 86 `DIRECT FAILED`, each exactly 4s after its `DIRECT:` line, and
  * 132 `DIRECT SKIP: context-dependent prompt`.

Only the first turned out to be a defect. The context-dependent gate was measured
against 376 real user prompts pulled from this machine's own transcripts (S64
workbench, arena-demo, llm-router; last ~10 days) and it lets **52.1%** through.
Loosening it — narrowing the noun list, and relaxing the deictic length gate from
12 words to 6 — freed 36 more prompts, but inspection showed almost every one of
them genuinely does need local context:

    "commit this and show me the demo again"
    "Seems like I see the old demo, can you please check it"
    "show me the score when it's done"
    "Use Agenticgraphs and llm-router to work on this"

Routing those would produce exactly the fabricated drafts the gate exists to
prevent. So the gate is left alone; `test_gate_still_holds_the_line` pins that
decision so a future attempt to "improve the routing rate" by widening the gate
has to argue with the measurement rather than rediscover it.

The path to a higher rate is Stage 1 — give the routed model the context these
prompts refer to, so they become routable *with* their material — not pretending
they are self-contained.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:  # the hook calls sys.exit() when run without stdin
        pass
    return mod


@pytest.fixture(scope="module")
def hook():
    return _load(HOOK, "stage0_auto_route")


# ── defect 1: the DIRECT timeout no local model could meet ──────────────────

def test_ollama_timeout_default_clears_real_local_latency(hook):
    """4s was below every measured p50, so DIRECT could only ever fail.

    Slowest routinely-used local model here is qwen3.8 at ~28.5s p50; a cold load
    behind Ollama.app's single slot is slower still.
    """
    assert hook.OLLAMA_TIMEOUT >= 30, (
        f"OLLAMA_TIMEOUT={hook.OLLAMA_TIMEOUT}s cannot complete a local call; "
        "measured p50s are 11.4s / 15.8s / 28.5s"
    )


def test_ollama_timeout_still_overridable(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_TIMEOUT", "7")
    mod = _load(HOOK, "stage0_auto_route_env")
    assert mod.OLLAMA_TIMEOUT == 7


# ── the decision not to widen the gate ──────────────────────────────────────

# Verbatim from this machine's transcripts. Every one was freed by loosening the
# gate, and every one refers to state only the local session can see.
REAL_PROMPTS_NEEDING_CONTEXT = [
    "maybe should commit the changes and push this into a private repo",
    "Seems like I see the old demo, can you please check it",
    "commit this and show me the demo again",
    "show me the score when it's done",
    "Use Agenticgraphs and llm-router to work on this",
    "wire the other six prompts into the workflow",
    "now do the same for the other 5 agents and create an HTML",
]


@pytest.mark.parametrize("prompt", REAL_PROMPTS_NEEDING_CONTEXT)
def test_gate_still_holds_the_line(hook, prompt):
    """Do not widen the gate to raise the routing rate — this is the cost.

    Measured on 376 real prompts: narrowing the noun list (+10) and relaxing the
    deictic gate to 6 words (+26) raise eligibility from 52.1% to 61.7%, and the
    prompts gained are these. A routed answer to "commit this and show me the demo
    again" is fabrication, not a saving.
    """
    assert hook._is_context_dependent(prompt), (
        f"gate no longer catches a prompt that needs local context: {prompt!r}"
    )


def test_gate_lets_genuinely_self_contained_prompts_through(hook):
    """The counterpart: the gate is not a blanket veto, or nothing would route."""
    routable = [
        "what is the difference between a mutex and a semaphore",
        "explain CRDTs to me",
        "summarise the tradeoffs of event sourcing",
    ]
    for p in routable:
        assert not hook._is_context_dependent(p), f"gate over-caught: {p!r}"


def test_measured_eligibility_is_documented_in_the_module_docstring():
    """The 52.1% figure is the reason the gate was left alone; keep it findable."""
    doc = sys.modules[__name__].__doc__ or ""
    assert "52.1%" in doc and "376 real" in doc


# ── defect 2: the debug log resolved $HOME at import ────────────────────────

def test_debug_log_path_is_resolved_per_call(hook, monkeypatch, tmp_path):
    """As a constant this wrote test output into the developer's live log.

    227 invocations carrying `chain=['ollama/fake-model']` and an empty session_id
    landed in `~/.llm-router/auto-route-debug.log` interleaved with real routing
    decisions, and had to be filtered out by hand before the routing rate could be
    measured at all.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert hook._debug_log_path() == tmp_path / ".llm-router" / "auto-route-debug.log"


def test_debug_log_writes_under_the_patched_home(hook, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".llm-router").mkdir(parents=True)
    hook._debug_log("STAGE0 PROBE")
    written = (tmp_path / ".llm-router" / "auto-route-debug.log").read_text()
    assert "STAGE0 PROBE" in written


def test_no_module_level_debug_log_constant_remains():
    """A reintroduced constant would silently restore the leak."""
    src = HOOK.read_text(encoding="utf-8")
    assert not re.search(r"^_DEBUG_LOG\s*=", src, re.MULTILINE), (
        "_DEBUG_LOG is back as a module-level constant; it bakes in $HOME at import"
    )


# ── the installed copy must not drift from the source ───────────────────────

def test_installed_hook_matches_repo_source():
    """The hook is COPIED to ~/.claude/hooks, not symlinked, so an edit to the repo
    source has no effect on live routing until it is reinstalled. Skipped when no
    copy is installed (CI)."""
    installed = Path.home() / ".claude" / "hooks" / "llm_router-auto-route.py"
    if not installed.exists():
        pytest.skip("no installed hook copy on this machine")
    result = subprocess.run(
        ["diff", "-q", str(installed), str(HOOK)], capture_output=True, text=True
    )
    assert result.returncode == 0, (
        "installed hook has drifted from src/llm_router/hooks/auto-route.py — "
        "live routing is running different code than the tests exercise"
    )
