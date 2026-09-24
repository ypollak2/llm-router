"""M: a background-task notification is not a user prompt — do not draft on it.

Claude Code delivers `<task-notification>` messages (a background command or
sub-agent finished) through UserPromptSubmit like a typed prompt. Measured
2026-09-24 over the last 7 days of transcripts on the maintainer's machine:
190 of 733 user-turn prompts (25.9%) were notifications. Each one could cost up
to the 55s hook deadline in local drafting, and its draft can never be relayed
— so it was also judged UNUSED and pushed the I5 auto-revert toward firing on
traffic that was never a question. Seen live the same day: a 16.3s qwen3-coder
draft summarising a commit, attached to a notification.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path


HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"

NOTIFICATION = (
    "<task-notification>\n<task-id>b8x</task-id>\n<status>completed</status>\n"
    "<summary>Background command \"Run full suite\" completed (exit code 0)</summary>\n"
    "</task-notification>"
)


def _load():
    cached = sys.modules.get("auto_route_m")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("auto_route_m", HOOK)
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_m"] = module
    spec.loader.exec_module(module)
    return module


def _run(monkeypatch, tmp_path, prompt, zero_claude="off"):
    ar = _load()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ar, "_router_dir", lambda: tmp_path / ".llm-router", raising=False)
    monkeypatch.setattr(ar, "log_routing_decision", lambda **kw: None, raising=False)
    for k, v in {"LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1", "LLM_ROUTER_DIRECT_EXECUTION": "1",
                 "LLM_ROUTER_ENFORCE": "suggest", "LLM_ROUTER_ZERO_CLAUDE": zero_claude}.items():
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
        {"prompt": prompt, "session_id": "sess-m4n5o6"})))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    try:
        ar.main()
    except SystemExit as e:
        assert e.code in (0, None)
    return drafted, log, out.getvalue()


def test_a_notification_is_not_drafted_and_says_why(monkeypatch, tmp_path):
    drafted, log, _ = _run(monkeypatch, tmp_path, NOTIFICATION)
    assert drafted == []
    outcomes = [line for line in log
                if any(t in line for t in ("DIRECT:", "DIRECT SKIP:", "BYPASS", "CONTINUATION"))]
    assert len(outcomes) == 1 and "SYSTEM_NOTIFICATION_BYPASS" in outcomes[0], outcomes


def test_zero_claude_does_not_block_a_notification(monkeypatch, tmp_path):
    _, _, out = _run(monkeypatch, tmp_path, NOTIFICATION, zero_claude="on")
    assert '"block"' not in out, "a notification is not a turn zero-Claude may refuse"


def test_a_prompt_that_only_mentions_the_tag_still_drafts(monkeypatch, tmp_path):
    """Premise + precision: the match is the message SHAPE, not the word."""
    drafted, _, _ = _run(monkeypatch, tmp_path,
                         "What does os.path.join do? (not a <task-notification>)")
    assert drafted, "an ordinary question must still reach the draft path"


# ── O: a gated prompt logs ONE outcome, and the right one ────────────────────
# Found while writing M: a context-dependent prompt logged "DIRECT SKIP:
# context-dependent prompt" AND "DIRECT SKIP: direct execution disabled by env"
# — two terminal outcomes, the second false (the env had drafting ON).

def test_a_gated_prompt_logs_exactly_one_true_outcome(monkeypatch, tmp_path):
    _, log, _ = _run(monkeypatch, tmp_path, "fix the bug in this file")
    outcomes = [line for line in log
                if any(t in line for t in ("DIRECT:", "DIRECT SKIP:", "BYPASS", "CONTINUATION"))]
    assert any("context-dependent" in o for o in outcomes), f"premise: {outcomes}"
    assert len(outcomes) == 1, outcomes


def test_drafting_off_by_env_still_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_DIRECT_EXECUTION", "off")
    ar = _load()
    import llm_router.hooks.direct_executor as de
    log: list[str] = []
    monkeypatch.setattr(ar, "_debug_log", log.append)
    monkeypatch.setattr(ar, "log_routing_decision", lambda **kw: None, raising=False)
    monkeypatch.setattr(de, "execute_chain", lambda *a, **k: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"prompt": "What does os.path.join do?", "session_id": "sess-m4n5o6"})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    try:
        ar.main()
    except SystemExit:
        pass
    skips = [line for line in log if "DIRECT SKIP:" in line]
    assert len(skips) == 1 and "disabled by env" in skips[0], skips
