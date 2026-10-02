"""A follow-up prompt carries the previous turn into retrieval and local context.

"do it", "fix that", "what about the npm?" name nothing, so lexical retrieval
returns nothing for them, while what they point at is the last exchange. 15 of 40
(37.5%) of the owner's real prompts are of this kind (semantic_audit REPORT.md,
item 3). These tests pin the contract, behaviourally:

* a follow-up retrieves with the prior user turn and the assistant's last reply;
* a prompt that stands alone (no deixis, or its own path/identifier) is untouched;
* only THIS session's log is read (never another session's);
* the prior-turn text is bounded;
* every failure leaves the prompt exactly as it would have been without it;
* the MCP `llm()` door uses the session for retrieval only (no session text in
  the prompt), and the proxy's local tier gets the small prior-turn block.
"""
from __future__ import annotations

import copy
import json
import textwrap
from pathlib import Path

import pytest

from llm_router import context_injection as ci
from llm_router import okf, session_store
from llm_router.context_signal import is_reply_shaped

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
DOC_TITLE = "billing/invoice_reconciler.py"
MARK = "ZEBRA-4471"  # appears only in prior-turn text, never in the knowledge doc


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_OKF", "on")
    for var in ("LLM_ROUTER_CONTEXT_INJECTION", "LLM_ROUTER_PROJECT_ROOT",
                "LLM_ROUTER_PROJECT_DIR", "LLM_ROUTER_PROJECT_ID",
                "LLM_ROUTER_SEMANTIC_ARM", "LLM_ROUTER_SESSION_CONTEXT",
                "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    r = tmp_path / "repo"
    r.mkdir()
    (r / ".git").mkdir()
    store = okf._knowledge_dir() / "projects" / okf.project_slug(r.resolve())
    store.mkdir(parents=True)
    (store / "invoice.md").write_text(DOC, encoding="utf-8")
    okf.invalidate_cache()
    monkeypatch.chdir(r)  # the hook's situation: cwd is the project
    yield r
    okf.invalidate_cache()


def _say(sid, user=None, reply=None, *, reply_kind="claude_answer"):
    if user:
        session_store.record_event(sid, "user_prompt", user, role="user")
    if reply:
        session_store.record_event(sid, reply_kind, reply, role="assistant")


# ── the gate ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("prompt", [
    "do it", "fix that", "what about the npm?", "the third option...", "keep going",
])
def test_bare_follow_ups_are_followups(prompt):
    assert is_reply_shaped(prompt) and ci.is_followup(prompt)


@pytest.mark.parametrize("prompt", [
    "what is the capital of Portugal?",
    "write a function that reverses a string",
    "fix it in billing/invoice_reconciler.py",       # carries its own path
    "why does reconcile_invoice raise InvoiceMismatch, fix it",  # own identifiers
    "tell me about WebSocketServer",                  # CamelCase symbol
])
def test_standalone_prompts_are_not_followups(prompt):
    assert not ci.is_followup(prompt)


@pytest.mark.parametrize("prompt", [
    "Next, implement pagination for the orders API",
    "Yes please add retry logic with backoff",
    "Same pattern for the billing service",
    "Resume the deployment after the freeze window",
    "Proceed with the migration tonight",
    "Both approaches have tradeoffs worth noting",
    "The first step is to install the dependencies",
])
def test_ordinary_standalone_asks_that_open_with_a_discourse_word_are_not_followups(prompt):
    assert not ci.is_followup(prompt)


def test_the_opener_rule_only_applies_to_short_prompts():
    long_standalone = ("the second paragraph of the essay on Roman aqueducts should "
                       "explain how gradients were surveyed over long distances")
    assert not is_reply_shaped(long_standalone)


# ── follow-up uses the prior turn ─────────────────────────────────────────────


