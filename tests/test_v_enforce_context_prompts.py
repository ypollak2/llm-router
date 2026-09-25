"""V: context-dependent prompts are enforced — route WITH context.

In a 10-day replay (2026-09-14..24, n=142 prompts the user typed), smart mode
would have enforced routing on only 42%: 51% were exempt because they refer to
the repo or session ("bounce-back fix": routed models once had no context, so a
forced llm() call was thrown away and Claude redid the work). llm(context=…)
now carries OKF, semantic and session context, so these prompts are enforced
with a directive to route WITH the relevant excerpts.
LLM_ROUTER_ENFORCE_CONTEXT=off restores the exemption. (A locally drafted
version of this test was discarded: it called a non-existent hook entry point
and swallowed every exception, so two tests passed without the hook running.)
"""
import importlib.util
import io
import json
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"
PROMPT = "why does the classifier in this repo return query for my prompt?"


def _load():
    cached = sys.modules.get("auto_route_v")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("auto_route_v", HOOK)
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_v"] = module
    spec.loader.exec_module(module)
    return module


def _run(monkeypatch, tmp_path, env):
    ar = _load()
    home = tmp_path / ".llm-router"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ar, "_router_dir", lambda: home, raising=False)
    monkeypatch.setattr(ar, "log_routing_decision", lambda **kw: None, raising=False)
    for k, v in {"LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1", "LLM_ROUTER_DIRECT_EXECUTION": "off",
                 "LLM_ROUTER_ZERO_CLAUDE": "off", **env}.items():
        monkeypatch.setenv(k, v)
    log: list[str] = []
    monkeypatch.setattr(ar, "_debug_log", log.append)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"prompt": PROMPT, "session_id": "sess-v1w2x3"})))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    try:
        ar.main()
    except SystemExit as e:
        assert e.code in (0, None)
    assert any("prompt_len=" in line for line in log), "premise: the hook ran"
    return out.getvalue(), list(home.glob("pending_route_*.json"))


def test_context_prompt_is_enforced_by_default(monkeypatch, tmp_path):
    out, pending = _run(monkeypatch, tmp_path, {"LLM_ROUTER_ENFORCE": "smart"})
    assert "CONTEXT" in out, "premise: classified context-dependent"
    assert pending, "no pending route written — enforce-route has nothing to enforce"
    assert "context=" in out and "stateless routed model cannot see" not in out


def test_switch_off_restores_the_exemption(monkeypatch, tmp_path):
    out, pending = _run(monkeypatch, tmp_path, {"LLM_ROUTER_ENFORCE": "smart",
                                                "LLM_ROUTER_ENFORCE_CONTEXT": "off"})
    assert "CONTEXT" in out and not pending and "Nothing is blocked" in out


def test_advise_mode_never_writes_pending(monkeypatch, tmp_path):
    out, pending = _run(monkeypatch, tmp_path, {"LLM_ROUTER_ENFORCE": "advise"})
    assert not pending
