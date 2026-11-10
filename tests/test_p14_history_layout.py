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
                {"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": (f"out {i} " * 200)[:chars]}]}]


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
        msgs = hl.arrange(pb.non_system(body["messages"]), window=WINDOW, fixed_est=500,
                          reserved=DEFAULT_RESERVED_OUTPUT_TOKENS)
        out.append((msgs, hl.take_stats(hl.conv_key(body["messages"]))))
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
        hl.arrange(pb.non_system(body["messages"]), window=WINDOW, fixed_est=500, reserved=DEFAULT_RESERVED_OUTPUT_TOKENS)
        seen.add(hl.take_stats(hl.conv_key(body["messages"]))["reason"])
    assert "over_hard" in seen and "k_turns" not in seen


def test_client_rewriting_history_moves_the_boundary(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    turns = pb.non_system(_body(30, 2400)["messages"])
    hl.arrange(turns, window=WINDOW, fixed_est=500, reserved=0)
    hl.take_stats(hl.conv_key(turns))
    rewritten = copy.deepcopy(turns)
    for m in rewritten[1:]:
        if m["role"] == "assistant":
            m["content"][0]["input"]["command"] += " --changed"
    hl.arrange(rewritten + _pair(99), window=WINDOW, fixed_est=500, reserved=0)
    assert hl.take_stats(hl.conv_key(turns))["reason"] == "history_changed"


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
    m1 = hl.arrange(t1, window=WINDOW, fixed_est=500, reserved=0)
    t2 = _p9_body(25, 10, 1, pin)
    m2 = hl.arrange(t2, window=WINDOW, fixed_est=500, reserved=0)
    head = m2[0]["content"]
    assert head[0]["text"].startswith("Fix the failing test")
    assert head[1]["text"].startswith("<chunk-summary") and "pinned:" in head[1]["text"] and "src/app.py" in head[1]["text"]
    assert m2[: len(m1)] == m1  # summaries + pins + tail so far are a byte-identical prefix
    st = hl.take_stats(hl.conv_key(t2))
    assert st["moved"] is False and st["msgs_same_prev"] == len(m1)


def test_a_new_closed_chunk_moves_the_boundary_at_once(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    m1 = hl.arrange(_p9_body(24, 10, 1, ""), window=WINDOW, fixed_est=500, reserved=0)
    hl.take_stats(hl.conv_key(m1))
    t2 = _p9_body(25, 14, 2, "")  # P9 closed one more chunk: two summaries, the cut moved forward
    m2 = hl.arrange(t2, window=WINDOW, fixed_est=500, reserved=0)
    st = hl.take_stats(hl.conv_key(t2))
    assert st["moved"] and st["reason"] == "chunk_closed"
    heads = [b["text"] for b in m2[0]["content"] if b["text"].startswith("<chunk-summary")]
    assert len(heads) == 2
    assert m2[0]["content"][0] == m1[0]["content"][0]  # the base of the first message is untouched


def test_conv_key_ignores_summary_blocks():
    a = _p9_body(24, 10, 1, "")
    b = _p9_body(24, 14, 2, "")
    full = _body(24, 2400)["messages"]
    assert hl.conv_key(a) == hl.conv_key(b) == hl.conv_key(full)


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
    msgs = hl.arrange(turns, window=WINDOW, fixed_est=500, reserved=0)
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
    assert hl.arrange(orphan, window=WINDOW, fixed_est=0, reserved=0) is None
    assert hl.arrange([{"role": "user", "content": "hi"}], window=WINDOW, fixed_est=0, reserved=0) is None
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
        hl.arrange(pb.non_system(body["messages"]), window=WINDOW, fixed_est=500, reserved=0)
        row: dict = {}
        ps._note_prefix(row, body, {"prefix_obs": {"sys": "x", "msgs": ["a"], "keep_alive": -1}})
        rows.append(row["layout"])
    assert rows[0]["msgs_same_prev"] is None and rows[0]["tokens_same_prev"] is None
    assert rows[1]["msgs_same_prev"] == rows[0]["msgs"] and rows[1]["tokens_same_prev"] > 0
    assert rows[2]["msgs_same_prev"] == rows[1]["msgs"]
    for r in rows:
        assert set(r) == {"moved", "reason", "age", "held_before_move", "msgs", "tokens_est", "msgs_same_prev", "tokens_same_prev"}
        assert all(isinstance(v, (int, bool, str, type(None))) for v in r.values())  # counts only, no text


def test_a_retry_of_the_same_request_does_not_age_the_boundary(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    turns = pb.non_system(_body(10, 400)["messages"])
    for _ in range(6):
        hl.arrange(turns, window=WINDOW, fixed_est=0, reserved=0)
    assert hl.take_stats(hl.conv_key(turns))["age"] <= 1


def test_state_is_lru_bounded(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    for i in range(hl.MAX_CONVERSATIONS + 10):
        turns = [{"role": "user", "content": f"task {i}"}] + _pair(0)
        hl.arrange(turns, window=WINDOW, fixed_est=0, reserved=0)
    assert len(hl._STATE) == hl.MAX_CONVERSATIONS


# -- non-vacuity: mutate in-process and watch the guards fail --------------------------------------------

def test_mutation_sliding_boundary_breaks_prefix_stability(monkeypatch):
    """If the boundary slid every turn (K=1 and soft 0), the stability test above must fail."""
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT_K", "1")
    monkeypatch.setattr(hl, "SOFT", 0.0)
    sent = [_send(_body(n, 3000))["messages"] for n in range(40, 44)]
    assert any(later[: len(sent[0])] != sent[0] for later in sent[1:])


def test_mutation_a_paired_check_that_always_passes_lets_an_orphan_through():
    p = _pair(1)
    assert hl.paired([p[1]]) is False
    orig = hl.paired
    try:
        hl.paired = lambda m: True
        assert hl.paired([p[1]]) is True  # the mutated guard is blind: the real one is what the tests above rely on
    finally:
        hl.paired = orig


def test_mutation_split_units_would_orphan(monkeypatch):
    """A boundary at a tool_result message (what a non-fused chunker gives) is caught by ``paired``."""
    turns = pb.non_system(_body(10, 400)["messages"])
    for cut in range(1, len(turns)):
        tail = [turns[0]] + turns[cut:]
        assert hl.paired(tail) == (turns[cut]["role"] == "assistant" or turns[cut]["content"][0].get("type") != "tool_result")


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
    monkeypatch.setattr(hl, "hold", lambda messages, fits: None)  # the mutation: never hold
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
    assert hl.hold(msgs, lambda m: True) is None  # nothing frozen yet
    head = copy.deepcopy(msgs[0])
    cut = msgs[1 + 2 * 20:]
    hl.observe([head] + cut, moved=True, reason="over_cap", freeze=True)
    grown = msgs + _pair(61)
    held = hl.hold(grown, lambda m: True)
    assert held is not None and held[: len([head] + cut)] == [head] + cut and len(held) == len(cut) + 3
    assert hl.hold(grown, lambda m: False) is None  # no longer fits: a move
    rewritten = copy.deepcopy(grown)
    rewritten[1 + 2 * 20]["content"][0]["input"]["command"] = "other"
    assert hl.hold(rewritten, lambda m: True) is None  # the boundary message is gone
    hl.observe(grown[:2], moved=False, reason="under_cap")  # freeze cleared when the whole history fits again
    assert hl.hold(grown, lambda m: True) is None


def test_move_goal_reserves_k_turns_of_observed_growth_and_never_goes_below_half_cap():
    msgs = _body(10, 400)["messages"]
    assert hl.move_goal(msgs, 25_000) == 25_000 - 4 * hl.DEFAULT_GROWTH
    assert hl.move_goal(msgs, 5_000) == int(5_000 * hl.SOFT)


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
