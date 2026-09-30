"""llm_router.local_agent: capability gating, compaction, the edit hard rule.

No network: embeddings, the quality breaker, the serving model and Anthropic
are all fakes. The request bodies are synthetic but shaped like a real Claude
Code 2.1.x request captured on 2026-09-29 with ENABLE_TOOL_SEARCH=true (reminder
blocks in the first user turn, a role:system SessionStart message, deferred
tools, a tool_reference result).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from llm_router.local_agent import LocalAgentConfig, capability, compact
from llm_router.local_agent.proxy_step import LocalAgent
from llm_router.proxy import ledger
from llm_router.proxy import server as ps
from llm_router.proxy.steps import STEP_CONTINUATION
from llm_router.proxy.translate import SERVED_TOOL_ID_PREFIX, from_ollama

STEPS = frozenset({STEP_CONTINUATION})


def _tool(name, desc="", props=None, required=(), **extra):
    return {"name": name, "description": desc or f"The {name} tool.",
            "input_schema": {"type": "object", "properties": props or {"x": {"type": "string"}},
                             "required": list(required)}, **extra}


TOOLS = [
    _tool("Agent", "Launch a new agent to handle complex multi-step tasks. " + "a" * 1600),
    _tool("Bash", "Executes a bash command and returns its output. " + "b" * 1300,
          {"command": {"type": "string"}}, ["command"]),
    _tool("Edit", "Performs exact string replacements in files.",
          {"file_path": {"type": "string"}, "old_string": {"type": "string"},
           "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}},
          ["file_path", "old_string", "new_string"]),
    _tool("Glob", "Fast file pattern matching."),
    _tool("Grep", "A powerful search tool built on ripgrep."),
    _tool("Read", "Reads a file from the local filesystem.", {"file_path": {"type": "string"}}, ["file_path"]),
    _tool("ScheduleWakeup", "Schedule a wakeup. " + "s" * 3000),
    _tool("Workflow", "Run a workflow script. " + "w" * 3400),
    _tool("Write", "Writes a file to the local filesystem.",
          {"file_path": {"type": "string"}, "content": {"type": "string"}}, ["file_path", "content"]),
    _tool("ToolSearch", "Fetches full schema definitions for deferred tools."),
    _tool("mcp__llm_router__llm", "Route a prompt to a cheap model.", defer_loading=True),
    _tool("mcp__other__thing", "Some deferred MCP tool.", defer_loading=True),
]

ASK = "Run the full pytest suite first (python3 -m pytest -q). Fix ONLY the source module responsible."


def _body(cwd="/work/repo", pairs=None, *, reminder="R" * 18000):
    """A continuation request: first ask (with reminders), a system message,
    then (assistant tool_use, user tool_result) pairs."""
    system = [{"type": "text", "text": "x-anthropic-billing-header: fixture"},
              {"type": "text", "text": "You are Claude Code.\n# Environment\n"
                                       f" - Primary working directory: {cwd}\n - Is a git repository: true\n"
                                       " - Platform: darwin\n - Shell: zsh\n" + "sys " * 1500}]
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": f"<system-reminder>\n{reminder}\n</system-reminder>"},
        {"type": "text", "text": ASK}]},
        {"role": "system", "content": "SessionStart hook additional context: " + "h" * 30000}]
    pairs = pairs if pairs is not None else [
        ("Bash", {"command": "python3 -m pytest -q"}, "F....\nFAILED tests/test_pagination.py::test_total_pages\n"
         + "x" * 9000),
        ("Grep", {"pattern": "def total_pages"}, "src/pkg/pagination.py:5:def total_pages"),
    ]
    for i, (name, inp, out) in enumerate(pairs):
        tid = f"toolu_{i:04d}"
        msgs.append({"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "tool_use", "id": tid, "name": name, "input": inp}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": out},
                                                 {"type": "text", "text": "<system-reminder>r</system-reminder>"}]})
    return {"model": "claude-sonnet-5-5", "system": system, "messages": msgs, "tools": json.loads(json.dumps(TOOLS)),
            "max_tokens": 32000, "stream": True, "metadata": {"user_id": json.dumps({"session_id": "s1"})}}


class FakeEmbed:
    """Deterministic 'embedding': bag of a few keywords."""

    VOCAB = ["file", "bash", "command", "search", "read", "edit", "agent", "schedule", "workflow", "pytest"]

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def __call__(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise httpx.ConnectError("ollama down")
        return [[float(t.lower().count(w)) + 0.01 for w in self.VOCAB] for t in texts]


def _retriever(embed=None, tmp_path=None):
    return compact.ToolRetriever(embed, "nomic-embed-text", tmp_path / "emb.json" if tmp_path else None)


# ── compaction ───────────────────────────────────────────────────────────────


async def test_compaction_fits_budget_and_keeps_exact_tool_names_and_schemas(tmp_path):
    body = _body()
    assert compact.estimate_tokens(body) > 15000  # the uncompacted request is large
    out, info = await compact.compact(body, _retriever(FakeEmbed(), tmp_path), LocalAgentConfig())
    assert info["est_tokens_out"] <= 5000 and not info["over_budget"]
    assert info["retrieval"]["method"] == "embed"
    by_name = {t["name"]: t for t in body["tools"]}
    for t in out["tools"]:
        assert t["input_schema"] == by_name[t["name"]]["input_schema"]  # schemas byte-identical
    names = [t["name"] for t in out["tools"]]
    assert names == [n for n in by_name if n in names]  # request order kept
    assert {"Bash", "Grep", "Edit", "Write"} <= set(names)  # used + edit signal tools
    assert "mcp__other__thing" not in names  # deferred and never loaded: not offered
    assert len(names) < len(body["tools"])


async def test_history_drops_reminders_keeps_ask_env_and_newest_output(tmp_path):
    body = _body()
    out, _ = await compact.compact(body, _retriever(FakeEmbed(), tmp_path), LocalAgentConfig())
    dump = json.dumps(out["messages"])
    assert "<system-reminder>" not in dump and "R" * 100 not in dump
    assert "SessionStart" not in dump and "h" * 100 not in dump
    assert out["messages"][0] == {"role": "user", "content": ASK}
    assert "Primary working directory: /work/repo" in out["system"]
    assert "FAILED tests/test_pagination.py::test_total_pages" in dump  # head of the long output kept
    assert "omitted by llm-router compaction" in dump  # and its middle cut, visibly
    assert "thinking" not in dump
    assert body["messages"][0]["content"][0]["text"].startswith("<system-reminder>")  # input not mutated


async def test_older_steps_are_dropped_but_a_needed_file_read_is_kept(tmp_path):
    pairs = [("Read", {"file_path": "/work/repo/src/pkg/pagination.py"}, "def total_pages(n, k):\n    return n // k"),
             ("Bash", {"command": "ls"}, "a b c"), ("Bash", {"command": "echo 1"}, "1"),
             ("Bash", {"command": "echo 2"}, "2"),
             ("Bash", {"command": "python3 -m pytest -q"}, "FAILED ... src/pkg/pagination.py:6 AssertionError")]
    out, info = await compact.compact(_body(pairs=pairs), _retriever(FakeEmbed(), tmp_path),
                                      LocalAgentConfig(keep_results=2))
    assert info["results_kept"] == 2 and info["file_reads_kept"] == 1 and info["steps_omitted"] == 2
    dump = json.dumps(out["messages"])
    assert "return n // k" in dump and "echo 1" not in dump
    assert "[2 earlier tool step(s) omitted" in out["messages"][0]["content"]


async def test_ladder_shrinks_to_a_small_budget(tmp_path):
    out, info = await compact.compact(_body(), _retriever(FakeEmbed(), tmp_path),
                                      LocalAgentConfig(max_prompt_tokens=1500))
    assert info["est_tokens_out"] <= 1500, info
    assert info["ladder_rung"] > 0
    assert {"Edit", "Write", "Grep"} <= set(info["tools_kept"])  # protected: edit signal + newest answered


async def test_tool_embeddings_are_computed_once_and_cached_on_disk(tmp_path):
    embed = FakeEmbed()
    r = _retriever(embed, tmp_path)
    await compact.compact(_body(), r, LocalAgentConfig())
    doc_batches = [c for c in embed.calls if c[0].startswith("search_document: ")]
    assert len(doc_batches) == 1 and len(doc_batches[0]) == len(TOOLS)
    await compact.compact(_body(), r, LocalAgentConfig())
    assert len([c for c in embed.calls if c[0].startswith("search_document: ")]) == 1  # memory cache
    embed2 = FakeEmbed()
    await compact.compact(_body(), _retriever(embed2, tmp_path), LocalAgentConfig())  # new process, disk cache
    assert all(c[0].startswith("search_query: ") for c in embed2.calls)
    assert json.loads((tmp_path / "emb.json").read_text())  # keyed by hash, not tool text
    assert "Bash" not in (tmp_path / "emb.json").read_text()


async def test_embed_failure_falls_back_to_lexical_and_says_so(tmp_path):
    out, info = await compact.compact(_body(), _retriever(FakeEmbed(fail=True), tmp_path), LocalAgentConfig())
    assert info["retrieval"]["method"] == "lexical"
    assert "ConnectError" in info["retrieval"]["embed_error"]
    assert out["tools"]


async def test_compaction_info_holds_no_prompt_or_tool_output_text(tmp_path):
    _, info = await compact.compact(_body(), _retriever(FakeEmbed(), tmp_path), LocalAgentConfig())
    dump = json.dumps(info)
    assert "pytest" not in dump and "total_pages" not in dump and "/work/repo" not in dump


# ── capability: before the call ──────────────────────────────────────────────


class Breaker:
    def __init__(self, allowed=True):
        self.allowed, self.calls = allowed, []

    def __call__(self, lever, task_type):
        self.calls.append((lever, task_type))
        return type("D", (), {"allowed": self.allowed, "reason": f"fake {self.allowed}"})()


def _step(body=None, task_type="code", model="ollama/qwen3-coder:30b", steps=STEPS):
    return capability.Step(body=body or _body(), enabled_steps=steps, task_type=task_type, model=model)


async def test_capability_gates_in_order_with_reasons():
    b = capability.BreakerCache(fn=Breaker())
    first = _body(pairs=[])
    first["messages"] = first["messages"][:2]
    assert (await capability.can_serve_locally(_step(first), b)).reason == "not_eligible"
    assert (await capability.can_serve_locally(_step(steps=frozenset()), b)).reason == "not_eligible"
    assert (await capability.can_serve_locally(_step(model=None), b)).reason == "policy_kept"
    d = await capability.can_serve_locally(_step(task_type="generate"), b)
    assert (d.route, d.reason) == (capability.ROUTE_CLAUDE, "task_type_unsuitable")
    multi = _body()
    multi["messages"][0]["content"][1]["text"] = "Refactor the entire codebase to use pathlib"
    d = await capability.can_serve_locally(_step(multi), b)
    assert (d.reason, d.detail) == ("multi_file_write", "multi-file-write signal in the ask")
    assert "Refactor" not in json.dumps(d.as_row())  # no prompt text in the ledger
    ok = await capability.can_serve_locally(_step(), b)
    assert (ok.route, ok.reason, ok.local) == (capability.ROUTE_LOCAL, "capable", True)
    closed = await capability.can_serve_locally(_step(), capability.BreakerCache(fn=Breaker(False)))
    assert (closed.route, closed.reason) == (capability.ROUTE_CLAUDE, "breaker_open")


async def test_breaker_is_asked_for_the_proxy_lever_and_cached():
    fake = Breaker()
    b = capability.BreakerCache(fn=fake)
    for _ in range(3):
        await capability.can_serve_locally(_step(), b)
    await capability.can_serve_locally(_step(task_type="analyze"), b)
    assert fake.calls == [("proxy", "code"), ("proxy", "analyze")]


async def test_breaker_error_fails_open_with_a_reason():
    def boom(lever, task_type):
        raise RuntimeError("state file unreadable")
    d = await capability.can_serve_locally(_step(), capability.BreakerCache(fn=boom))
    assert d.local and "fail-open" in d.detail


def _hook_module():
    path = Path(capability.__file__).parents[1] / "hooks" / "agent-route.py"
    spec = importlib.util.spec_from_file_location("agent_route_hook_for_capability", path)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


def test_task_shape_gate_is_the_same_as_the_codex_hooks():
    hook = _hook_module()
    assert capability.SUITABLE_TASK_TYPES == frozenset(hook._CODEX_SUITABLE_TASK_TYPES)
    assert capability.MULTI_FILE_WRITE_SIGNALS.pattern == hook._MULTI_FILE_WRITE_SIGNALS.pattern
    assert capability.MULTI_FILE_WRITE_SIGNALS.flags == hook._MULTI_FILE_WRITE_SIGNALS.flags


# ── capability: the edit hard rule ───────────────────────────────────────────


def _git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "mod.py").write_text("def total(n, k):\n    return n // k\n")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)
    return repo


def _edit_message(path, name="Edit"):
    inp = {"file_path": str(path), "old_string": "n // k", "new_string": "-(-n // k)"}
    if name == "Write":
        inp = {"file_path": str(path), "content": "x"}
    return {"content": [{"type": "tool_use", "id": "toolu_lrX", "name": name, "input": inp}]}


def _read_then_test_body(repo):
    target = repo / "src" / "mod.py"
    return _body(cwd=str(repo), pairs=[("Read", {"file_path": str(target)}, "def total(n, k): ..."),
                                       ("Bash", {"command": "pytest"}, "FAILED test_total")])


def test_non_edit_reply_stays_on_the_local_loop():
    msg = {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}
    assert capability.check_reply(msg, _body()).route == capability.ROUTE_LOCAL


def test_edit_reply_is_never_served_from_the_raw_loop(tmp_path):
    repo = _git_repo(tmp_path)
    target = repo / "src" / "mod.py"
    body = _read_then_test_body(repo)
    for name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        d = capability.check_reply(_edit_message(target, name), body, edit_mode="claude")
        assert d.route == capability.ROUTE_CLAUDE and d.reason == "edit_to_claude", name
    for name in ("Write", "NotebookEdit"):
        d = capability.check_reply(_edit_message(target, name), body, edit_mode="protocol")
        assert d.route == capability.ROUTE_CLAUDE, name
    d = capability.check_reply(_edit_message(target), body, edit_mode="protocol")
    assert (d.route, d.targets) == (capability.ROUTE_EDIT, (os.path.realpath(target),))


def test_edit_protocol_needs_a_read_clean_tracked_file(tmp_path):
    repo = _git_repo(tmp_path)
    target = repo / "src" / "mod.py"
    unread = _body(cwd=str(repo), pairs=[("Bash", {"command": "pytest"}, "FAILED")])
    assert capability.check_reply(_edit_message(target), unread).detail == "target not read in this session"
    body = _read_then_test_body(repo)
    target.write_text("def total(n, k):\n    return n // k  # user change\n")  # uncommitted
    assert capability.check_reply(_edit_message(target), body).detail == "target has uncommitted changes"
    seen = []
    d = capability.check_reply(_edit_message(target), body, dirty_fn=lambda root, rels: seen.append(rels) or [])
    assert d.route == capability.ROUTE_EDIT and seen == [["src/mod.py"]]  # dirty_files is the check
    missing = _edit_message(repo / "src" / "nope.py")
    assert capability.check_reply(missing, body).detail == "target is not an existing file"


async def test_run_edit_protocol_validates_and_retries_with_feedback(tmp_path):
    repo = _git_repo(tmp_path)
    target = os.path.realpath(repo / "src" / "mod.py")
    body = _read_then_test_body(repo)
    decision = capability.Decision(capability.ROUTE_EDIT, "edit_shaped", targets=(target,))
    prompts = []
    replies = [json.dumps([{"file": target, "old_string": "not in file", "new_string": "x"}]),
               json.dumps([{"file": target, "old_string": "return n // k", "new_string": "return -(-n // k)"}])]

    async def generate(system, prompt, num_predict, timeout_s):
        prompts.append(prompt)
        return replies[len(prompts) - 1], {"prompt_tokens": 100, "output_tokens": 20}

    msg, err, info = await capability.run_edit_protocol(decision, _edit_message(target), body, generate,
                                                        deadline=asyncio.get_event_loop().time() + 1e6)
    assert err is None and info["attempts"] == 2
    assert "Your previous answer was rejected" in prompts[1] and "appears 0 times" in prompts[1]
    (block,) = msg["content"]
    assert block["name"] == "Edit" and block["id"].startswith(SERVED_TOOL_ID_PREFIX)
    assert block["input"] == {"file_path": target, "old_string": "return n // k", "new_string": "return -(-n // k)"}
    assert msg["usage"]["input_tokens"] == 200


async def test_run_edit_protocol_gives_up_to_claude_after_max_attempts(tmp_path):
    repo = _git_repo(tmp_path)
    target = os.path.realpath(repo / "src" / "mod.py")
    decision = capability.Decision(capability.ROUTE_EDIT, "edit_shaped", targets=(target,))

    async def generate(system, prompt, num_predict, timeout_s):
        return "not json", {}

    import time
    msg, err, info = await capability.run_edit_protocol(decision, _edit_message(target),
                                                        _read_then_test_body(repo), generate,
                                                        deadline=time.monotonic() + 1e6)
    assert msg is None and err.startswith("edit protocol:") and info["attempts"] == 3


# ── wired into the proxy ─────────────────────────────────────────────────────


def _ollama(content="", calls=None):
    return {"message": {"role": "assistant", "content": content, "tool_calls": calls or []},
            "done_reason": "stop", "prompt_eval_count": 1234, "eval_count": 56}


class FakeBackend:
    def __init__(self, reply, edit_reply=None):
        self.reply, self.edit_reply, self.calls, self.edit_prompts = reply, edit_reply, [], []

    async def complete(self, body, timeout_s):
        self.calls.append(body)
        m, err = from_ollama(self.reply, body)
        return m, err, {"prompt_tokens": 10, "output_tokens": 2}

    async def generate_text(self, system, prompt, num_predict, timeout_s):
        self.edit_prompts.append(prompt)
        return self.edit_reply, {"prompt_tokens": 50, "output_tokens": 10}


def _sse():
    ev = [("message_start", {"type": "message_start", "message": {
        "id": "msg_up", "type": "message", "role": "assistant", "model": "m", "content": [],
        "stop_reason": None, "usage": {"input_tokens": 1, "output_tokens": 1}}}),
          ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                             "usage": {"output_tokens": 2}}),
          ("message_stop", {"type": "message_stop"})]
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in ev).encode()


@pytest.fixture
def policy(monkeypatch):
    async def _choose(text, pinned):
        return {"task_type": "code", "complexity": "moderate", "chain_head": ["ollama/fake:1"],
                "model": "ollama/fake:1"}
    monkeypatch.setattr(ps, "choose_model", _choose)


def _app(tmp_path, backend, local_agent: LocalAgentConfig | None, monkeypatch):
    up = []

    def upstream(request):
        up.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(_sse()), headers={"content-type": "text/event-stream"})

    cfg = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=tmp_path / "calls.jsonl",
                         warm_up=False, local_agent=local_agent)
    if local_agent is not None:
        real_init = LocalAgent.__init__

        def init(self, cfg_, **kw):
            real_init(self, cfg_, embed=FakeEmbed(), breaker_fn=Breaker(), cache_path=tmp_path / "emb.json")
        monkeypatch.setattr(LocalAgent, "__init__", init)
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return ps.build_app(cfg, client=client, backend_factory=lambda model: backend), up


async def _post(app, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as c:
        return await c.post("/v1/messages", content=json.dumps(body),
                            headers={"authorization": "Bearer sk-ant-oat01-x", "content-type": "application/json"})


def _edit_call(path):
    return {"function": {"name": "Edit", "arguments": {"file_path": str(path), "old_string": "n // k",
                                                        "new_string": "-(-n // k)"}}}


async def test_hard_rule_holds_with_local_agent_off(tmp_path, policy, monkeypatch):
    """Default proxy (fast trim, no local agent): an Edit reply goes to Claude."""
    backend = FakeBackend(_ollama("", [_edit_call("/work/repo/src/mod.py")]))
    app, up = _app(tmp_path, backend, None, monkeypatch)
    r = await _post(app, _body())
    assert r.status_code == 200 and len(up) == 1  # forwarded to Anthropic
    (row,) = ledger.read_rows(tmp_path / "calls.jsonl")
    assert (row["decision"], row["reason"]) == ("fallback", "edit_to_claude")
    assert "local_agent" not in row and "compaction" not in row


async def test_local_agent_serves_a_compacted_non_edit_step(tmp_path, policy, monkeypatch):
    backend = FakeBackend(_ollama("", [{"function": {"name": "Read",
                                                     "arguments": {"file_path": "/work/repo/src/pkg/p.py"}}}]))
    app, up = _app(tmp_path, backend, LocalAgentConfig(), monkeypatch)
    r = await _post(app, _body())
    assert r.status_code == 200 and not up  # served locally, Anthropic never called
    sent = backend.calls[0]
    assert compact.estimate_tokens(sent) <= 5000
    assert len(sent["tools"]) < len(TOOLS)
    (row,) = ledger.read_rows(tmp_path / "calls.jsonl")
    assert row["decision"] == "served" and row["served_blocks"] == ["tool_use:Read"]
    assert row["local_agent"]["route"] == "local_loop" and row["local_agent"]["reply"]["reason"] == "non_edit_reply"
    assert row["compaction"]["est_tokens_out"] <= 5000 and row["compaction"]["retrieval"]["method"] == "embed"


async def test_local_agent_capability_decline_is_logged(tmp_path, monkeypatch):
    async def _choose(text, pinned):
        return {"task_type": "generate", "complexity": "moderate", "chain_head": [], "model": "ollama/fake:1"}
    monkeypatch.setattr(ps, "choose_model", _choose)
    backend = FakeBackend(_ollama("Done."))
    app, up = _app(tmp_path, backend, LocalAgentConfig(), monkeypatch)
    await _post(app, _body())
    assert not backend.calls and len(up) == 1
    (row,) = ledger.read_rows(tmp_path / "calls.jsonl")
    assert (row["decision"], row["reason"]) == ("forwarded", "task_type_unsuitable")
    assert row["local_agent"] == {"route": "claude", "reason": "task_type_unsuitable",
                                  "detail": "task_type=generate"}


async def test_edit_shaped_step_is_served_through_the_edit_protocol(tmp_path, policy, monkeypatch):
    repo = _git_repo(tmp_path)
    target = os.path.realpath(repo / "src" / "mod.py")
    edit_json = json.dumps([{"file": target, "old_string": "return n // k", "new_string": "return -(-n // k)"}])
    backend = FakeBackend(_ollama("", [_edit_call(target)]), edit_reply=edit_json)
    app, up = _app(tmp_path, backend, LocalAgentConfig(), monkeypatch)
    r = await _post(app, _read_then_test_body(repo))
    assert r.status_code == 200 and not up
    assert '"name": "Edit"' in r.text and "return -(-n // k)" in r.text
    assert len(backend.edit_prompts) == 1 and "def total(n, k)" in backend.edit_prompts[0]  # file from disk
    (row,) = ledger.read_rows(tmp_path / "calls.jsonl")
    assert row["decision"] == "served" and row["served_via"] == "edit_protocol"
    assert row["local_agent"]["reply"] == {"route": "edit_protocol", "reason": "edit_shaped", "detail": "Edit"}
    assert row["edit_protocol"]["attempts"] == 1


async def test_failed_edit_protocol_falls_back_to_claude(tmp_path, policy, monkeypatch):
    repo = _git_repo(tmp_path)
    target = os.path.realpath(repo / "src" / "mod.py")
    backend = FakeBackend(_ollama("", [_edit_call(target)]), edit_reply="I cannot do that")
    app, up = _app(tmp_path, backend, LocalAgentConfig(), monkeypatch)
    await _post(app, _read_then_test_body(repo))
    assert len(up) == 1
    (row,) = ledger.read_rows(tmp_path / "calls.jsonl")
    assert (row["decision"], row["reason"]) == ("fallback", "edit_protocol_failed")


def test_config_from_env(monkeypatch):
    from llm_router import local_agent

    monkeypatch.delenv("LLM_ROUTER_LOCAL_AGENT", raising=False)
    assert local_agent.enabled_from_env() is False  # off by default
    assert ps.ProxyConfig.from_env().local_agent is None
    monkeypatch.setenv("LLM_ROUTER_LOCAL_AGENT", "on")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_AGENT_TOP_K", "4")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_AGENT_PROMPT_BUDGET", "bogus")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_AGENT_EDIT", "claude")
    cfg = ps.ProxyConfig.from_env().local_agent
    assert (cfg.top_k, cfg.max_prompt_tokens, cfg.edit_mode) == (4, 5000, "claude")


async def test_a_call_to_a_request_tool_outside_the_offered_subset_is_translated_back(tmp_path):
    out, _ = await compact.compact(_body(), _retriever(FakeEmbed(), tmp_path), LocalAgentConfig(top_k=1))
    offered = {t["name"] for t in out["tools"]}
    assert "Read" not in offered
    m, err = from_ollama(_ollama("", [{"function": {"name": "Read", "arguments": {"file_path": "/x"}}}]), out)
    assert err is None and m["content"][0]["name"] == "Read"
    _, err = from_ollama(_ollama("", [{"function": {"name": "NotAClientTool", "arguments": {}}}]), out)
    assert err == "unknown tool 'NotAClientTool'"
    from llm_router.proxy.translate import VALIDATE_TOOLS_KEY, to_ollama
    assert VALIDATE_TOOLS_KEY not in to_ollama(out, "m", num_ctx=8192)


def test_text_plus_edit_reply_is_still_edit_shaped():
    msg = {"content": [{"type": "text", "text": "Fixing it now."},
                       {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x", "old_string": "a",
                                                                       "new_string": "b"}}]}
    d = capability.check_reply(msg, _body(), edit_mode="claude")
    assert (d.route, d.reason) == (capability.ROUTE_CLAUDE, "edit_to_claude")
