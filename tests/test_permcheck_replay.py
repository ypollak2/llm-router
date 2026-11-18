"""scripts/permcheck_replay.py: parser, statistics, guards and the replay loop against a fake local backend.

Every request and reply below is synthetic, built from the classifier's documented format
(``<block>yes|no</block>``). No captured text is read; no model or port is contacted.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "permcheck_replay", Path(__file__).resolve().parent.parent / "scripts" / "permcheck_replay.py")
rp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rp)

UTC = dt.timezone.utc
NOON = dt.datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def request(marker: bool = True, text: str = "synthetic transcript") -> dict:
    sys_text = "classifier instructions: answer with <block>yes</block> or <block>no</block>" if marker else "plain"
    return {"model": "claude-test", "max_tokens": 64, "system": [{"type": "text", "text": sys_text}],
            "messages": [{"role": "user", "content": text}]}


def record(claude_reply: str, req: dict | None = None) -> dict:
    req = req or request()
    return {"request_body": json.dumps(req), "_request": req,
            "reply_content": [{"type": "text", "text": claude_reply}]}


# --- parser (B2) ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("<block>no</block>", "allow"),
    ("<block>yes</block>", "deny"),
    ("<block>no", "allow"),
    ("<block>yes</block><reason>synthetic</reason>", "deny"),
    # the product reads the raw text first: a yes and a no anywhere (thinking included) is unparsed
    ("<thinking>maybe <block>yes</block> is wrong</thinking><block>no</block>", "unparsed"),
    ("<thinking>maybe <block>no</block> is wrong</thinking><block>no</block>", "allow"),
    # not anchored: prose before the tag does not hide the verdict; a quoted format with both is unparsed
    ("Looking at this action, my answer is <block>yes</block>", "deny"),
    ("The format is <block>yes</block> or <block>no</block>. Answer: <block>no</block>", "unparsed"),
    ("<BLOCK>YES</BLOCK>", "deny"),
    ("<thinking>never closed <block>yes</block>", "unparsed"),
    ("  \n<block>yes</block>", "deny"),
    ("<block>maybe</block>", "unparsed"),
    ("<block>yesterday</block>", "unparsed"),
    ("allow", "unparsed"),
    ("", "unparsed"),
])
def test_parse_verdict_on_synthetic_replies(text, expected):
    assert rp.parse_verdict(text, rp.FORMAT_XML_BLOCK) == expected


def test_unknown_format_is_always_unparsed():
    assert rp.parse_verdict("<block>no</block>", None) == "unparsed"
    assert rp.detect_format(request(marker=False)) is None
    assert rp.detect_format(request()) == rp.FORMAT_XML_BLOCK


def test_detect_format_reads_messages_too():
    req = request(marker=False, text="end with <block>yes</block> or <block>no</block>")
    assert rp.detect_format(req) == rp.FORMAT_XML_BLOCK


# --- statistics (B5) -----------------------------------------------------------------------------------

def test_wilson_known_values():
    lo, hi = rp.wilson(0, 100)
    assert lo == 0.0 and hi == pytest.approx(0.0370, abs=1e-3)
    lo, hi = rp.wilson(50, 100)
    assert (lo, hi) == (pytest.approx(0.4038, abs=1e-3), pytest.approx(0.5962, abs=1e-3))
    assert rp.wilson(0, 0) == (None, None)


def test_percentiles_nearest_rank():
    assert rp.percentiles(list(range(1, 11))) == {"p50": 5, "p90": 9, "max": 10}
    assert rp.percentiles([]) == {"p50": None, "p90": None, "max": None}


def _res(claude, local, **kw):
    return {"claude": claude, "local": local, "over_window": False, "wall_ms": 10.0, "est_tokens": 100,
            "backend_error": False, **kw}


def test_summarise_matrix_agreement_and_false_allow():
    results = (
        [_res("allow", "allow")] * 6 + [_res("allow", "deny")] * 1 + [_res("allow", "timeout")] * 1
        + [_res("deny", "deny")] * 3 + [_res("deny", "allow")] * 1
        + [_res("unparsed", "allow")] * 2
        + [_res("allow", None, over_window=True, wall_ms=None)] * 2
    )
    s = rp.summarise(results, n_captured=16)
    assert (s["n_captured"], s["n_sent"], s["over_window"], s["timeout"], s["claude_unparsed"]) == (16, 14, 2, 1, 2)
    assert s["matrix_claude_by_local"]["allow"] == {"allow": 6, "deny": 1, "unparsed": 0, "timeout": 1}
    assert s["matrix_claude_by_local"]["deny"] == {"allow": 1, "deny": 3, "unparsed": 0, "timeout": 0}
    assert (s["agreement"]["k"], s["agreement"]["n"]) == (9, 12)
    assert (s["false_allow"]["k"], s["false_allow"]["n"]) == (1, 4)
    assert s["false_allow"]["ci95"] == list(rp.wilson(1, 4))
    assert (s["false_allow_of_all_matrix_rows"]["k"], s["false_allow_of_all_matrix_rows"]["n"]) == (1, 12)
    assert s["latency_ms"]["p50"] == 10.0 and s["prompt_tokens_est"]["max"] == 100


# --- guards (B6, B1) -----------------------------------------------------------------------------------

@pytest.mark.parametrize("h,m,quiet", [(10, 14, False), (10, 15, True), (10, 44, True), (10, 45, False),
                                       (16, 15, True), (16, 44, True), (16, 45, False), (12, 0, False)])
def test_quiet_slots(h, m, quiet):
    assert rp.in_quiet_slot(dt.datetime(2026, 11, 17, h, m, tzinfo=UTC)) is quiet


def test_quiet_slot_uses_utc():
    plus3 = dt.timezone(dt.timedelta(hours=3))
    assert rp.in_quiet_slot(dt.datetime(2026, 11, 17, 13, 20, tzinfo=plus3)) is True  # 10:20Z


def test_resident_refusal(monkeypatch):
    ps = {"models": [{"name": "fake:1", "context_length": 32768}]}
    assert rp.resident_refusal("ollama/fake:1", "http://h", 32768, lambda url: ps) is None
    assert "differs" in rp.resident_refusal("ollama/fake:1", "http://h", 65536, lambda url: ps)
    assert "not resident" in rp.resident_refusal("ollama/other:1", "http://h", 32768, lambda url: ps)

    def down(url):
        raise OSError("down")

    assert "cannot reach" in rp.resident_refusal("ollama/fake:1", "http://h", 32768, down)


# --- replay loop (B3, B4) ------------------------------------------------------------------------------

class FakeBackend:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, body, timeout_s, priority="interactive"):
        self.calls += 1
        r = self.replies.pop(0)
        if r == "hang":
            await asyncio.sleep(10)
        if r == "error":
            return None, "ollama error", {}
        return {"content": [{"type": "text", "text": r}]}, None, {}


def run(records, backend, *, window=1000, estimate=lambda r: 100, max_n=None, timeout_s=0.05, now_fn=lambda: NOON):
    return asyncio.run(rp.replay(records, backend=backend, window=window, timeout_s=timeout_s,
                                 estimate=estimate, max_n=max_n, now_fn=now_fn))


def test_replay_counts_over_window_timeout_and_errors():
    recs = [record("<block>no</block>", request(text="a")), record("<block>yes</block>", request(text="b")),
            record("<block>no</block>", request(text="c" * 5000)), record("<block>no</block>", request(text="d")),
            record("<block>no</block>", request(text="e"))]
    backend = FakeBackend(["<block>no</block>", "<block>no</block>", "hang", "error"])
    results, stopped = run(recs, backend, estimate=lambda r: 5000 if "ccc" in json.dumps(r) else 100)
    assert stopped is None and backend.calls == 4            # the over-window request is never sent
    assert [r["over_window"] for r in results] == [False, False, True, False, False]
    assert [r["local"] for r in results] == ["allow", "allow", None, "timeout", "unparsed"]
    assert [r["claude"] for r in results] == ["allow", "deny", "allow", "allow", "allow"]
    assert results[4]["backend_error"] is True
    assert all(r["wall_ms"] is not None for r in results if r["local"] is not None)
    s = rp.summarise(results, n_captured=len(recs))
    assert s["false_allow"]["k"] == 1 and s["false_allow"]["n"] == 1   # Claude denied, local allowed
    assert s["over_window"] == 1 and s["timeout"] == 1 and s["n_sent"] == 4


def test_replay_window_edge_uses_reserved_output():
    from llm_router.local_context_guard import DEFAULT_RESERVED_OUTPUT_TOKENS as RES

    backend = FakeBackend(["<block>no</block>"])
    results, _ = run([record("<block>no</block>"), record("<block>no</block>")], backend, window=1000,
                     estimate=lambda r, _s=iter([1000 - RES, 1000 - RES + 1]): next(_s))
    assert [r["over_window"] for r in results] == [False, True] and backend.calls == 1


def test_replay_max_n_limits_sent():
    recs = [record("<block>no</block>", request(text=str(i))) for i in range(5)]
    backend = FakeBackend(["<block>no</block>"] * 5)
    results, stopped = run(recs, backend, max_n=2)
    assert backend.calls == 2 and len(results) == 2 and stopped == "max_n"


def test_replay_stops_cleanly_in_quiet_slot():
    clock = iter([NOON, NOON, dt.datetime(2026, 11, 17, 16, 20, tzinfo=UTC)])
    recs = [record("<block>no</block>", request(text=str(i))) for i in range(4)]
    backend = FakeBackend(["<block>no</block>"] * 4)
    results, stopped = run(recs, backend, now_fn=lambda: next(clock))
    assert stopped == "quiet_slot" and backend.calls == 2 and len(results) == 2


# --- main (end to end, fake backend and fake /api/ps) --------------------------------------------------

def _write_captures(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps({"request_body": r["request_body"], "reply_content": r["reply_content"]}) + "\n"
                            for r in records))


def _argv(tmp_path, *extra):
    return ["--captures", str(tmp_path / "cap"), "--out", str(tmp_path / "out.json"), "--model", "ollama/fake:1",
            "--ollama-url", "http://127.0.0.1:11999", "--num-ctx", "32768", *extra]


def _ps(window=32768):
    return lambda url: {"models": [{"name": "fake:1", "context_length": window}]}


def test_main_writes_counts_only(tmp_path, capsys):
    (tmp_path / "cap").mkdir()
    _write_captures(tmp_path / "cap" / "2026-11-17.jsonl",
                    [record("<block>no</block>", request(text="secret-looking synthetic one")),
                     record("<block>yes</block>", request(text="secret-looking synthetic two"))])
    backend = FakeBackend(["<block>no</block>", "<block>yes</block>"])
    rc = rp.main(_argv(tmp_path), now_fn=lambda: NOON, get=_ps(), backend_factory=lambda m, u, n: backend)
    assert rc == 0
    out = json.loads((tmp_path / "out.json").read_text())
    assert out["n_captured"] == 2 and out["n_sent"] == 2 and out["agreement"]["k"] == 2
    assert out["false_allow"]["k"] == 0 and out["false_allow"]["n"] == 1
    printed = capsys.readouterr().out + (tmp_path / "out.json").read_text()
    assert "secret-looking" not in printed and "classifier instructions" not in printed


def test_main_refuses_in_quiet_slot_and_sends_nothing(tmp_path, capsys):
    backend = FakeBackend([])
    rc = rp.main(_argv(tmp_path), now_fn=lambda: dt.datetime(2026, 11, 17, 10, 30, tzinfo=UTC), get=_ps(),
                 backend_factory=lambda m, u, n: backend)
    assert rc == 2 and backend.calls == 0 and not (tmp_path / "out.json").exists()
    assert "quiet slot" in capsys.readouterr().err


def test_main_refuses_a_window_that_would_reload_the_runner(tmp_path, capsys):
    built = []
    rc = rp.main(_argv(tmp_path), now_fn=lambda: NOON, get=_ps(window=16384),
                 backend_factory=lambda m, u, n: built.append(1))
    assert rc == 2 and built == [] and "differs" in capsys.readouterr().err


def test_main_refuses_a_model_that_is_not_resident(tmp_path, capsys):
    built = []
    rc = rp.main(_argv(tmp_path), now_fn=lambda: NOON, get=lambda url: {"models": []},
                 backend_factory=lambda m, u, n: built.append(1))
    assert rc == 2 and built == [] and "not resident" in capsys.readouterr().err
