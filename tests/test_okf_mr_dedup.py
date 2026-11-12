"""The OKF prior-turn block does not repeat what the MR wire payload already carries (review follow-up S1, REVIEW-P8-897).

The block is ``session_store.build_session_context`` (via ``okf_context.attach``): the newest hook-recorded events
(user prompts, assistant answers and ``Tool(input) -> result`` events). The request itself carries the same tool
calls and results, and the three newest events are the ones in its verbatim tail. Beside MR chunk summaries, which
only happens with LLM_ROUTER_OKF_BY_WINDOW on (the default 5000-token ceiling leaves no room), the block therefore
repeated the tail. Measured by ``_measure`` (its own plain check, not ``_is_covered``) on the four shapes below, 1 run
each, est_tokens = chars/4, synthetic hook events built from the shape's own tool calls. Real hook results are JSON
objects (98.6%), so the dict format is the one that counts: at c305016b every shape put 2 events (644 of the block's
670 est tokens) into the block that the rest of the payload carries; with the decoder: none repeated (the block is empty unless an event the request lacks exists).

``dedupe_session`` (server.py passes ``okf_by_window_on()``) drops the events the request carries verbatim. Events
the request lost to summarisation are not covered and stay. Flags off, nothing changes (tests/test_proxy_off_golden*).
"""
from __future__ import annotations

import copy
import json
import subprocess

import pytest

from llm_router import session_store
from llm_router.proxy import okf_context
from llm_router.session_store import SENTINEL_CLOSE, SENTINEL_OPEN, _format_record, _is_covered, _squash
from llm_router.token_estimate import est_tokens
from tests.test_p8_summary_e2e import WireBackend, home  # noqa: F401  (fixture)
from tests.test_p9_proxy_mr import _session, _warm
from tests.test_proxy_local_mode import _overrides_readable, _post, env  # noqa: F401  (fixtures)

#: (round trips, chars per tool result): the p9 ``_session`` shape and three others MR summarises.
SHAPES = {"20x4500": (20, 4_500), "14x4500": (14, 4_500), "30x3000": (30, 3_000), "16x6500": (16, 6_500)}
MARKER = "PRIOR-TURN-MARKER the widget parser must keep its old signature"


def _result(i: int, chars: int) -> str:
    """Distinct rows (no repeated lines, which the event log would compact away); about ``chars`` long."""
    out, j = [], 0
    while sum(len(x) + 1 for x in out) < chars:
        out.append(f"r{i} row {j} val={(i + 1) * 7919 + j * 31}")
        j += 1
    return "\n".join(out)


FORMATS = ("dict", "text")


def _stored(fmt: str, text: str) -> str:
    """The result as the hook stores it, cut at 500 chars. ``dict``: ``json.dumps`` of a Bash ``tool_response``
    (98.6% of 32,184 real tool_call events, count from the 212 hook logs of 2026-11-12); ``text``: the bare string."""
    if fmt == "text":
        return text[:500]
    return json.dumps({"stdout": text, "stderr": "", "interrupted": False, "isImage": False})[:500]


def _body_and_log(home_, monkeypatch, pairs: int, chars: int, marker_last: bool = False,
                  fmt: str = "dict") -> tuple[dict, str, str]:
    """A follow-up conversation of ``pairs`` tool round trips plus the events the hooks would have recorded for it."""
    body = _session(pairs, chars)
    for i in range(pairs):
        body["messages"][2 + 2 * i]["content"][0]["content"] = _result(i, chars)
    repo = home_ / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    body["system"] = list(body["system"]) + [{"type": "text", "text": f"Primary working directory: {repo}\n"}]
    sid = json.loads(body["metadata"]["user_id"])["session_id"]
    msgs = body["messages"]
    monkeypatch.chdir(repo)
    first = msgs[0]["content"]
    session_store.record_event(sid, "user_prompt", first if isinstance(first, str) else first[0]["text"])
    for i in range(pairs):
        use, res = msgs[1 + 2 * i]["content"][0], msgs[2 + 2 * i]["content"][0]
        session_store.record_event(sid, "tool_call", f"{use['name']}({json.dumps(use['input'])}) -> {_stored(fmt, res['content'])}",
                                   role="tool", tool=use["name"])
    if marker_last:  # an event the request does not carry, newest in the log
        session_store.record_event(sid, "user_prompt", MARKER)
    monkeypatch.chdir(home_)
    msgs += [{"role": "assistant", "content": "I changed the parser."}, {"role": "user", "content": "do it"}]
    return body, sid, str(repo)


