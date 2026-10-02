"""The proxy's local tier gets repo knowledge (llm_router.proxy.okf_context).

Diagnosis: ``~/.rsi/research/routing-experiment-2026-10-01/okf_context_diagnosis.md``
section 2 — the proxy's local tier sent the Ollama backend a trimmed Claude
request with no knowledge attached, one of three local-serving paths that ran
blind. These tests pin the contract the module's docstring states:

* injected for a project that has indexed docs, into the local request only;
* nothing for an unknown cwd, an out-of-repo cwd, an un-indexed project, or a
  prompt the docs do not bear on;
* the block respects the ~5k-token prompt budget and its own cap;
* every failure leaves the request exactly as it was.

The end-to-end tests drive the real server with a fake backend and a mocked
Anthropic upstream, like tests/test_proxy.py.
"""
from __future__ import annotations

import copy
import json
import textwrap
from pathlib import Path

import httpx
import pytest

from llm_router import okf
from llm_router.proxy import okf_context
from llm_router.proxy import server as ps
from llm_router.proxy.backends import CONDENSED_SYSTEM, apply_trims, resolve_trims
from llm_router.proxy.steps import tier_text
from llm_router.proxy.translate import from_ollama

FIXTURE = Path(__file__).parent / "fixtures" / "proxy" / "continuation_request.json"
ASK = "reconcile_invoice raises InvoiceMismatch on ledger_delta drift, please fix it"
DOC = textwrap.dedent("""\
    ---
    type: SourceFile
    title: billing/invoice_reconciler.py
    description: Reconciles invoice line items against ledger entries.
    tags: [billing, invoice, reconciler]
    key_symbols: [reconcile_invoice, ledger_delta, InvoiceMismatch]
    ---

    Defines: reconcile_invoice, ledger_delta, InvoiceMismatch.
    """)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(h))
    monkeypatch.delenv("LLM_ROUTER_CONTEXT_INJECTION", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_DIR", raising=False)
    okf.invalidate_cache()
    yield h
    okf.invalidate_cache()


def _repo(tmp_path: Path, name: str, *, git: bool = True, indexed: bool = True) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    if git:
        (repo / ".git").mkdir()
    if indexed:
        store = okf._knowledge_dir() / "projects" / okf.project_slug(repo.resolve())
        store.mkdir(parents=True)
        (store / "invoice.md").write_text(DOC, encoding="utf-8")
    okf.invalidate_cache()
    return repo


def _request(cwd: str | Path, ask: str = ASK, *, system_as_list: bool = False) -> dict:
    """The proxy fixture with its working directory and first ask replaced."""
    body = json.loads(FIXTURE.read_text().replace("/work/repo", str(cwd)))
    body["messages"][0]["content"] = [{"type": "text", "text": ask}]
    if system_as_list:
        body["system"] = [{"type": "text", "text": "S1"}, {"type": "text", "text": "S2"}]
    return body


def _send(body: dict) -> dict:
    return apply_trims(body, resolve_trims("fast"))


# ── unit: attach() ────────────────────────────────────────────────────────────


def test_injected_for_an_indexed_project(home, tmp_path):
    repo = _repo(tmp_path, "repo-a")
    body = _request(repo)
    before = copy.deepcopy(body)
    send = _send(body)

    out, info = okf_context.attach(send, body)

    assert info["injected"] is True and info["reason"] == okf_context.REASON_INJECTED
    assert "<knowledge_context>" in out["system"]
    assert "billing/invoice_reconciler.py" in out["system"]
    assert out["system"].endswith(CONDENSED_SYSTEM)  # the trimmed system prompt is kept
    assert body == before, "the client's own request must never be mutated"
    assert send["system"] == CONDENSED_SYSTEM, "the trimmed request must not be mutated either"
    assert {k: v for k, v in out.items() if k != "system"} == {k: v for k, v in send.items() if k != "system"}


def test_block_is_a_first_system_text_block_when_system_is_a_list(home, tmp_path):
    repo = _repo(tmp_path, "repo-a")
    body = _request(repo, system_as_list=True)
    out, info = okf_context.attach(body, body)  # no trim: the system stays a list
    assert info["injected"] is True
    assert out["system"][0]["type"] == "text" and "<knowledge_context>" in out["system"][0]["text"]
    assert out["system"][1:] == body["system"]


def test_query_is_the_human_ask_not_the_system_reminders(home, tmp_path):
    """Retrieval must be driven by what the user asked, never by the 30 KB
    preamble: a system-reminder naming an indexed file must not retrieve it."""
    repo = _repo(tmp_path, "repo-a")
    body = _request(repo, ask="please tidy the readme")
    body["messages"][1]["content"] += "\nreconcile_invoice InvoiceMismatch ledger_delta"
    assert "reconcile_invoice" not in tier_text(body)
    out, info = okf_context.attach(_send(body), body)
    assert info["injected"] is False and info["reason"] == okf_context.REASON_NO_MATCH
    assert out["system"] == CONDENSED_SYSTEM


def test_cwd_unknown_injects_nothing(home, tmp_path):
    _repo(tmp_path, "repo-a")
    body = _request("/definitely/not/a/dir")
    send = _send(body)
    out, info = okf_context.attach(send, body)
    assert out is send and info["reason"] == okf_context.REASON_NO_SCOPE


def test_request_with_no_cwd_at_all_injects_nothing(home, tmp_path, monkeypatch):
    _repo(tmp_path, "repo-a")
    body = _request(tmp_path / "repo-a")
    for m in body["messages"]:
        if m["role"] == "system" and isinstance(m["content"], str):
            m["content"] = m["content"].replace("Primary working directory", "Something else")
    monkeypatch.setattr("llm_router.session_store.read_session_cwd", lambda sid: None)
    send = _send(body)
    out, info = okf_context.attach(send, body)
    assert out is send and info["reason"] == okf_context.REASON_NO_SCOPE


def test_cwd_from_the_session_pointer_when_the_request_has_none(home, tmp_path, monkeypatch):
    repo = _repo(tmp_path, "repo-a")
    body = _request(repo)
    for m in body["messages"]:
        if m["role"] == "system" and isinstance(m["content"], str):
            m["content"] = m["content"].replace("Primary working directory", "Something else")
    monkeypatch.setattr("llm_router.session_store.read_session_cwd", lambda sid: str(repo))
    _out, info = okf_context.attach(_send(body), body)
    assert info["injected"] is True


def test_out_of_repo_cwd_injects_nothing_even_if_docs_exist_for_it(home, tmp_path):
    """A directory that is not inside a git repo is not a project. Docs keyed to
    it (the $HOME bucket in the field) must not be offered to a prompt."""
    plain = _repo(tmp_path, "not-a-repo", git=False, indexed=True)
    body = _request(plain)
    send = _send(body)
    out, info = okf_context.attach(send, body)
    assert out is send and info["reason"] == okf_context.REASON_NO_SCOPE


def test_another_projects_docs_are_never_injected(home, tmp_path):
    _repo(tmp_path, "repo-a", indexed=True)
    b = _repo(tmp_path, "repo-b", indexed=False)
    body = _request(b)
    send = _send(body)
    out, info = okf_context.attach(send, body)
    assert out is send and info["injected"] is False
    assert info["reason"] == okf_context.REASON_NO_MATCH


def test_unrelated_prompt_in_an_indexed_project_injects_nothing(home, tmp_path):
    repo = _repo(tmp_path, "repo-a")
    body = _request(repo, ask="what is the capital of Portugal?")
    send = _send(body)
    out, info = okf_context.attach(send, body)
    assert out is send and info["reason"] == okf_context.REASON_NO_MATCH


def test_switch_disables_it(home, tmp_path, monkeypatch):
    repo = _repo(tmp_path, "repo-a")
    monkeypatch.setenv("LLM_ROUTER_CONTEXT_INJECTION", "off")
    body = _request(repo)
    send = _send(body)
    out, info = okf_context.attach(send, body)
    assert out is send and info["reason"] == okf_context.REASON_DISABLED


# ── unit: budget ──────────────────────────────────────────────────────────────


def _fake_inject(monkeypatch, sizes_by_limit: dict[int, int]):
    """Make the choke point return a knowledge block of a chosen size per limit."""
    calls: list[int] = []

    def fake(system, objective, *, root=None, session_id=None, limit=3):
        calls.append(limit)
        n = sizes_by_limit[limit]
        return "<knowledge_context>\n" + "x" * (n * 4) + "\n</knowledge_context>" if n else None

    monkeypatch.setattr("llm_router.context_injection.inject_system_prompt", fake)
    return calls


def test_block_over_budget_is_retried_with_fewer_documents(home, tmp_path, monkeypatch):
    repo = _repo(tmp_path, "repo-a")
    body = _request(repo)
    send = _send(body)
    room_ceiling = 1000 + okf_context.estimate_tokens(send)  # room = 1000 tokens
    calls = _fake_inject(monkeypatch, {3: 1400, 2: 1100, 1: 600})
    out, info = okf_context.attach(send, body, ceiling_tokens=room_ceiling)
    assert calls == [3, 2, 1]
    assert info["injected"] is True and info["tokens"] <= 1000
    assert out["system"].count("<knowledge_context>") == 1


def test_block_that_never_fits_is_dropped_not_truncated(home, tmp_path, monkeypatch):
    repo = _repo(tmp_path, "repo-a")
    body = _request(repo)
    send = _send(body)
    room_ceiling = 500 + okf_context.estimate_tokens(send)
    _fake_inject(monkeypatch, {3: 900, 2: 800, 1: 700})
    out, info = okf_context.attach(send, body, ceiling_tokens=room_ceiling)
    assert out is send
    assert info["injected"] is False and info["reason"] == okf_context.REASON_OVER_BUDGET


def test_block_is_capped_even_when_the_request_has_room(home, tmp_path, monkeypatch):
    repo = _repo(tmp_path, "repo-a")
    body = _request(repo)
    send = _send(body)
    _fake_inject(monkeypatch, {3: okf_context.MAX_BLOCK_TOKENS + 200, 2: okf_context.MAX_BLOCK_TOKENS + 100,
                               1: okf_context.MAX_BLOCK_TOKENS + 1})
    out, info = okf_context.attach(send, body, ceiling_tokens=1_000_000)
    assert out is send and info["reason"] == okf_context.REASON_OVER_BUDGET


def test_a_request_already_at_the_ceiling_gets_nothing(home, tmp_path, monkeypatch):
    repo = _repo(tmp_path, "repo-a")
    body = _request(repo)
    send = _send(body)
    calls = _fake_inject(monkeypatch, {3: 10, 2: 10, 1: 10})
    out, info = okf_context.attach(send, body, ceiling_tokens=okf_context.estimate_tokens(send) + 50)
    assert out is send and info["reason"] == okf_context.REASON_NO_ROOM
    assert calls == [], "no retrieval is spent when there is no room for its result"


def test_real_block_stays_inside_the_default_budget(home, tmp_path):
    repo = _repo(tmp_path, "repo-a")
    body = _request(repo)
    send = _send(body)
    out, info = okf_context.attach(send, body)
    assert info["injected"] is True
    total = okf_context.estimate_tokens(out) + okf_context._tokens(out["system"]) - okf_context._tokens(CONDENSED_SYSTEM)
    assert total <= okf_context.DEFAULT_MAX_PROMPT_TOKENS


# ── unit: fail-open ───────────────────────────────────────────────────────────


def test_retrieval_failure_returns_the_request_unchanged(home, tmp_path, monkeypatch):
    repo = _repo(tmp_path, "repo-a")
    body = _request(repo)
    send = _send(body)

    def boom(*a, **k):
        raise RuntimeError("store exploded")

    monkeypatch.setattr("llm_router.context_injection.inject_system_prompt", boom)
    out, info = okf_context.attach(send, body)
    assert out is send and info["reason"] == okf_context.REASON_ERROR and info["injected"] is False


def test_scope_failure_returns_the_request_unchanged(home, tmp_path, monkeypatch):
    repo = _repo(tmp_path, "repo-a")
    body = _request(repo)
    send = _send(body)

    def boom(_body):
        raise OSError("no cwd for you")

    monkeypatch.setattr(okf_context, "session_cwd", boom)
    out, info = okf_context.attach(send, body)
    assert out is send and info["reason"] == okf_context.REASON_ERROR


# ── end to end through the server ─────────────────────────────────────────────


class _Upstream:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.dumps({"id": "msg_x", "type": "message", "role": "assistant",
                           "content": [{"type": "text", "text": "ok"}],
                           "stop_reason": "end_turn", "usage": {}}).encode()
        return httpx.Response(200, stream=httpx.ByteStream(body),
                              headers={"content-type": "application/json", "request-id": "req_x"})


class _Backend:
    def __init__(self):
        self.calls: list[dict] = []

    async def complete(self, body, timeout_s):
        self.calls.append(body)
        m, err = from_ollama({"message": {"role": "assistant", "content": "Done."}, "done_reason": "stop"}, body)
        return m, err, {"prompt_tokens": 10, "output_tokens": 2}


def _app(tmp_path, upstream, backend, **cfg):
    config = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=tmp_path / "proxy_calls.jsonl", **cfg)
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return ps.build_app(config, client=client, backend_factory=lambda model: backend)


