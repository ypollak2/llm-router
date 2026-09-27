"""NS3 lever 1: hardening tests for llm_edit.

Covers the rules validated by rsi_engine.scaffold (~/Projects/rsi-engine) that
llm_edit did not previously enforce:

1. exact-once matching (apply_edits rejects a non-unique or absent old_string)
2. syntax check of the result for known file types (Python/JSON/YAML)
3. all-or-nothing apply (one bad instruction rejects the whole batch)
4. the format example uses the REAL file path, not a placeholder
5. retry-with-feedback in the llm_edit MCP tool (max 3 attempts)
6. thinking is off for Ollama models reached via route_and_call
7. the ledger row llm_edit writes to edit_outcomes.jsonl
"""

from __future__ import annotations

import json
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── apply_edits: exact-once matching ─────────────────────────────────────────


def test_apply_edits_rejects_absent_old_string():
    from llm_router.edit import EditInstruction, apply_edits

    files = {"a.py": "x = 1\n"}
    instr = [EditInstruction(file="a.py", old_string="y = 2", new_string="y = 3")]
    new_contents, reasons = apply_edits(files, instr)
    assert new_contents is None
    assert any("0 times" in r for r in reasons)


def test_apply_edits_rejects_non_unique_old_string():
    from llm_router.edit import EditInstruction, apply_edits

    files = {"a.py": "x = 1\nx = 1\n"}
    instr = [EditInstruction(file="a.py", old_string="x = 1", new_string="x = 2")]
    new_contents, reasons = apply_edits(files, instr)
    assert new_contents is None
    assert any("2 times" in r for r in reasons)


def test_apply_edits_accepts_unique_match():
    from llm_router.edit import EditInstruction, apply_edits

    files = {"a.py": "x = 1\ny = 2\n"}
    instr = [EditInstruction(file="a.py", old_string="x = 1", new_string="x = 9")]
    new_contents, reasons = apply_edits(files, instr)
    assert reasons == []
    assert new_contents == {"a.py": "x = 9\ny = 2\n"}


def test_apply_edits_rejects_empty_old_string():
    from llm_router.edit import EditInstruction, apply_edits

    files = {"a.py": "x = 1\n"}
    instr = [EditInstruction(file="a.py", old_string="   ", new_string="y")]
    new_contents, reasons = apply_edits(files, instr)
    assert new_contents is None
    assert any("empty" in r for r in reasons)


# ── apply_edits: syntax check ────────────────────────────────────────────────


def test_check_syntax_python_valid():
    from llm_router.edit import check_syntax

    assert check_syntax("a.py", "def f():\n    return 1\n") is None


def test_check_syntax_python_invalid():
    from llm_router.edit import check_syntax

    err = check_syntax("a.py", "def f(:\n    return 1\n")
    assert err is not None
    assert "a.py" in err


def test_check_syntax_json_invalid():
    from llm_router.edit import check_syntax

    err = check_syntax("a.json", '{"x": }')
    assert err is not None


def test_check_syntax_yaml_invalid():
    from llm_router.edit import check_syntax

    err = check_syntax("a.yaml", "x: [1, 2\n")
    assert err is not None


def test_check_syntax_unknown_extension_not_checked():
    from llm_router.edit import check_syntax

    # .txt has no known checker — garbage is not flagged, by design (scope
    # limitation documented on check_syntax, not silently "fine").
    assert check_syntax("a.txt", "not { valid json") is None


def test_apply_edits_rejects_syntax_break():
    """A batch that applies cleanly (exact-once) but breaks Python syntax is
    still rejected — the syntax gate runs after the string-match gate."""
    from llm_router.edit import EditInstruction, apply_edits

    files = {"a.py": "def f():\n    return 1\n"}
    instr = [EditInstruction(file="a.py", old_string="return 1", new_string="return (")]
    new_contents, reasons = apply_edits(files, instr)
    assert new_contents is None
    assert any("valid py" in r for r in reasons)