def test_a_follow_up_retrieves_with_the_prior_user_turn(repo):
    sid = "sess-a"
    _say(sid, user="reconcile_invoice raises InvoiceMismatch on ledger_delta drift")
    assert DOC_TITLE not in ci.inject("do it", root=str(repo))                 # premise
    out = ci.inject("do it", root=str(repo), session_id=sid)
    assert DOC_TITLE in out
    assert out.rstrip().endswith("do it"), "the prompt itself must not change"


def test_a_follow_up_retrieves_with_the_assistants_last_reply(repo):
    """The hook stores Claude's answers as kind 'claude_answer', role 'assistant'."""
    sid = "sess-b"
    _say(sid, user="please look at the billing module",
         reply="The mismatch is raised in reconcile_invoice when ledger_delta drifts.")
    assert DOC_TITLE in ci.inject("fix it", root=str(repo), session_id=sid)


def test_the_assistants_reply_is_recognised_by_role_not_kind(repo):
    sid = "sess-c"
    _say(sid, reply="see reconcile_invoice and InvoiceMismatch", reply_kind="routed_qa")
    assert "reconcile_invoice" in (ci._prior_turn_text(sid, "do it") or "")


def test_the_prior_turn_is_not_the_current_prompt(repo):
    """The hook records the current prompt before the model runs; it must not be
    mistaken for the previous turn."""
    sid = "sess-d"
    _say(sid, user="reconcile_invoice raises InvoiceMismatch")
    _say(sid, user="do it")
    prior = ci._prior_turn_text(sid, "do it")
    assert prior == "reconcile_invoice raises InvoiceMismatch"


# ── a standalone prompt is unchanged ──────────────────────────────────────────


def test_a_standalone_prompt_is_not_widened(repo):
    sid = "sess-e"
    _say(sid, user="reconcile_invoice raises InvoiceMismatch on ledger_delta drift")
    for prompt in ("what is the capital of Portugal?",
                   "write a function that reverses a string"):
        assert ci._retrieval_query(prompt, sid, None) == prompt
        assert DOC_TITLE not in ci.inject(prompt, root=str(repo), session_id=sid)
        assert ci.inject(prompt, root=str(repo), retrieval_sid=sid) == \
            ci.inject(prompt, root=str(repo))


def test_a_prompt_with_its_own_anchor_keeps_its_exact_query(repo):
    sid = "sess-f"
    _say(sid, user="reconcile_invoice raises InvoiceMismatch")
    own = "fix it in some/other_file.py"
    assert ci._retrieval_query(own, sid, None) == own


def test_no_session_means_no_change(repo):
    assert ci._retrieval_query("do it", None, None) == "do it"
    assert ci._prior_turn_text(None, "do it") is None


def test_the_switch_still_turns_everything_off(repo, monkeypatch):
    sid = "sess-g"
    _say(sid, user="reconcile_invoice raises InvoiceMismatch")
    monkeypatch.setenv("LLM_ROUTER_CONTEXT_INJECTION", "off")
    assert ci.inject("do it", root=str(repo), session_id=sid) == "do it"


# ── cross-session isolation ───────────────────────────────────────────────────


def test_another_sessions_turns_are_never_read(repo):
    _say("sess-other", user="reconcile_invoice raises InvoiceMismatch",
         reply=f"{MARK} reconcile_invoice")
    _say("sess-mine", user="hello there")
    assert ci._prior_turn_text("sess-mine", "do it") == "hello there"
    out = ci.inject("do it", root=str(repo), session_id="sess-mine")
    assert DOC_TITLE not in out and MARK not in out