async def _post(app, body):
    h = {"authorization": "Bearer sk-ant-oat01-" + "Zq9" * 20, "anthropic-version": "2023-06-01",
         "content-type": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as c:
        return await c.post("/v1/messages?beta=true", content=json.dumps(body), headers=h)


@pytest.fixture
def policy(monkeypatch):
    decision = {"task_type": "code", "complexity": "moderate",
                "chain_head": ["ollama/fake:1"], "model": "ollama/fake:1"}

    async def _choose(text, pinned):
        return dict(decision)

    monkeypatch.setattr(ps, "choose_model", _choose)
    return decision


async def test_served_step_reaches_the_local_backend_with_the_knowledge(home, tmp_path, policy):
    repo = _repo(tmp_path, "repo-a")
    backend, up = _Backend(), _Upstream()
    await _post(_app(tmp_path, up, backend), _request(repo))
    assert up.requests == [], "the step was served locally"
    system = backend.calls[0]["system"]
    assert "<knowledge_context>" in system and "billing/invoice_reconciler.py" in system
    assert system.endswith(CONDENSED_SYSTEM)
    row = json.loads((tmp_path / "proxy_calls.jsonl").read_text().splitlines()[-1])
    assert row["okf"]["injected"] is True and row["okf"]["tokens"] > 0


async def test_unknown_project_is_served_with_no_knowledge_and_the_reason_is_recorded(home, tmp_path, policy):
    repo = _repo(tmp_path, "repo-b", indexed=False)
    backend, up = _Backend(), _Upstream()
    await _post(_app(tmp_path, up, backend), _request(repo))
    assert backend.calls[0]["system"] == CONDENSED_SYSTEM
    row = json.loads((tmp_path / "proxy_calls.jsonl").read_text().splitlines()[-1])
    assert row["okf"]["injected"] is False and row["okf"]["reason"] == okf_context.REASON_NO_MATCH


async def test_a_broken_attach_still_serves_the_step(home, tmp_path, policy, monkeypatch):
    repo = _repo(tmp_path, "repo-a")

    def boom(*a, **k):
        raise RuntimeError("store exploded")

    monkeypatch.setattr("llm_router.context_injection.inject_system_prompt", boom)
    backend, up = _Backend(), _Upstream()
    r = await _post(_app(tmp_path, up, backend), _request(repo))
    assert r.status_code == 200 and up.requests == []
    assert backend.calls[0]["system"] == CONDENSED_SYSTEM


async def test_a_slow_attach_is_bounded_and_the_step_is_still_served(home, tmp_path, policy, monkeypatch):
    import time as _time

    from llm_router.proxy import server as proxy_server
    repo = _repo(tmp_path, "repo-a")
    monkeypatch.setattr(proxy_server, "OKF_ATTACH_TIMEOUT_S", 0.2)
    monkeypatch.setattr(okf_context, "attach", lambda *a, **k: (_time.sleep(1.0), (a[0], {}))[1])
    backend, up = _Backend(), _Upstream()
    t0 = _time.monotonic()
    r = await _post(_app(tmp_path, up, backend), _request(repo))
    assert r.status_code == 200 and up.requests == []
    assert backend.calls[0]["system"] == CONDENSED_SYSTEM
    assert _time.monotonic() - t0 < 0.9  # did not wait for the 1 s attach


async def test_pass_through_to_claude_is_never_touched(home, tmp_path, monkeypatch):
    """When the policy keeps the step on Claude the upstream gets the client's
    bytes: no knowledge block, no attach call at all."""
    repo = _repo(tmp_path, "repo-a")

    async def _keep(text, pinned):
        return {"task_type": "code", "complexity": "hard", "chain_head": [], "model": None}

    monkeypatch.setattr(ps, "choose_model", _keep)
    seen = []
    monkeypatch.setattr(okf_context, "attach", lambda *a, **k: seen.append(1) or (a[0], {}))
    backend, up = _Backend(), _Upstream()
    body = _request(repo)
    await _post(_app(tmp_path, up, backend), body)
    assert seen == [] and backend.calls == []
    assert json.loads(up.requests[0].content) == body
    assert b"knowledge_context" not in up.requests[0].content
