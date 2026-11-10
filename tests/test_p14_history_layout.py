"""P14: LLM_ROUTER_PROXY_LAYOUT (default off), the append-only history layout for local requests.

Behaviour only, no network. Wire assertions use the real OllamaBackend against a mocked Ollama transport and
compare the JSON the runner would receive. Time-free: every K is a turn count.
"""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from llm_router.local_context_guard import DEFAULT_RESERVED_OUTPUT_TOKENS
from llm_router.proxy import backends as pb
from llm_router.proxy import history_layout as hl
from llm_router.proxy import mr_summary
from llm_router.token_estimate import est_tokens
from tests.test_p9_proxy_mr import SUMMARY, MRBackend, _session, _warm
from tests.test_proxy_local_mode import _overrides_readable, _post, env  # noqa: F401  (fixtures)

MODEL = "ollama/fake:1"
WINDOW = 32_768
USABLE = WINDOW - DEFAULT_RESERVED_OUTPUT_TOKENS
TOOLS = [{"name": n, "description": n, "input_schema": {"type": "object", "properties": {}}}
         for n in ("Read", "Edit", "Write", "Bash", "Grep", "Glob")]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("LLM_ROUTER_PROXY_LAYOUT", "LLM_ROUTER_PROXY_LAYOUT_K"):
        monkeypatch.delenv(k, raising=False)
    hl.clear()
    yield
    hl.clear()


