"""Pull is gated on approval and uses an injected stream; no network."""

from __future__ import annotations

from llm_router.model_pull import pull_model


def _stream(objs):
    calls = []

    def s(url, body):
        calls.append((url, body))
        yield from objs
    s.calls = calls
    return s


def test_not_approved_never_calls_stream():
    s = _stream([{"status": "success"}])
    for approved in (False, None, 1, "yes"):
        assert pull_model("m:1b", approved=approved, stream=s).status == "not_approved"
    assert s.calls == []


def test_progress_and_success():
    s = _stream([{"status": "pulling x", "completed": 5, "total": 10}, {"status": "success"}])
    seen = []
    r = pull_model("m:1b", approved=True, stream=s, on_progress=lambda *a: seen.append(a))
    assert r.status == "success" and (r.last_completed, r.last_total) == (5, 10)
    assert seen == [("pulling x", 5, 10), ("success", 0, 0)]
    assert s.calls[0] == ("http://localhost:11434/api/pull", {"model": "m:1b", "stream": True})


def test_error_object_is_fixed_text():
    r = pull_model("m", approved=True, stream=_stream([{"error": "secret sk-123"}]))
    assert r.status == "error" and "sk-123" not in r.detail


def test_stream_raising_and_incomplete():
    def boom(url, body):
        raise OSError("sk-999")
        yield {}
    r = pull_model("m", approved=True, stream=boom)
    assert r.status == "error" and "sk-999" not in r.detail
    assert pull_model("m", approved=True, stream=_stream([{"status": "pulling"}])).detail == "incomplete"


def test_non_loopback_refused():
    s = _stream([{"status": "success"}])
    assert pull_model("m", approved=True, stream=s, base_url="http://evil.example").status == "error"
    assert s.calls == []
