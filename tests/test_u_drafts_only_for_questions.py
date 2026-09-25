"""U: hook drafts only for questions.

The hook drafted for every task type — code, generate, analyze — and Claude used
0 of 1,191 drafts (audit 2026-09-24). A draft for a code or instruction turn can
only add latency: Claude does the work itself either way. Drafting is limited
to questions (query, research); `LLM_ROUTER_DRAFT_TASKS=all` restores the old
behaviour. The quota-saving path is Claude calling `llm(...)` itself (smart
enforcement), not the draft. Test drafted locally via llm(task="code") and
corrected by review.
"""
import importlib.util
import io
import json
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"
REFACTOR = "Write a short haiku about autumn leaves"


def _load():
    cached = sys.modules.get("auto_route_u")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("auto_route_u", HOOK)
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_u"] = module
    spec.loader.exec_module(module)
    return module


def _run(monkeypatch, tmp_path, prompt):
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
    drafted: list = []
    monkeypatch.setattr(de, "execute_chain", lambda *a, **k: drafted.append(1))
    monkeypatch.setattr(de, "execute_agent", lambda *a, **k: drafted.append(1))
    log: list[str] = []
    monkeypatch.setattr(ar, "_debug_log", log.append)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"prompt": prompt, "session_id": "sess-u1v2w3"})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    try:
        ar.main()
    except SystemExit as e:
        assert e.code in (0, None)
    return drafted, log


def _skips(log):
    return [line for line in log if "drafts only for questions" in line]


def test_a_code_task_is_not_drafted(monkeypatch, tmp_path):
    drafted, log = _run(monkeypatch, tmp_path, REFACTOR)
    assert not any("task=query" in line or "task=research" in line for line in log), "premise: not a question"
    assert drafted == []
    assert len(_skips(log)) == 1, log


def test_a_question_is_still_drafted(monkeypatch, tmp_path):
    drafted, log = _run(monkeypatch, tmp_path, "What does os.path.join do?")
    assert drafted and not _skips(log)


def test_the_switch_restores_drafting_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_DRAFT_TASKS", "all")
    drafted, log = _run(monkeypatch, tmp_path, REFACTOR)
    assert drafted and not _skips(log)