def _pair(i: int, chars: int = 400) -> list[dict]:
    return [{"role": "assistant", "content": [
                {"type": "tool_use", "id": f"toolu_{i}", "name": "Bash", "input": {"command": f"cat f{i}.py"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": (f"out {i} " * (chars // 5 + 1))[:chars]}]}]


def _body(pairs: int, chars: int = 400) -> dict:
    msgs = [{"role": "user", "content": "Fix the failing test in src/app.py"}]
    for i in range(pairs):
        msgs += _pair(i, chars)
    return {"model": "claude-x", "system": "client system", "tools": TOOLS, "messages": msgs}


def _fast(body: dict, window: int = WINDOW) -> dict:
    return pb.apply_trims(body, [pb._trim_fast], target_window=window)


def _wire(sent_out: list, **kw):
    def ollama(request: httpx.Request) -> httpx.Response:
        sent_out.append(json.loads(request.content))
        line = json.dumps({"message": {"role": "assistant", "content": "ok"}, "done": True, "done_reason": "stop",
                           "prompt_eval_count": 5, "eval_count": 1})
        return httpx.Response(200, content=(line + "\n").encode())

    client = httpx.AsyncClient(transport=httpx.MockTransport(ollama))
    return pb.OllamaBackend(MODEL, client, base_url="http://127.0.0.1:11999", num_ctx=WINDOW, num_predict=64, **kw)


def _send(body: dict, window: int = WINDOW) -> dict:
    """The /api/chat payload the real OllamaBackend sends for ``body`` after the fast trim."""
    sent: list = []
    backend = _wire(sent)
    trimmed = _fast(body, window)

    async def go():
        return await backend.complete(trimmed, 30)

    msg, err, _usage = asyncio.run(go())
    assert err is None, err
    return sent[-1]


def _k(msgs, **extra) -> str | None:
    return hl.conv_key(dict({"messages": msgs}, **extra))


def _arr(turns, key=None, window=WINDOW, fixed_est=500, reserved=0):
    return hl.arrange(turns, key=key or _k(turns), window=window, fixed_est=fixed_est, reserved=reserved)


P9_KEY = _k(_body(24, 2400)["messages"])  # what the client's own (unshortened) body keys to


def _lead(a: list, b: list) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


# -- flag off: byte-identical -----------------------------------------------------------------------------

def test_flag_off_never_calls_the_layout_and_payload_is_the_legacy_packing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("layout ran with the flag off")

    monkeypatch.setattr(hl, "arrange", boom)
    body = _body(40, 3000)
    out = _fast(body)
    legacy_sys, legacy_msgs = pb._pack_messages(
        pb.non_system(copy.deepcopy(body)["messages"]), WINDOW, {"system": pb.CONDENSED_SYSTEM, "tools": []})
    assert out["messages"] == legacy_msgs
    assert out["system"] == (legacy_sys + "\n\n" + pb.CONDENSED_SYSTEM if legacy_sys else pb.CONDENSED_SYSTEM)
    assert json.dumps(_send(body), sort_keys=True)  # the wire payload builds


def test_flag_off_wire_payload_bytes_do_not_depend_on_layout_state(monkeypatch):
    body = _body(40, 3000)
    off1 = json.dumps(_send(body), sort_keys=True)
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    _send(body)
    monkeypatch.delenv("LLM_ROUTER_PROXY_LAYOUT")
    assert json.dumps(_send(body), sort_keys=True) == off1


def test_flag_off_adds_no_ledger_field():
    from llm_router.proxy import server as ps
    row: dict = {}
    obs = {"sys": "x", "msgs": ["a"], "keep_alive": -1}
    ps._note_prefix(row, _body(2), {"prefix_obs": obs})
    assert "layout" not in row


# -- prefix stability over turns --------------------------------------------------------------------------

def test_early_messages_are_identical_for_k_turns_and_the_tail_only_appends(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    sent = [_send(_body(n, 3000))["messages"] for n in range(40, 40 + 4)]
    # the legacy sliding window would change message 1 every turn; the layout holds it
    base = sent[0]
    for later in sent[1:]:
        assert later[: len(base)] == base, "an early message changed between turns"
        assert len(later) > len(base) or later == base
    legacy = []
    monkeypatch.delenv("LLM_ROUTER_PROXY_LAYOUT")
    legacy = [_send(_body(n, 3000))["messages"] for n in range(40, 44)]
    assert _lead(legacy[0], legacy[1]) < len(legacy[0])  # non-vacuity: the old window really slides


def test_system_and_the_four_fixed_tools_are_constant(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    a, b = _send(_body(40, 3000)), _send(_body(42, 3000))
    assert a["messages"][0] == b["messages"][0]
    assert [t["function"]["name"] for t in a["tools"]] == ["Read", "Edit", "Write", "Bash"]
    assert a["tools"] == b["tools"]


# -- boundary movement ------------------------------------------------------------------------------------

def _run(monkeypatch, start: int, turns: int, chars: int, k: int | None = None):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    if k is not None:
        monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT_K", str(k))
    out = []
    for n in range(start, start + turns):
        body = _body(n, chars)
        msgs = _arr(pb.non_system(body["messages"]), fixed_est=500, reserved=DEFAULT_RESERVED_OUTPUT_TOKENS)
        out.append((msgs, hl.take_stats(_k(body["messages"]))))
    return out


def test_boundary_moves_only_at_k_turns_once_over_soft(monkeypatch):
    # each pair ~ 700 est; SOFT*USABLE ~ 12k: starts fitting, crosses soft around pair ~17
    runs = _run(monkeypatch, 4, 40, 2400, k=4)
    stats = [s for _, s in runs]
    assert stats[0]["reason"] == "first" and stats[0]["moved"]
    moves = [i for i, s in enumerate(stats) if s["moved"]]
    assert len(moves) >= 2, "the run never crossed the soft target; the test would be vacuous"
    for a, b in zip(moves, moves[1:]):
        assert b - a >= 4, f"boundary moved after {b - a} turns, K is 4"
    for i, s in enumerate(stats):
        if not s["moved"]:
            assert s["reason"] == "hold" and s["msgs_same_prev"] == len(runs[i - 1][0])  # pure append
    for i in moves[1:]:
        assert stats[i]["reason"] in ("k_turns", "over_hard")


def test_k_env_changes_the_cadence(monkeypatch):
    a = [s["moved"] for _, s in _run(monkeypatch, 4, 40, 2400, k=4)]
    hl.clear()
    b = [s["moved"] for _, s in _run(monkeypatch, 4, 40, 2400, k=8)]
    assert sum(a) > sum(b)


def test_a_move_cuts_the_tail_to_the_soft_target(monkeypatch):
    runs = _run(monkeypatch, 4, 40, 2400, k=4)
    moved = [(m, s) for m, s in runs if s["moved"] and s["reason"] == "k_turns"]
    assert moved
    for msgs, s in moved:
        assert s["tokens_est"] <= hl.SOFT * USABLE + 1500  # +fixed est; the newest unit may overshoot slightly


def test_hard_limit_forces_a_move_before_k(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT_K", "1000")
    seen = set()
    for n in range(4, 60):
        body = _body(n, 2400)
        _arr(pb.non_system(body["messages"]), fixed_est=500, reserved=DEFAULT_RESERVED_OUTPUT_TOKENS)
        seen.add(hl.take_stats(_k(body["messages"]))["reason"])
    assert "over_hard" in seen and "k_turns" not in seen


def test_client_rewriting_history_moves_the_boundary(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    turns = pb.non_system(_body(30, 2400)["messages"])
    _arr(turns, fixed_est=500, reserved=0)
    hl.take_stats(_k(turns))
    rewritten = copy.deepcopy(turns)
    for m in rewritten[3:]:  # message 1 stays: it is part of the conversation key
        if m["role"] == "assistant":
            m["content"][0]["input"]["command"] += " --changed"
    _arr(rewritten + _pair(99), fixed_est=500, reserved=0)
    assert hl.take_stats(_k(turns))["reason"] == "history_changed"


# -- composition with P9 summaries and P11 pins ------------------------------------------------------------

def _summary(i: int, n: int, a: int, b: int, pins: str = "") -> str:
    return mr_summary.fit_summary("## Goal and user constraints\nfix\n" + pins, i, n, a, b)


def _p9_body(total_pairs: int, closed_pairs: int, n_summaries: int, pin: str) -> list[dict]:
    """What P9 hands the trim: first message + summary blocks, then the history from the cut."""
    full = pb.non_system(_body(total_pairs, 2400)["messages"])
    first = copy.deepcopy(full[0])
    first["content"] = [{"type": "text", "text": first["content"]}] + [
        {"type": "text", "text": _summary(i, n_summaries, 1, 2 * closed_pairs, pin)} for i in range(1, n_summaries + 1)]
    return [first] + full[1 + 2 * closed_pairs:]


def test_p9_summaries_and_p11_pins_stay_in_the_frozen_head_in_order(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    pin = "\npinned:\n- path src/app.py\n- test test_it"
    t1 = _p9_body(24, 10, 1, pin)
    m1 = _arr(t1, key=P9_KEY, fixed_est=500, reserved=0)
    t2 = _p9_body(25, 10, 1, pin)
    m2 = _arr(t2, key=P9_KEY, fixed_est=500, reserved=0)
    head = m2[0]["content"]
    assert head[0]["text"].startswith("Fix the failing test")
    assert head[1]["text"].startswith("<chunk-summary") and "pinned:" in head[1]["text"] and "src/app.py" in head[1]["text"]
    assert m2[: len(m1)] == m1  # summaries + pins + tail so far are a byte-identical prefix
    st = hl.take_stats(P9_KEY)
    assert st["moved"] is False and st["msgs_same_prev"] == len(m1)


def test_a_new_closed_chunk_moves_the_boundary_at_once(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    m1 = _arr(_p9_body(24, 10, 1, ""), key=P9_KEY, fixed_est=500, reserved=0)
    hl.take_stats(P9_KEY)
    t2 = _p9_body(25, 14, 2, "")  # P9 closed one more chunk: two summaries, the cut moved forward
    m2 = _arr(t2, key=P9_KEY, fixed_est=500, reserved=0)
    st = hl.take_stats(P9_KEY)
    assert st["moved"] and st["reason"] == "chunk_closed"
    heads = [b["text"] for b in m2[0]["content"] if b["text"].startswith("<chunk-summary")]
    assert len(heads) == 2
    assert m2[0]["content"][0] == m1[0]["content"][0]  # the base of the first message is untouched


def test_conv_key_ignores_summary_blocks():
    full = _body(24, 2400)["messages"]
    shortened = copy.deepcopy(full)
    shortened[0]["content"] = [{"type": "text", "text": full[0]["content"]}, {"type": "text", "text": _summary(1, 1, 1, 8)}]
    assert _k(shortened) == _k(full)


# -- tool-call pairing -------------------------------------------------------------------------------------

def _ids(msgs):
    uses, results = [], []
    for m in msgs:
        for b in m["content"] if isinstance(m["content"], list) else []:
            if b.get("type") == "tool_use":
                uses.append(b["id"])
            if b.get("type") == "tool_result":
                results.append(b["tool_use_id"])
    return uses, results


def test_no_orphan_in_any_layout_and_the_wire_payload(monkeypatch):
    runs = _run(monkeypatch, 4, 40, 2400, k=4)
    assert any(s["moved"] and s["reason"] != "first" for _, s in runs), "no cut happened: vacuous"
    for msgs, _ in runs:
        uses, results = _ids(msgs)
        assert set(results) <= set(uses) and set(uses) == set(results)
        assert hl.paired(msgs)
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    wire = _send(_body(60, 2400))["messages"]
    calls = [c for m in wire for c in m.get("tool_calls") or []]
    tools = [m for m in wire if m["role"] == "tool"]
    assert calls and len(calls) == len(tools)


def test_unit_with_a_tool_pair_is_never_split_even_when_the_cut_falls_inside(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    turns = pb.non_system(_body(60, 2400)["messages"])
    msgs = _arr(turns, fixed_est=500, reserved=0)
    assert msgs[1]["role"] == "assistant"  # the tail starts at a tool_use, never at its result
    assert hl.paired(msgs)


def test_paired_rejects_orphans():
    p = _pair(1)
    assert hl.paired(p)
    assert not hl.paired([p[1]])                       # result without its use
    assert not hl.paired([p[0], {"role": "user", "content": "x"}])  # use without result, not last
    assert hl.paired([{"role": "user", "content": "x"}, p[0]])      # a use in the very last message is allowed


def test_malformed_input_falls_back_to_none(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    orphan = [{"role": "user", "content": "hi"}, _pair(1)[1], {"role": "user", "content": "go"}]
    assert _arr(orphan, fixed_est=0, reserved=0) is None
    assert _arr([{"role": "user", "content": "hi"}], fixed_est=0, reserved=0) is None
    # and the trim then keeps the legacy packing
    body = {"system": "s", "tools": TOOLS, "messages": orphan}
    assert _fast(body)["messages"]


# -- ledger counter ----------------------------------------------------------------------------------------

def test_ledger_counter_counts_leading_identical_messages_and_tokens(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    from llm_router.proxy import server as ps
    rows = []
    for n in (20, 21, 22):
        body = _body(n, 2400)
        _arr(pb.non_system(body["messages"]), key=hl.conv_key(body), fixed_est=500, reserved=0)
        row: dict = {}
        ps._note_prefix(row, body, {"prefix_obs": {"sys": "x", "msgs": ["a"], "keep_alive": -1}})
        rows.append(row["layout"])
    assert rows[0]["msgs_same_prev"] is None and rows[0]["tokens_same_prev"] is None
    assert rows[1]["msgs_same_prev"] == rows[0]["msgs"] and rows[1]["tokens_same_prev"] > 0
    assert rows[2]["msgs_same_prev"] == rows[1]["msgs"]
    for r in rows:
        assert set(r) == {"moved", "reason", "age", "held_before_move", "msgs", "tokens_est", "dropped", "msgs_same_prev", "tokens_same_prev"}
        assert all(isinstance(v, (int, bool, str, list, type(None))) for v in r.values())  # counts only, no text


def test_a_retry_of_the_same_request_does_not_age_the_boundary(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    turns = pb.non_system(_body(10, 400)["messages"])
    for _ in range(6):
        _arr(turns, fixed_est=0, reserved=0)
    assert hl.take_stats(_k(turns))["age"] <= 1


def test_state_is_lru_bounded(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    for i in range(hl.MAX_CONVERSATIONS + 10):
        turns = [{"role": "user", "content": f"task {i}"}] + _pair(0)
        _arr(turns, fixed_est=0, reserved=0)
    assert len(hl._STATE) == hl.MAX_CONVERSATIONS


# -- non-vacuity: mutate in-process and watch the guards fail --------------------------------------------

def test_mutation_sliding_boundary_breaks_prefix_stability(monkeypatch):
    """If the boundary slid every turn (K=1 and soft 0), the stability test above must fail."""
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT_K", "1")
    monkeypatch.setattr(hl, "SOFT", 0.0)
    sent = [_send(_body(n, 3000))["messages"] for n in range(40, 44)]
    assert any(later[: len(sent[0])] != sent[0] for later in sent[1:])


# -- end to end through the proxy (local-agent mode + P9): the wire payload and the ledger row -----------



async def _grow(env, backend, first: int, turns: int, result_chars: int = 8_000):  # noqa: F811
    app = env.app(backend)
    await _warm(app)
    lives_before = len(backend.lives)
    for pairs in range(first, first + turns):
        await _post(app, copy.deepcopy(_session(pairs=pairs, result_chars=result_chars)))
    return backend.lives[lives_before:]


async def test_proxy_flag_on_holds_the_boundary_and_spends_fewer_maps(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    backend = MRBackend()
    lives = await _grow(env, backend, 40, 6, 3_000)
    assert len(lives) == 6
    all_rows = [r["layout"] for r in env.rows() if r.get("layout")]
    assert all_rows[0]["reason"] == "under_cap"  # the warm-up turn: small, sent whole
    rows = all_rows[1:]
    assert len(rows) == 6
    assert rows[0]["moved"] and rows[0]["reason"] == "over_cap"
    assert [r["reason"] for r in rows].count("hold") >= 4
    held = [r for r in rows if not r["moved"]]
    assert held, "no turn was held: the test is vacuous"
    first_msgs = [b["messages"] for b in lives]
    assert all(hl.paired(m) for m in first_msgs)  # held or moved: no orphan tool_result / tool_use reaches the backend
    for prev, cur, r in zip(first_msgs, first_msgs[1:], rows[1:]):
        if not r["moved"]:
            assert cur[: len(prev)] == prev  # real backend input: pure append
            assert r["msgs_same_prev"] == len(prev)
    maps_on = len(backend.maps)
    # the same run without the layout re-maps every turn: strictly more map calls
    monkeypatch.delenv("LLM_ROUTER_PROXY_LAYOUT")
    env.ledger.unlink()
    off = MRBackend()
    await _grow(env, off, 40, 6, 3_000)
    assert len(off.maps) > maps_on
    assert not any(r.get("layout") for r in env.rows())


async def test_proxy_flag_on_wire_payload_through_a_real_ollama_backend(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    sent: list = []
    n = {"i": 0}
    summary = "\n".join(f"{h}\nnone-{i}" for i, h in enumerate(mr_summary.HEADINGS))

    def ollama(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        sent.append(payload)
        n["i"] += 1
        is_map = payload["messages"][0]["content"].startswith("You compress one slice")
        msg = ({"role": "assistant", "content": summary} if is_map else
               {"role": "assistant", "content": "", "tool_calls": [
                   {"function": {"name": "Bash", "arguments": {"command": f"ls {n['i']}"}}}]})
        line = json.dumps({"message": msg, "done": True, "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 5})
        return httpx.Response(200, content=(line + "\n").encode())

    backend = pb.OllamaBackend(MODEL, httpx.AsyncClient(transport=httpx.MockTransport(ollama)),
                               base_url="http://127.0.0.1:11999", num_ctx=32_768, num_predict=2048)
    app = env.app(backend)
    await _warm(app)
    live = []
    for pairs in range(40, 45):
        before = len(sent)
        await _post(app, copy.deepcopy(_session(pairs=pairs, result_chars=3_000)))
        lives = [p for p in sent[before:] if not p["messages"][0]["content"].startswith("You compress one slice")]
        assert len(lives) == 1
        live.append(lives[0])
    rows = [r["layout"] for r in env.rows() if r.get("layout")]
    for prev, cur, r in zip(live, live[1:], rows[1:]):
        if not r["moved"]:
            assert cur["messages"][: len(prev["messages"])] == prev["messages"]
            assert cur["tools"] == prev["tools"]
    assert any(not r["moved"] for r in rows[1:]), "no held turn: vacuous"
    first_text = json.dumps(live[0]["messages"][:3])
    assert "<chunk-summary" in first_text  # the P9 summaries ride in the frozen head of the real payload


async def test_mutation_no_hold_re_maps_and_moves_the_prefix_every_turn(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    monkeypatch.setattr(hl, "hold", lambda key, messages, fits: None)  # the mutation: never hold
    backend = MRBackend()
    lives = await _grow(env, backend, 40, 6, 3_000)
    rows = [r["layout"] for r in env.rows() if r.get("layout")][1:]
    assert [r["reason"] for r in rows].count("hold") == 0
    assert sum(r["moved"] for r in rows) >= 4
    firsts = [json.dumps(b["messages"][:3]) for b in lives]
    assert len(set(firsts)) > 1  # the early messages changed between turns


async def test_proxy_a_chunk_close_while_held_is_a_move_with_a_reason(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    backend = MRBackend()
    app = env.app(backend)
    await _warm(app)
    for pairs in (40, 41, 42):
        await _post(app, copy.deepcopy(_session(pairs=pairs, result_chars=3_000)))
    assert [r["layout"]["reason"] for r in env.rows() if r.get("layout")][-2:] == ["hold", "hold"]
    # a jump: the held layout cannot fit, P9 closes more chunks
    await _post(app, copy.deepcopy(_session(pairs=70, result_chars=3_000)))
    row = [r["layout"] for r in env.rows() if r.get("layout")][-1]
    assert row["moved"] and row["reason"] == "over_cap"


def test_hold_returns_none_without_state_or_boundary_and_when_it_does_not_fit():
    msgs = _body(60, 2400)["messages"]
    key = _k(msgs)
    assert hl.hold(key, msgs, lambda m: True) is None  # nothing frozen yet
    head = copy.deepcopy(msgs[0])
    cut = msgs[1 + 2 * 20:]
    hl.observe(key, [head] + cut, moved=True, reason="over_cap", freeze=True)
    grown = msgs + _pair(61)
    held = hl.hold(key, grown, lambda m: True)
    assert held is not None and held[: len([head] + cut)] == [head] + cut and len(held) == len(cut) + 3
    assert hl.hold(key, grown, lambda m: False) is None  # no longer fits: a move
    rewritten = copy.deepcopy(grown)
    rewritten[1 + 2 * 20]["content"][0]["input"]["command"] = "other"
    assert hl.hold(key, rewritten, lambda m: True) is None  # the boundary message is gone
    hl.observe(key, grown[:2], moved=False, reason="under_cap")  # freeze cleared when the whole history fits again
    assert hl.hold(key, grown, lambda m: True) is None


def test_move_goal_reserves_k_turns_of_observed_growth_and_never_goes_below_half_cap():
    msgs = _body(10, 400)["messages"]
    assert hl.move_goal(_k(msgs), 25_000) == 25_000 - 4 * hl.DEFAULT_GROWTH
    assert hl.move_goal(_k(msgs), 5_000) == int(5_000 * hl.SOFT)


async def test_p9_goal_is_a_target_not_a_requirement():
    """shorten(goal=deeper) accepts a result that reaches only the usual goal; goal=None is unchanged."""
    body = _session(pairs=20, result_chars=8_000)

    async def map_call(prompt, n):
        return SUMMARY

    def est(b):
        return est_tokens(b["messages"])

    cap = est(body) // 2
    plain = await mr_summary.shorten(copy.deepcopy(body), map_call, cap=cap, est=est)
    deep = await mr_summary.shorten(copy.deepcopy(body), map_call, cap=cap, est=est, goal=1)
    assert plain.changed and deep.changed and deep.row["tokens_after"] <= plain.row["tokens_after"] <= cap


# -- review fixes (#900): privacy, key, double write, dropped layers, size, guards ---------------------------

def test_session_context_off_makes_the_layout_step_aside(monkeypatch):
    """LLM_ROUTER_SESSION_CONTEXT=off: the old packing withholds history; the layout must send none of its own."""
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    body = _body(40, 3000)
    on = _fast(body)["messages"]
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "off")
    hl.clear()
    off = _fast(body)["messages"]
    monkeypatch.delenv("LLM_ROUTER_PROXY_LAYOUT")
    legacy = _fast(body)["messages"]
    assert off == legacy and off != on  # the layout stepped aside: exactly what the privacy-honouring path sends
    assert hl.take_stats(hl.conv_key(body)) is None


def test_keys_differ_for_system_tools_session_and_first_reply_and_none_without_a_human_message():
    base = _body(5)
    k = hl.conv_key(base)
    assert k and k == hl.conv_key(copy.deepcopy(base))
    assert hl.conv_key(dict(base, system="another system")) != k
    assert hl.conv_key(dict(base, tools=TOOLS[:2])) != k
    assert hl.conv_key(dict(base, metadata={"user_id": json.dumps({"session_id": "s-2"})})) != k
    other = copy.deepcopy(base)
    other["messages"][1]["content"][0]["input"]["command"] = "different"
    assert hl.conv_key(other) != k
    billing = dict(base, system="x-anthropic-billing-header: cch=abc\nsame")
    assert hl.conv_key(billing) == hl.conv_key(dict(base, system="x-anthropic-billing-header: cch=zzz\nsame"))
    assert hl.conv_key({"messages": [_pair(1)[1]]}) is None and hl.conv_key({"messages": []}) is None


def test_two_conversations_with_the_same_first_message_never_share_or_cross_apply_state(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    a, b = _body(30, 2400), _body(30, 2400)
    b["system"] = "a different system prompt"
    ka, kb = hl.conv_key(a), hl.conv_key(b)
    assert ka != kb
    hl.arrange(pb.non_system(a["messages"]), key=ka, window=WINDOW, fixed_est=0, reserved=0)
    assert hl.take_stats(kb) is None
    hl.arrange(pb.non_system(b["messages"]), key=kb, window=WINDOW, fixed_est=0, reserved=0)
    assert hl.take_stats(kb)["reason"] == "first"  # b did not inherit a's boundary
    # hold() refuses a head whose source message is not this conversation's, even under a (forced) shared key
    head = copy.deepcopy(a["messages"][0])
    head["content"] = [{"type": "text", "text": head["content"]}, {"type": "text", "text": _summary(1, 1, 1, 8)}]
    hl.observe(ka, [head] + a["messages"][21:], moved=True, reason="over_cap", freeze=True)
    foreign = copy.deepcopy(a["messages"])
    foreign[0]["content"] = "a different first message"
    assert hl.hold(ka, foreign, lambda m: True) is None
    assert hl.hold(ka, a["messages"], lambda m: True) is not None


def test_no_key_means_no_layout_and_no_state(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    turns = [_pair(1)[1], {"role": "user", "content": "x"}]
    assert hl.arrange(turns, key=None, window=WINDOW, fixed_est=0, reserved=0) is None
    hl.observe(None, [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}], moved=True, reason="x")
    assert hl._STATE == {} or "-" not in hl._STATE


def test_one_request_writes_the_state_once_when_the_local_agent_path_owns_it(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    body = _body(30, 2400)
    key = hl.conv_key(body)
    hl.observe(key, body["messages"][:2], moved=False, reason="under_cap")
    before = copy.deepcopy(hl._STATE[key]).__dict__
    tok = hl.LOCAL_AGENT_OWNS.set(True)
    try:
        out = _fast(body)  # the fast trim runs in the same request (all three flags on)
    finally:
        hl.LOCAL_AGENT_OWNS.reset(tok)
    assert hl._STATE[key].__dict__ == before  # untouched: age and growth counted once
    monkeypatch.delenv("LLM_ROUTER_PROXY_LAYOUT")
    assert out["messages"] == _fast(body)["messages"]  # it fell back to exactly the old packing


def test_trim_path_marks_the_dropped_layers_in_the_ledger_row(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    body = _body(30, 2400)
    _fast(body)
    st = hl.take_stats(hl.conv_key(body))
    assert st["dropped"] == ["rolling_summary", "session_context", "context_pack_text_caps", "full_mode_cap"]
    hl.clear()
    turns = pb.non_system(body["messages"])
    hl.observe(hl.conv_key(body), turns[:4], moved=False, reason="under_cap")
    assert hl.take_stats(hl.conv_key(body))["dropped"] == []  # local-agent path drops nothing


def test_huge_head_or_huge_newest_unit_returns_none_instead_of_moving_every_turn(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    big_head = [{"role": "user", "content": "log " * 30_000}] + _pair(0) + _pair(1)
    assert _arr(big_head, window=WINDOW, fixed_est=0, reserved=0) is None
    big_unit = [{"role": "user", "content": "task"}] + _pair(0) + _pair(1, 120_000)
    assert _arr(big_unit, window=WINDOW, fixed_est=0, reserved=0) is None
    assert hl.take_stats(_k(big_head)) is None and hl.take_stats(_k(big_unit)) is None
    assert _arr([{"role": "user", "content": "task"}] + _pair(0), window=WINDOW, fixed_est=0, reserved=0)  # control


def test_sizes_use_the_guards_math_and_reserve_the_okf_block(monkeypatch):
    """HARD is 80% of the window by token_estimate x 1.18 less reserve and OKF, not by the raw estimate."""
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT_K", "1000")
    worst = 0
    for n in range(4, 60):
        turns = pb.non_system(_body(n, 2400)["messages"])
        msgs = _arr(turns, fixed_est=0, reserved=DEFAULT_RESERVED_OUTPUT_TOKENS)
        worst = max(worst, est_tokens(msgs))
    limit = (WINDOW - DEFAULT_RESERVED_OUTPUT_TOKENS - hl.OKF_RESERVE)
    assert worst > 0.5 * limit / 1.18  # the run does reach the hard region: not vacuous
    assert worst * 1.18 <= hl.HARD * limit  # 1.18: the guard/estimate ratio measured by the #900 review; 80% of the window by the guard's math, not the raw estimate


def test_starts_never_begin_a_tail_at_a_tool_result():
    """Pins the fused units directly: the pairing loop must not be what repairs a split."""
    rest = _pair(0) + _pair(1) + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "go"}]
    starts = hl._starts(rest, prefer_assistant=False)
    assert starts == [0, 2, 4, 5]
    assert all(not (rest[a]["role"] == "user" and rest[a]["content"][0].get("type") == "tool_result")
               for a in starts if isinstance(rest[a]["content"], list))


def test_hold_refuses_an_unpaired_candidate():
    key_msgs = [{"role": "user", "content": "task"}, _pair(1)[1], {"role": "assistant", "content": "ok"}, {"role": "user", "content": "go"}]
    key = _k(key_msgs)
    hl.observe(key, [key_msgs[0], key_msgs[1], key_msgs[2], key_msgs[3]], moved=True, reason="over_cap", freeze=True)
    # the frozen boundary is a tool_result whose tool_use is not in the history: held, it would be an orphan
    assert hl.hold(key, key_msgs, lambda m: True) is None


def test_cache_control_moving_between_turns_does_not_break_the_hold():
    msgs = _body(30, 2400)["messages"]
    key = _k(msgs)
    cut = msgs[1 + 2 * 10:]
    hl.observe(key, [copy.deepcopy(msgs[0])] + cut, moved=True, reason="over_cap", freeze=True)
    moved = copy.deepcopy(msgs) + _pair(99)
    for m in moved[1 + 2 * 10: 1 + 2 * 10 + 2]:
        for b in m["content"]:
            b["cache_control"] = {"type": "ephemeral"}  # Claude Code moves this marker every turn
    held = hl.hold(key, moved, lambda m: True)
    assert held is not None and len(held) == len(cut) + 3


def test_hold_returns_a_copy_of_the_head_and_touches_lru_on_read():
    msgs = _body(30, 2400)["messages"]
    key = _k(msgs)
    hl.observe(key, [copy.deepcopy(msgs[0])] + msgs[21:], moved=True, reason="over_cap", freeze=True)
    held = hl.hold(key, msgs, lambda m: True)
    held[0]["content"] = "mutated downstream"
    assert hl.hold(key, msgs, lambda m: True)[0]["content"] != "mutated downstream"
    other = [{"role": "user", "content": "other"}] + _pair(0)
    hl.observe(_k(other), other, moved=False, reason="under_cap")
    assert list(hl._STATE)[-1] == _k(other)
    hl.hold(key, msgs, lambda m: True)
    assert list(hl._STATE)[-1] == key


def test_both_paths_pick_the_same_boundary_among_duplicate_messages():
    dup = {"role": "assistant", "content": "ok"}
    rest = [copy.deepcopy(dup), {"role": "user", "content": "a"}, copy.deepcopy(dup), {"role": "user", "content": "b"}]
    assert hl._locate(rest, hl.sig(dup)) == 0  # the first, as hold() and arrange() both resolve it


def test_commit_without_a_key_keeps_no_state():
    hl._commit(None, [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}], moved=True, reason="x")
    assert len(hl._STATE) == 0