def _plain(sent: dict) -> str:
    """The text of the Ollama payload's messages, no JSON escaping."""
    parts: list[str] = []

    def add(v):
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            for x in v:
                add(x)
        elif isinstance(v, dict):
            add(v.get("text") if v.get("type") == "text" else v.get("content", v.get("input")))

    for m in sent["messages"]:
        add(m.get("content"))
        for call in m.get("tool_calls") or []:  # the Ollama payload carries a call's input here
            parts.append(json.dumps((call.get("function") or {}).get("arguments"), ensure_ascii=False))
    return "\n".join(str(p) for p in parts)


def _block_of(plain: str) -> str:
    a = plain.find(SENTINEL_OPEN)
    return "" if a < 0 else plain[a:plain.index(SENTINEL_CLOSE, a) + len(SENTINEL_CLOSE)]


def _on_wire(rec: dict, rest: str) -> bool:
    """Whether *rec*'s tool result is in *rest*, judged WITHOUT the code under test (a deliberately plain
    check: the first 80 chars of the stored result, a JSON ``stdout`` string un-escaped by hand)."""
    content = _squash(rec.get("content", ""))
    if " -> " not in content:
        return False
    res = content.split(" -> ", 1)[1]
    if res.startswith("{"):
        res = res.split('"stdout": "', 1)[-1].replace("\\n", "\n").replace('\\"', '"')
    probe = _squash(res)[:80]
    return len(probe) >= 40 and probe in rest


def _measure(sent: dict, sid: str, repo: str) -> dict:
    """Events in the session block that the rest of the same wire payload carries verbatim, in estimated tokens
    (``est_tokens`` of the block's own rendering of each event). The block is cut out of the payload first.
    ``_on_wire`` is independent of ``session_store._is_covered``, so an old ``_is_covered`` cannot hide a repeat."""
    plain = _plain(sent)
    block = _block_of(plain)
    rest = _squash(plain.replace(block, "")) if block else _squash(plain)
    events = session_store.load_events(sid, limit=200, project_root=repo)
    block_sq = _squash(block)
    in_block = [r for r in events if block and (_squash(r["content"])[:60] in block_sq or _squash(r["content"])[-60:] in block_sq)]
    dup = [r for r in in_block if _on_wire(r, rest)]
    return {"block_tokens": est_tokens(block) if block else 0, "events": len(in_block), "dup_events": len(dup),
            "dup_tokens": sum(est_tokens(_format_record(r)) for r in dup),
            "log_events_in_wire": sum(1 for r in events if _on_wire(r, rest))}


async def _run(env_, home_, monkeypatch, pairs, chars, *, marker_last=False, by_window="1", fmt="dict"):
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setenv("LLM_ROUTER_OKF_BY_WINDOW", by_window)
    body, sid, repo = _body_and_log(home_, monkeypatch, pairs, chars, marker_last, fmt)
    wire = WireBackend()
    app = env_.app(wire)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    return wire.live[-1], env_.rows()[-1], sid, repo


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("shape", list(SHAPES))
async def test_the_block_repeats_nothing_the_wire_carries(shape, fmt, env, home, monkeypatch):  # noqa: F811
    sent, row, sid, repo = await _run(env, home, monkeypatch, *SHAPES[shape], marker_last=True, fmt=fmt)
    m = _measure(sent, sid, repo)
    assert row["mr_summary"]["outcome"] == "summarized" and row["okf"]["injected"] is True
    assert m["log_events_in_wire"] >= 2, m  # premise: there IS logged content the wire carries, so 0 below is not vacuous
    assert m["events"] >= 1 and m["dup_events"] == 0 and m["dup_tokens"] == 0, m


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("shape", list(SHAPES))
async def test_content_the_wire_lacks_survives_exactly_once(shape, fmt, env, home, monkeypatch):  # noqa: F811
    sent, row, _, _ = await _run(env, home, monkeypatch, *SHAPES[shape], marker_last=True, fmt=fmt)
    plain = _plain(sent)
    assert plain.count(MARKER) == 1 and plain.count(SENTINEL_OPEN) == 1
    assert plain.count("<chunk-summary") == row["mr_summary"]["chunks"]  # the MR summaries are all still there