def test_apply_edits_all_or_nothing():
    """One bad instruction in a multi-file batch rejects ALL of it — the
    good file is not silently written while the bad one is dropped."""
    from llm_router.edit import EditInstruction, apply_edits

    files = {"a.py": "x = 1\n", "b.py": "y = 1\n"}
    instr = [
        EditInstruction(file="a.py", old_string="x = 1", new_string="x = 2"),
        EditInstruction(file="b.py", old_string="NOT PRESENT", new_string="y = 2"),
    ]
    new_contents, reasons = apply_edits(files, instr)
    assert new_contents is None
    assert reasons  # b.py's failure is reported; a.py's success is discarded


# ── build_edit_prompt: real path in the example ──────────────────────────────


def test_prompt_example_uses_real_first_path_not_placeholder():
    from llm_router.edit import build_edit_prompt

    prompt = build_edit_prompt("fix bug", {"src/real_module.py": "x = 1"})
    assert "src/real_module.py" in prompt
    # The old placeholder must be gone — a model that pattern-matches the
    # example must not be able to copy a fake path into its answer.
    assert "src/main.py" not in prompt


def test_prompt_includes_feedback_when_given():
    from llm_router.edit import build_edit_prompt

    prompt = build_edit_prompt("fix bug", {"a.py": "x = 1"}, feedback="old_string appeared 0 times")
    assert "previous answer was rejected" in prompt
    assert "old_string appeared 0 times" in prompt


def test_prompt_omits_feedback_section_on_first_attempt():
    from llm_router.edit import build_edit_prompt

    prompt = build_edit_prompt("fix bug", {"a.py": "x = 1"})
    assert "previous answer was rejected" not in prompt


# ── llm_edit MCP tool: retry with feedback ───────────────────────────────────


def _mock_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.request_id = "test-request-id"
    return ctx


def _mock_resp(content: str, model: str = "ollama/qwen3.5:latest") -> MagicMock:
    resp = MagicMock()
    resp.content = content
    resp.model = model
    resp.citations = []
    resp.header.return_value = f"Model: {model}"
    return resp


def _good_json(path: str) -> str:
    return json.dumps([
        {"file": path, "old_string": "x = 1", "new_string": "x = 2", "description": "bump"}
    ])


def _bad_json(path: str) -> str:
    return json.dumps([{"file": path, "old_string": "NOT IN FILE", "new_string": "x = 2"}])


@pytest.mark.asyncio
async def test_llm_edit_retries_with_feedback_then_succeeds(tmp_path, monkeypatch):
    from llm_router.tools.text import llm_edit

    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))

    responses = [_mock_resp(_bad_json(str(f))), _mock_resp(_good_json(str(f)))]
    mock_route = AsyncMock(side_effect=responses)

    with (
        patch("llm_router.tools.text.route_and_call", mock_route),
        patch("llm_router.tools.text._announce_routing", new_callable=AsyncMock),
    ):
        out = await llm_edit("bump x", [str(f)], _mock_ctx())

    assert mock_route.await_count == 2
    # The retry prompt carries the rejection reason forward.
    second_call_prompt = mock_route.await_args_list[1].args[1]
    assert "0 times" in second_call_prompt
    assert "applied=True" in out
    assert "attempts=2" in out


@pytest.mark.asyncio
async def test_llm_edit_gives_up_after_max_attempts(tmp_path, monkeypatch):
    from llm_router.tools.text import llm_edit

    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))

    mock_route = AsyncMock(return_value=_mock_resp(_bad_json(str(f))))

    with (
        patch("llm_router.tools.text.route_and_call", mock_route),
        patch("llm_router.tools.text._announce_routing", new_callable=AsyncMock),
    ):
        out = await llm_edit("bump x", [str(f)], _mock_ctx())

    assert mock_route.await_count == 3  # MAX_EDIT_ATTEMPTS
    assert "applied=False" in out
    assert "attempts=3" in out


