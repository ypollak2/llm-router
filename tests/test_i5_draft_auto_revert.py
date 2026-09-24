"""I5: drafting turns itself off when nobody uses the drafts.

A draft costs the user wall-clock before Claude sees the prompt (up to the 55s
hook deadline with the read-only loop). Measured before I1-I4: 0 of 1,185
audited drafts used, ~12.7s median added per prompt. Re-enabling drafting is an
experiment, and an experiment that keeps charging latency after it has failed
is not an experiment.

The rule: after LLM_ROUTER_DRAFT_REVERT_AFTER (default 50) drafts in a row are
judged UNUSED by hooks/draft_usage.py, the hook stops drafting and logs
`DIRECT SKIP: auto-revert`. One USED draft resets the count. 50 because at
~55 drafts/day it is about a day of evidence, and if the true use rate were
10% the chance of 50 straight misses is 0.9^50 = 0.5%.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

from llm_router.hooks import draft_usage as du


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LLM_ROUTER_DRAFT_REVERT_AFTER", raising=False)


def _judge(text, sid="s-i5a"):
    du.record_draft(sid, 1.0, "m")
    return du.audit(sid, text)


def test_unused_verdicts_count_up_and_a_used_one_resets():
    for _ in range(3):
        assert _judge("I answered it myself.")[0] == du.UNUSED
    assert du.unused_streak() == 3
    assert _judge(du.RELAY_MARKER + " → qwen\n\nanswer")[0] == du.USED
    assert du.unused_streak() == 0


def test_no_draft_no_verdict_no_count():
    assert du.audit("s-none", "text") is None
    assert du.unused_streak() == 0


def test_the_revert_fires_at_the_threshold(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_DRAFT_REVERT_AFTER", "3")
    for _ in range(2):
        _judge("no")
    assert du.drafting_reverted() is None
    _judge("no")
    assert du.drafting_reverted() == 3


def test_the_default_threshold_is_50():
    for _ in range(49):
        _judge("no")
    assert du.drafting_reverted() is None
    _judge("no")
    assert du.drafting_reverted() == 50


def test_zero_disables_the_revert(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_DRAFT_REVERT_AFTER", "0")
    for _ in range(60):
        _judge("no")
    assert du.drafting_reverted() is None


# ── the hook honours it, and says so ─────────────────────────────────────────

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


def _load():
    cached = sys.modules.get("auto_route_i5")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("auto_route_i5", HOOK)
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_i5"] = module
    spec.loader.exec_module(module)
    return module


def _run_hook(monkeypatch, tmp_path):
    ar = _load()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ar, "_router_dir", lambda: tmp_path / ".llm-router", raising=False)
    monkeypatch.setattr(ar, "log_routing_decision", lambda **kw: None, raising=False)
    for k, v in {"LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1", "LLM_ROUTER_DIRECT_EXECUTION": "1",
                 "LLM_ROUTER_ENFORCE": "suggest", "LLM_ROUTER_ZERO_CLAUDE": "off"}.items():
        monkeypatch.setenv(k, v)
    import llm_router.hooks.chain_builder as chain_builder
    import llm_router.hooks.direct_executor as de
    model = de.ModelSpec(provider="ollama", model="fake-model")
    monkeypatch.setattr(chain_builder, "get_current_pressure", lambda: ("green", 10.0))
    monkeypatch.setattr(chain_builder, "build_chain", lambda c, z, t: [model])
    monkeypatch.setattr(chain_builder, "needs_claude_tools", lambda p, t: False)
    drafted = []
    monkeypatch.setattr(de, "execute_chain", lambda *a, **k: drafted.append(1))
    monkeypatch.setattr(de, "execute_agent", lambda *a, **k: drafted.append(1))
    log: list[str] = []
    monkeypatch.setattr(ar, "_debug_log", log.append)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"prompt": "What does os.path.join do?", "session_id": "sess-i5b7c9"})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    try:
        ar.main()
    except SystemExit as e:        # the hook exits on some paths, returns on others
        assert e.code in (0, None)
    return drafted, log


def test_the_hook_stops_drafting_once_reverted(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_DRAFT_REVERT_AFTER", "2")
    _judge("no")
    _judge("no")
    drafted, log = _run_hook(monkeypatch, tmp_path)
    assert drafted == [], "a reverted hook must not spend the user's time drafting"
    skips = [line for line in log if "DIRECT SKIP:" in line]
    assert len(skips) == 1 and "auto-revert" in skips[0], skips


def test_below_the_threshold_the_hook_still_drafts(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_DRAFT_REVERT_AFTER", "2")
    _judge("no")
    drafted, log = _run_hook(monkeypatch, tmp_path)
    assert drafted, "premise: this prompt drafts when not reverted"
    assert not any("auto-revert" in line for line in log)