def test_a_session_in_another_project_is_not_reachable(repo, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    (other / ".git").mkdir()
    monkeypatch.chdir(other)
    _say("sess-x", user="reconcile_invoice raises InvoiceMismatch")
    monkeypatch.chdir(repo)
    assert ci._prior_turn_text("sess-x", "do it") is None


def test_the_session_seed_reads_the_callers_project_not_the_cwd(repo, tmp_path, monkeypatch):
    """The MCP server's cwd is $HOME: the I3b seed must read the bucket of the
    root it was given, or it silently sees an empty log."""
    sid = "sess-mcp"
    session_store.record_event(
        sid, "tool_call", 'Edit({"file_path": "billing/invoice_reconciler.py"})',
        role="tool", tool="Edit")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert ci._session_seed_query("keep going", sid) is None
    assert "invoice_reconciler" in ci._session_seed_query("keep going", sid, str(repo.resolve()))


# ── bounded size ──────────────────────────────────────────────────────────────


def test_the_prior_turn_text_is_capped(repo):
    sid = "sess-h"
    _say(sid, user="reconcile_invoice " + "x" * 20_000, reply="y" * 20_000)
    prior = ci._prior_turn_text(sid, "do it")
    assert prior is not None
    user, reply = prior.split("\n")
    assert len(user) == len(reply) == ci._PRIOR_TURN_CHARS
    q = ci._retrieval_query("do it", sid, None)
    assert len(q) <= len("do it") + 1 + 2 * ci._PRIOR_TURN_CHARS + 1


def test_only_the_last_exchange_is_used(repo):
    sid = "sess-i"
    _say(sid, user="OLDEST first ask", reply="OLDEST first answer")
    _say(sid, user="newer ask", reply="newer answer")
    prior = ci._prior_turn_text(sid, "do it")
    assert prior == "newer ask\nnewer answer"


# ── fail-open ─────────────────────────────────────────────────────────────────


def test_an_unreadable_session_store_changes_nothing(repo, monkeypatch):
    sid = "sess-j"
    _say(sid, user="reconcile_invoice raises InvoiceMismatch")
    baseline = ci.inject("do it", root=str(repo))

    def boom(*a, **k):
        raise OSError("store exploded")

    monkeypatch.setattr(session_store, "load_events", boom)
    assert ci._prior_turn_text(sid, "do it") is None
    assert ci.inject("do it", root=str(repo), session_id=sid) == baseline


def test_a_broken_gate_changes_nothing(repo, monkeypatch):
    sid = "sess-k"
    _say(sid, user="reconcile_invoice raises InvoiceMismatch")

    def boom(_p):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr("llm_router.context_signal.is_reply_shaped", boom)
    assert ci.is_followup("do it") is False
    assert ci._retrieval_query("do it", sid, None) == "do it"


# ── the session block respects the privacy target ─────────────────────────────


def test_retrieval_sid_widens_retrieval_but_adds_no_session_text(repo):
    """The MCP `llm()` door does not know its provider yet, so the session may
    steer retrieval only; nothing the user or the assistant said enters the prompt."""
    sid = "sess-l"
    _say(sid, user=f"{MARK} reconcile_invoice raises InvoiceMismatch")
    out = ci.inject("do it", root=str(repo), retrieval_sid=sid)
    assert DOC_TITLE in out
    assert MARK not in out and session_store.SENTINEL_OPEN not in out


def test_a_local_target_gets_the_prior_turn_block_and_an_external_one_does_not(repo, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    sid = "sess-m"
    _say(sid, user=f"{MARK} reconcile_invoice raises InvoiceMismatch")
    local = ci.inject("do it", root=str(repo), session_id=sid, target_provider="local")
    external = ci.inject("do it", root=str(repo), session_id=sid, target_provider="openai")
    assert MARK in local and session_store.SENTINEL_OPEN in local
    assert MARK not in external


# ── router wiring (the MCP llm() door) ────────────────────────────────────────


class _Stop(BaseException):
    pass


async def test_the_router_hands_inject_the_session_for_retrieval_only(repo, monkeypatch):
    from llm_router import router
    from llm_router.types import TaskType

    seen: dict = {}

    def record(prompt, **kw):
        seen.update(kw, prompt=prompt)
        return prompt

    async def stop(*a, **k):
        raise _Stop()

    monkeypatch.setattr(ci, "inject", record)
    monkeypatch.setattr(router, "_dispatch_model_loop", stop)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-router")
    with pytest.raises(_Stop):
        await router.route_and_call(TaskType.QUERY, "do it", project_root=str(repo),
                                    model_override="ollama/qwen3:8b")
    assert seen["retrieval_sid"] == "sess-router"
    assert seen["session_root"] == str(repo.resolve())
    assert "session_id" not in seen, "no session text may reach a model whose provider is unknown"


# ── proxy local tier ──────────────────────────────────────────────────────────


FIXTURE = Path(__file__).parent / "fixtures" / "proxy" / "continuation_request.json"


def _proxy_request(cwd, ask, sid):
    body = json.loads(FIXTURE.read_text().replace("/work/repo", str(cwd)))
    body["messages"][0]["content"] = [{"type": "text", "text": ask}]
    body["metadata"] = {"user_id": json.dumps({"session_id": sid})}
    return body


def test_the_proxy_local_tier_gets_the_prior_turn_for_a_follow_up(repo, monkeypatch):
    from llm_router.proxy import okf_context
    from llm_router.proxy.backends import apply_trims, resolve_trims

    sid = "sess-proxy"
    _say(sid, user=f"{MARK} reconcile_invoice raises InvoiceMismatch")
    body = _proxy_request(repo, "do it", sid)
    before = copy.deepcopy(body)
    send = apply_trims(body, resolve_trims("fast"))
    out, info = okf_context.attach(send, body)
    assert info["injected"] is True
    assert DOC_TITLE in out["system"] and MARK in out["system"]
    assert body == before
    assert info["tokens"] <= okf_context.MAX_BLOCK_TOKENS


def test_the_proxy_does_not_widen_a_standalone_ask(repo):
    from llm_router.proxy import okf_context
    from llm_router.proxy.backends import apply_trims, resolve_trims

    sid = "sess-proxy2"
    _say(sid, user=f"{MARK} reconcile_invoice raises InvoiceMismatch")
    body = _proxy_request(repo, "what is the capital of Portugal?", sid)
    send = apply_trims(body, resolve_trims("fast"))
    out, info = okf_context.attach(send, body)
    assert out is send and info["injected"] is False


def test_the_proxy_never_reads_another_sessions_log(repo):
    from llm_router.proxy import okf_context
    from llm_router.proxy.backends import apply_trims, resolve_trims

    _say("sess-theirs", user=f"{MARK} reconcile_invoice raises InvoiceMismatch")
    body = _proxy_request(repo, "do it", "sess-mine-empty")
    send = apply_trims(body, resolve_trims("fast"))
    out, info = okf_context.attach(send, body)
    assert info["injected"] is False and out is send


def test_the_proxy_session_block_is_budgeted(repo):
    from llm_router.proxy import okf_context
    from llm_router.proxy.backends import apply_trims, resolve_trims

    sid = "sess-proxy3"
    _say(sid, user="reconcile_invoice " + "word " * 5000, reply="reply " * 5000)
    body = _proxy_request(repo, "do it", sid)
    send = apply_trims(body, resolve_trims("fast"))
    out, info = okf_context.attach(send, body)
    assert info["injected"] is True and info["tokens"] <= info["room_tokens"]


def test_a_failing_session_lookup_still_serves_the_proxy_step(repo, monkeypatch):
    from llm_router.proxy import okf_context
    from llm_router.proxy.backends import apply_trims, resolve_trims

    sid = "sess-proxy4"
    _say(sid, user="reconcile_invoice raises InvoiceMismatch")
    body = _proxy_request(repo, "do it", sid)
    send = apply_trims(body, resolve_trims("fast"))

    def boom(*a, **k):
        raise OSError("store exploded")

    monkeypatch.setattr(session_store, "load_events", boom)
    out, info = okf_context.attach(send, body)
    assert info["reason"] in (okf_context.REASON_NO_MATCH, okf_context.REASON_INJECTED)