@pytest.mark.asyncio
async def test_llm_edit_succeeds_first_try_makes_one_call(tmp_path, monkeypatch):
    from llm_router.tools.text import llm_edit

    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))

    mock_route = AsyncMock(return_value=_mock_resp(_good_json(str(f))))

    with (
        patch("llm_router.tools.text.route_and_call", mock_route),
        patch("llm_router.tools.text._announce_routing", new_callable=AsyncMock),
    ):
        out = await llm_edit("bump x", [str(f)], _mock_ctx())

    assert mock_route.await_count == 1
    assert "applied=True" in out
    assert "attempts=1" in out


# ── thinking off reaches the router's actual dispatch path ───────────────────


@pytest.mark.asyncio
async def test_route_and_call_sends_think_false_for_ollama(monkeypatch):
    """llm_edit calls route_and_call(TaskType.CODE, ...), which dispatches
    through providers.call_llm. Confirms an Ollama model on THAT path — not
    just a direct call_llm() call — still gets think=False, closing the gap
    between 'call_llm defaults this' and 'the router plumbing llm_edit uses
    doesn't strip it away'."""
    import litellm

    from llm_router.router import route_and_call
    from llm_router.types import TaskType

    captured: dict = {}

    async def _fake_acompletion(**kwargs):
        captured.clear()
        captured.update(kwargs)
        usage = types.SimpleNamespace(
            prompt_tokens=5, completion_tokens=5,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )
        msg = types.SimpleNamespace(content="[]", tool_calls=None)
        choice = types.SimpleNamespace(message=msg)
        return types.SimpleNamespace(choices=[choice], usage=usage)

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_THINK", raising=False)

    await route_and_call(
        TaskType.CODE,
        "return an empty JSON array",
        model_override="ollama/qwen3.5:latest",
        temperature=0.1,
    )

    assert captured.get("model") == "ollama/qwen3.5:latest"
    assert captured.get("think") is False


# ── ledger ────────────────────────────────────────────────────────────────────


def test_record_edit_outcome_writes_expected_row(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-123")
    from llm_router.edit_ledger import LEDGER_FILENAME, record_edit_outcome

    record_edit_outcome(file="src/foo.py", model="ollama/qwen3.5:latest", applied=True)

    ledger_path = tmp_path / LEDGER_FILENAME
    rows = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["file"] == "src/foo.py"
    assert row["model"] == "ollama/qwen3.5:latest"
    assert row["applied"] is True
    assert row["survived"] is None
    assert row["session_id"] == "sess-123"
    assert isinstance(row["ts"], float)


def test_record_edit_outcome_never_raises_on_bad_path(tmp_path, monkeypatch):
    from llm_router.edit_ledger import record_edit_outcome

    # A FILE where a directory is expected: mkdir(parents=True) must fail
    # with NotADirectoryError, which record_edit_outcome must swallow.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(blocker / "sub"))
    # Must not raise — fail-silent, matching _cache_result / _record_quality.
    record_edit_outcome(file="a.py", model="x", applied=True)


@pytest.mark.asyncio
async def test_llm_edit_writes_ledger_row_on_success(tmp_path, monkeypatch):
    from llm_router.edit_ledger import LEDGER_FILENAME
    from llm_router.tools.text import llm_edit

    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(state_dir))

    mock_route = AsyncMock(return_value=_mock_resp(_good_json(str(f))))
    with (
        patch("llm_router.tools.text.route_and_call", mock_route),
        patch("llm_router.tools.text._announce_routing", new_callable=AsyncMock),
    ):
        await llm_edit("bump x", [str(f)], _mock_ctx())

    ledger_path = state_dir / LEDGER_FILENAME
    rows = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert any(r["file"] == str(f) and r["applied"] is True for r in rows)
    assert any(r["model"] == "ollama/qwen3.5:latest" for r in rows)
