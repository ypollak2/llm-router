"""P1.4 (R-CTX-8) offline tests: stable prefix ordering, breakpoint placement,
opt-in default, and that the live `inject_cache_control` path is unchanged."""
from __future__ import annotations

import subprocess
import sys

from llm_router import cache_layout as cl
from llm_router.prompt_cache import inject_cache_control

RECENT = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
          {"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"}]


def _lay(request="REQ", **kw):
    return cl.build_layout(request, instructions="INSTR", knowledge="KNOW", recent=RECENT, **kw)


def test_order_is_instructions_knowledge_recent_request():
    lay = _lay()
    assert [b["text"] for b in lay.system] == ["INSTR", "KNOW"]
    assert [m["content"][0]["text"] for m in lay.messages] == ["q1", "a1", "q2", "a2", "REQ"]
    assert lay.messages[-1]["role"] == "user"


def test_breakpoints_exactly_two_and_never_on_request():
    lay = _lay()
    assert lay.breakpoints() == [("system", 1), ("messages", 3)]
    assert "cache_control" not in lay.messages[-1]["content"][-1]


def test_breakpoint1_on_instructions_when_no_knowledge():
    lay = cl.build_layout("R", instructions="I", recent=RECENT)
    assert lay.breakpoints() == [("system", 0), ("messages", 3)]


def test_no_recent_only_system_breakpoint():
    lay = cl.build_layout("R", instructions="I", knowledge="K")
    assert lay.breakpoints() == [("system", 1)]


def test_empty_everything_has_no_breakpoint():
    assert cl.build_layout("R").breakpoints() == []


def test_mark_false_same_order_no_breakpoints():
    marked, plain = _lay(), _lay(mark=False)
    assert plain.breakpoints() == []
    assert marked.stable_prefix == plain.stable_prefix


def test_prefix_stable_across_different_requests():
    a, b = _lay("first request"), _lay("a totally different request")
    assert a.prefix_hash == b.prefix_hash
    assert a.system == b.system and a.messages[:-1] == b.messages[:-1]


def test_prefix_changes_when_knowledge_changes():
    a = cl.build_layout("R", instructions="I", knowledge="K1", recent=RECENT)
    b = cl.build_layout("R", instructions="I", knowledge="K2", recent=RECENT)
    assert a.prefix_hash != b.prefix_hash


def test_request_is_never_truncated_or_altered():
    big = "x" * 50_000
    assert _lay(big).messages[-1]["content"][0]["text"] == big


def test_openai_shape_has_system_first():
    msgs = _lay().as_openai_messages()
    assert msgs[0]["role"] == "system" and msgs[0]["content"][-1]["cache_control"]
    assert msgs[-1]["content"][0]["text"] == "REQ"


def test_enabled_default_off(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_CACHE_LAYOUT", raising=False)
    assert cl.enabled() is False
    monkeypatch.setenv("LLM_ROUTER_CACHE_LAYOUT", "on")
    assert cl.enabled() is True


def test_live_paths_do_not_import_cache_layout():
    """LIVE-PATH-FREE proof: importing the live entry points must not pull in cache_layout."""
    code = ("import sys, llm_router.prompt_cache, llm_router.context_injection, llm_router.proxy.server;"
            "sys.exit(1 if 'llm_router.cache_layout' in sys.modules else 0)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]


# ── prompt_cache.inject_cache_control: recent_end ────────────────────────────
def _msgs():
    big = "y" * 5000
    return [{"role": "system", "content": big}, {"role": "user", "content": big},
            {"role": "assistant", "content": big}, {"role": "user", "content": "mid"},
            {"role": "assistant", "content": "tail"}, {"role": "user", "content": "REQUEST"}]


def _cached_idx(res):
    return [i for i, m in enumerate(res) if isinstance(m["content"], list)]


def test_inject_default_unchanged():
    m = _msgs()
    assert inject_cache_control(m, "anthropic/claude-sonnet-4-6") == \
        inject_cache_control(m, "anthropic/claude-sonnet-4-6", recent_end=None)
    assert _cached_idx(inject_cache_control(m, "anthropic/claude-sonnet-4-6")) == [0, 4]


def test_recent_end_moves_breakpoint2():
    res = inject_cache_control(_msgs(), "anthropic/claude-sonnet-4-6", recent_end=3)
    assert _cached_idx(res) == [0, 2]


def test_recent_end_never_marks_request():
    res = inject_cache_control(_msgs(), "anthropic/claude-sonnet-4-6", recent_end=99)
    assert 5 not in _cached_idx(res) and _cached_idx(res) == [0, 4]


def test_recent_end_before_start_falls_back():
    res = inject_cache_control(_msgs(), "anthropic/claude-sonnet-4-6", recent_end=0)
    assert _cached_idx(res) == [0, 4]


# ── context_injection.inject_system_block ────────────────────────────────────
def test_inject_system_block_returns_block_alone(monkeypatch):
    from llm_router import context_injection as ci
    monkeypatch.setattr(ci, "inject", lambda p, **k: f"KNOWLEDGE BLOCK\n\n{p}")
    assert ci.inject_system_block("do the thing") == "KNOWLEDGE BLOCK"


def test_inject_system_block_empty_when_unchanged_or_failing(monkeypatch):
    from llm_router import context_injection as ci
    monkeypatch.setattr(ci, "inject", lambda p, **k: p)
    assert ci.inject_system_block("x") == ""

    def boom(p, **k):
        raise RuntimeError
    monkeypatch.setattr(ci, "inject", boom)
    assert ci.inject_system_block("x") == ""
