"""I6: the notice Claude reads must say what the draft actually saw.

Live 2026-09-24, after I4: a read-only loop draft opened six repo files and
answered correctly, and the hook still told Claude "this draft was produced
WITHOUT access to your files, codebase, tools..." and "if the answer depends on
... the user's files, repo ... IGNORE the draft entirely". A false premise
followed by a rule that discards every repo answer: no grounded draft could
ever be used, whatever its quality.

Now a draft that read files carries them (DirectResult.files_read), and the
notice names them and asks Claude to check the key claim instead of discarding.
A draft that read nothing keeps the old, still-true wording.
"""
from __future__ import annotations

import json

from llm_router.hooks import agent_loop
from llm_router.hooks.direct_executor import DirectResult, ModelSpec
from llm_router.hooks.response_formatter import format_direct_response, format_echo_context

M = ModelSpec(provider="ollama", model="qwen3-coder:30b")


def test_a_draft_that_read_files_is_not_described_as_blind():
    r = DirectResult(text="50", model=M, latency_ms=900,
                     files_read=("read_file(src/pkg/draft_usage.py)",))
    ctx = format_echo_context(r, "query", "simple")
    assert "WITHOUT access to your files" not in ctx
    assert "read_file(src/pkg/draft_usage.py)" in ctx
    assert "🎯 LLM Router routed" in ctx, "the relay line must still be offered"
    # The rule may still exclude what the draft could NOT see — but not the repo.
    ignore_rule = next(line for line in ctx.splitlines() if "IGNORE the draft" in line)
    assert "repo" not in ignore_rule and "files" not in ignore_rule, ignore_rule


def test_a_draft_that_read_nothing_keeps_the_blind_wording():
    r = DirectResult(text="x", model=M, latency_ms=900)
    ctx = format_echo_context(r, "query", "simple")
    assert "WITHOUT access to your files" in ctx


def test_block_mode_is_truthful_too():
    r = DirectResult(text="50", model=M, latency_ms=900,
                     files_read=("search_files(_REVERT_DEFAULT)",))
    out = format_direct_response(r, "query", "simple")
    assert "no access to your files" not in out.lower()
    assert "search_files(_REVERT_DEFAULT)" in out


class _R:
    def __init__(self, p): self._p = p
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return json.dumps(self._p).encode()


def test_the_loop_records_what_it_read(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("X = 1\n")
    seq = [{"content": "", "tool_calls": [{"function": {"name": "read_file",
                                                        "arguments": {"path": "a.py"}}}]},
           {"content": "X is 1"}]
    monkeypatch.setattr(agent_loop.urllib.request, "urlopen",
                        lambda *a, **k: _R({"message": seq.pop(0)}))
    log: list[str] = []
    out = agent_loop.run_agent_loop("q", "m", tmp_path, read_only=True, read_log=log)
    assert out == "X is 1"
    assert log == ["read_file(a.py)"]


def test_execute_agent_carries_the_reads_onto_the_result(monkeypatch, tmp_path):
    from llm_router.hooks import direct_executor as de

    def fake_loop(**kw):
        kw["read_log"].append("read_file(a.py)")
        return "X is 1 according to a.py"
    monkeypatch.setattr(agent_loop, "run_agent_loop", fake_loop)
    r = de.execute_agent("q", [M], project_root=str(tmp_path), read_only=True)
    assert r is not None and r.files_read == ("read_file(a.py)",)