@pytest.mark.parametrize("fmt", FORMATS)
async def test_when_the_block_would_only_repeat_the_tail_it_is_not_attached(fmt, env, home, monkeypatch):  # noqa: F811
    sent, row, sid, repo = await _run(env, home, monkeypatch, 20, 4_500, fmt=fmt)
    m = _measure(sent, sid, repo)
    assert row["mr_summary"]["outcome"] == "summarized" and m["log_events_in_wire"] >= 2, m
    assert m["dup_events"] == 0 and m["dup_tokens"] == 0, m
    assert row["okf"]["injected"] is False or SENTINEL_OPEN not in _plain(sent), (row["okf"], m)


async def test_dedupe_off_keeps_the_block_as_it_was(env, home, monkeypatch):  # noqa: F811
    """attach() without dedupe_session builds the block exactly as before: the tail's newest result is in it."""
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    body, _, _ = _body_and_log(home, monkeypatch, 20, 4_500, fmt="text")
    send = copy.deepcopy(body)
    send["messages"] = send["messages"][-8:]  # a verbatim tail that carries the newest tool calls and results
    newest = _squash(_result(19, 4_500))[:60]
    assert newest in _squash(_plain(send))  # premise: the request carries it
    off, info_off = okf_context.attach(send, body, ceiling_tokens=20_000)
    on, _ = okf_context.attach(send, body, ceiling_tokens=20_000, dedupe_session=True)
    assert info_off["injected"] is True and newest in _squash(json.dumps(off["system"]).replace("\\n", " "))
    assert newest not in _squash(json.dumps(on["system"]).replace("\\n", " "))


def test_a_short_event_is_never_counted_as_covered():
    assert not _is_covered({"content": "do it"}, _squash("please do it now, then do it again"))
    assert _is_covered({"content": 'Bash({"command": "ls -la"}) -> r1 row 0 val=7919 r1 row 1 val=7950'},
                       _squash('x {"command": "ls -la"} r1 row 0 val=7919\nr1 row 1 val=7950 y'))


HEAD80 = "No matches found in the repository for this pattern, searched under src and tests: "  # 80+ chars shared


def test_a_different_call_with_the_same_result_prefix_does_not_cover_an_event():
    assert len(HEAD80) >= 80
    on_wire = _squash('{"pattern": "alpha"} ' + HEAD80 + "0 files (alpha)")
    other = {"content": f'Grep({{"pattern": "beta"}}) -> {HEAD80}0 files (beta)'}
    assert not _is_covered(other, on_wire)
    same = {"content": f'Grep({{"pattern": "alpha"}}) -> {HEAD80}0 files (alpha)'}
    assert _is_covered(same, on_wire)  # control: the call that IS on the wire is covered


def test_results_that_differ_only_after_char_80_are_not_covered():
    on_wire = _squash('{"pattern": "alpha"} ' + HEAD80 + "0 files (alpha)")
    differs = {"content": f'Grep({{"pattern": "alpha"}}) -> {HEAD80}7 files (alpha)'}  # same call, same 80 chars, other tail
    assert not _is_covered(differs, on_wire)


def test_an_event_lost_to_summarisation_stays(home, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_SESSIONS_PATH", str(home / "sessions.db"))
    monkeypatch.chdir(home)
    sid = "dedupe-unit"
    session_store.record_event(sid, "tool_call", "Read(a.py) -> alpha widget parser body " + "a" * 40, role="tool", tool="Read")
    for i in range(3):
        session_store.record_event(sid, "tool_call", f"Bash(ls {i}) -> newest listing number {i} " + "b" * 40, role="tool", tool="Bash")
    covered = "".join(f"ls {i} newest listing number {i} " + "b" * 40 + "\n" for i in range(3))
    kw = dict(max_tokens=1500, query="widget parser", project_root=str(home))
    plain = session_store.build_session_context(sid, **kw)
    deduped = session_store.build_session_context(sid, covered_text=covered, **kw)
    assert "alpha widget parser" in plain and "newest listing number 2" in plain  # premise: all four are chosen
    assert "alpha widget parser" in deduped and "newest listing number" not in deduped


def test_a_dropped_slot_is_not_refilled_by_an_older_event(home, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_SESSIONS_PATH", str(home / "sessions.db"))
    monkeypatch.chdir(home)
    sid = "dedupe-norefill"
    session_store.record_event(sid, "tool_call", "Read(old.py) -> zebra unrelated older listing " + "z" * 40, role="tool", tool="Read")
    for i in range(3):
        session_store.record_event(sid, "tool_call", f"Bash(ls {i}) -> newest listing number {i} " + "b" * 40, role="tool", tool="Bash")
    covered = "".join(f"ls {i} newest listing number {i} " + "b" * 40 + "\n" for i in range(3))
    kw = dict(max_tokens=1500, query="widget parser", project_root=str(home))
    plain = session_store.build_session_context(sid, **kw)
    assert "newest listing number 0" in plain and "zebra" not in plain  # premise: the older event is not selected
    deduped = session_store.build_session_context(sid, covered_text=covered, **kw)
    assert "zebra" not in deduped and "newest listing number" not in deduped


@pytest.mark.timing
def test_request_text_and_cover_check_stay_fast_on_a_large_payload():
    import time

    big = "x" * 9_000
    msgs = []
    for i in range(300):  # about 5 MB of messages: 300 tool_use inputs and 300 results
        msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash",
                                                       "input": {"command": f"cat f{i}.py " + big}}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": f"r{i} " + big}]})
    events = [{"content": f'Bash({json.dumps(msgs[2 * i]["content"][0]["input"])[:200]}) -> r{i} xxxxxxxxxxxxxxxx'}
              for i in range(0, 300, 10)]  # the hook stores at most 200 chars of a call's input
    t0 = time.monotonic()
    text = okf_context._request_text({"messages": msgs})
    covered = _squash(text)
    hits = sum(_is_covered(e, covered) for e in events)
    elapsed = time.monotonic() - t0
    assert len(text) > 2_500_000  # premise: the payload is as large as claimed
    assert hits == len(events), hits  # premise: every probe really searched and matched
    assert elapsed < 2.0, elapsed


# -- JSON-encoded results: 98.6% of real hook events (see _stored) ------------------------------------------

def _dict_event(cmd: str, stdout: str, cut: int = 500) -> dict:
    stored = json.dumps({"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False})[:cut]
    return {"content": f"Bash({json.dumps({'command': cmd})}) -> {stored}"}


def _wire_of(cmd: str, stdout: str) -> str:
    return _squash(json.dumps({"command": cmd}) + "\n" + stdout)


def test_payload_text_decodes_escapes_and_drops_a_half_escape():
    from llm_router.session_store import _payload_text

    assert _payload_text('{"stdout": "a\\nb \\"q\\" \\u00e9 \\\\ tail", "stderr": ""}') == 'a\nb "q" é \\ tail'
    assert _payload_text('{"stdout": "line one\\nline tw') == "line one\nline tw"  # cut mid-string
    assert _payload_text('{"stdout": "cut after backslash\\') == "cut after backslash"
    assert _payload_text('{"stdout": "cut in unicode \\u00') == "cut in unicode "
    assert _payload_text('{"stdout": "pair \\ud83d\\ude00 ok"}') == "pair \U0001F600 ok"
    assert _payload_text('{"type": "text", "file": {"filePath": "/x", "content": "inner text"}}') == "inner text"
    assert _payload_text('{"filenames": ["a", "b"]}') is None


def test_a_dict_shaped_result_on_the_wire_is_covered_even_when_cut_mid_string():
    out = "".join(f"line {i} of the build output\n" for i in range(60))
    assert _is_covered(_dict_event("make test", out), _wire_of("make test", out))  # cut at 500 chars, mid-string


def test_dict_result_a_different_call_with_the_same_output_prefix_does_not_cover_an_event():
    out = HEAD80 + "0 files (alpha)"
    assert not _is_covered(_dict_event("grep beta src", out), _wire_of("grep alpha src", out))
    assert _is_covered(_dict_event("grep alpha src", out), _wire_of("grep alpha src", out))  # control


def test_dict_result_that_differs_inside_the_stored_prefix_is_not_covered():
    wire = _wire_of("make test", HEAD80 + "0 failures, build ok")
    assert not _is_covered(_dict_event("make test", HEAD80 + "7 failures, build broken"), wire)


def test_dict_result_with_a_short_payload_is_never_covered():
    assert not _is_covered(_dict_event("echo ok", "ok"), _wire_of("echo ok", "ok"))
