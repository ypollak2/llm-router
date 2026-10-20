"""LC-3: local-check runtimes + runner, against fake loopback servers on ephemeral ports.

No test touches a real Ollama or any non-loopback host: every URL is 127.0.0.1:<ephemeral>.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from llm_router.local_check import runner as R
from llm_router.local_check import runtimes as T


class Fake:
    """Configurable fake runtime. kind: ollama | openai."""

    def __init__(self, kind, *, answer="hello world", delay=0.0, finish="stop", digest="abc123",
                 ctx=4096, runtime_extra=None, ctx_field=None):
        self.kind, self.answer, self.delay, self.finish = kind, answer, delay, finish
        self.digest, self.ctx, self.extra, self.ctx_field = digest, ctx, runtime_extra or {}, ctx_field
        self.chat_bodies: list[dict] = []
        self.answers: list[str] | None = None
        self.hosts: set[str] = set()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                outer.hosts.add(self.client_address[0])
                p = self.path
                if outer.kind == "ollama" and p == "/api/tags":
                    m = {"name": "fake-a:1b", "model": "fake-a:1b"}
                    if outer.digest:
                        m["digest"] = outer.digest
                    return self._send(200, {"models": [m]})
                if outer.kind == "openai" and p == "/v1/models":
                    e = {"id": "fake-oa", "object": "model", **outer.extra}
                    return self._send(200, {"data": [e]})
                if outer.kind == "openai" and p == "/props" and outer.ctx_field == "props":
                    return self._send(200, {"default_generation_settings": {"n_ctx": outer.ctx},
                                            "model_path": "/m/fake.gguf"})
                if outer.kind == "openai" and p == "/api/v0/models" and outer.ctx_field == "lmstudio":
                    return self._send(200, {"data": [{"id": "fake-oa", "max_context_length": 99999,
                                                      "loaded_context_length": outer.ctx}]})
                self._send(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/show" and outer.kind == "ollama":
                    return self._send(200, {"model_info": {"fake.context_length": outer.ctx},
                                            "parameters": ""})
                if self.path in ("/api/chat", "/v1/chat/completions"):
                    outer.chat_bodies.append(body)
                    return self._chat(body)
                self._send(404, {})

            def _chat(self, body):
                if outer.delay:
                    time.sleep(outer.delay)
                ans = outer.answers.pop(0) if outer.answers else outer.answer
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                try:
                    if outer.kind == "ollama":
                        for i in range(0, len(ans), 5):
                            self.wfile.write(json.dumps({"message": {"content": ans[i:i+5]},
                                                         "done": False}).encode() + b"\n")
                        self.wfile.write(json.dumps({"message": {"content": ""}, "done": True,
                                                     "done_reason": outer.finish,
                                                     "prompt_eval_count": 7,
                                                     "eval_count": 9}).encode() + b"\n")
                    else:
                        for i in range(0, len(ans), 5):
                            c = {"choices": [{"delta": {"content": ans[i:i+5]}}]}
                            self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
                        self.wfile.write(b"data: " + json.dumps({"choices": [
                            {"delta": {}, "finish_reason": outer.finish}],
                            "usage": {"prompt_tokens": 7, "completion_tokens": 9}}).encode() + b"\n\n")
                        self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def mk():
    made = []

    def _mk(*a, **k):
        f = Fake(*a, **k)
        made.append(f)
        return f
    yield _mk
    for f in made:
        f.close()


# ---------------------------------------------------------------- adapters

def test_ollama_request_shape_and_ctx_from_runtime(mk):
    f = mk("ollama", ctx=8192)
    a = T.OllamaAdapter(f.url)
    r = a.generate("fake-a:1b", "hi", timeout_s=5)
    assert r.status == "ok" and r.text == "hello world"
    assert r.prompt_tokens == 7 and r.completion_tokens == 9 and r.first_token_s is not None
    b = f.chat_bodies[0]
    assert b["think"] is False and b["stream"] is True
    assert b["options"] == {"temperature": 0, "num_predict": 1024, "num_ctx": 8192}


def test_openai_request_shape_and_think_stripping(mk):
    f = mk("openai", answer="<think>secret\nreasoning</think>The answer.")
    a = T.OpenAICompatAdapter(f.url)
    r = a.generate("fake-oa", "hi", timeout_s=5)
    assert r.text == "The answer." and r.raw_chars > len(r.text)
    b = f.chat_bodies[0]
    assert b["temperature"] == 0 and b["max_tokens"] == 1024


@pytest.mark.parametrize("raw,want", [
    ("<think>a</think>X", "X"),
    ("<THINK>a</THINK> X ", "X"),
    ("<think>never closed", ""),
    ("pre</think>Y", "Y"),
    ("A<think>b</think>B<think>c</think>C", "ABC"),
    ("plain", "plain"),
])
def test_strip_think(raw, want):
    assert T.strip_think(raw) == want


def test_unclosed_think_is_empty_status(mk):
    f = mk("openai", answer="<think>still thinking")
    r = T.OpenAICompatAdapter(f.url).generate("fake-oa", "hi", timeout_s=5)
    assert r.status == "empty" and r.text == ""


def test_truncation_status_keeps_text(mk):
    for kind, cls in (("ollama", T.OllamaAdapter), ("openai", T.OpenAICompatAdapter)):
        f = mk(kind, finish="length", answer="cut off mid sen")
        r = cls(f.url).generate("m", "hi", timeout_s=5)
        assert r.status == "truncated" and r.text == "cut off mid sen", kind


@pytest.mark.timing
def test_timeout_is_a_result_not_a_retry(mk):
    f = mk("openai", delay=1.5)
    a = T.OpenAICompatAdapter(f.url)
    t0 = time.monotonic()
    r = a.generate("fake-oa", "hi", timeout_s=0.4)
    assert r.status == "timeout" and time.monotonic() - t0 < 1.2
    assert len(f.chat_bodies) == 1            # no retry


def test_connection_refused_is_error():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = T.OpenAICompatAdapter(f"http://127.0.0.1:{port}").generate("m", "hi", timeout_s=1)
    assert r.status == "error"


# ---------------------------------------------------------------- ctx fit / settings

def test_ctx_sources_per_runtime(mk):
    assert T.OllamaAdapter(mk("ollama", ctx=6000).url).context_length("fake-a:1b") == 6000
    assert T.OpenAICompatAdapter(mk("openai", ctx=7000, ctx_field="props").url,
                                 "llamacpp").context_length("fake-oa") == 7000
    assert T.OpenAICompatAdapter(mk("openai", ctx=5000, ctx_field="lmstudio").url,
                                 "lmstudio").context_length("fake-oa") == 5000
    f = mk("openai", runtime_extra={"max_model_len": 3000})
    assert T.OpenAICompatAdapter(f.url, "vllm").context_length("fake-oa") == 3000
    assert T.OpenAICompatAdapter(mk("openai").url, "openai").context_length("fake-oa") is None


def test_ctx_fit_exclusion(mk):
    f = mk("ollama", ctx=2048)
    a = T.OllamaAdapter(f.url)
    cap = a.max_prompt_chars("fake-a:1b")
    assert cap == int((2048 - 1024 - 256) * 3.0)
    small, big = R.SimpleItem("s", "x" * cap), R.SimpleItem("b", "x" * (cap + 1))
    fits, out = R.partition_by_fit([small, big], cap)
    assert [i.id for i in fits] == ["s"] and [i.id for i in out] == ["b"]
    assert R.partition_by_fit([small, big], None) == ([small, big], [])   # unknown: exclude nothing
    assert T.max_prompt_chars(None) is None and T.max_prompt_chars(1000) == 0


def test_settings_hash_tracks_each_setting():
    base = dict(temperature=0, ctx=4096, max_tokens=1024, think=False)
    h = T.settings_hash(**base)
    assert h == T.settings_hash(**base)
    for k, v in (("temperature", 0.7), ("ctx", 8192), ("max_tokens", 512), ("think", True)):
        assert T.settings_hash(**{**base, k: v}) != h, k


# ---------------------------------------------------------------- digest

def test_digest_present_and_absent(mk):
    assert T.OllamaAdapter(mk("ollama", digest="abc123").url).digest("fake-a:1b") == "sha256:abc123"
    assert T.OllamaAdapter(mk("ollama", digest=None).url).digest("fake-a:1b") is None
    assert T.OllamaAdapter(mk("ollama").url).digest("not-installed") is None
    # id alone is not a digest
    assert T.OpenAICompatAdapter(mk("openai").url).digest("fake-oa") is None
    f = mk("openai", runtime_extra={"size": 123})
    d1 = T.OpenAICompatAdapter(f.url).digest("fake-oa")
    assert d1 and d1.startswith("meta:sha256:")
    f2 = mk("openai", runtime_extra={"size": 124})
    assert T.OpenAICompatAdapter(f2.url).digest("fake-oa") != d1
    # llama.cpp: /props supplies model_path
    assert T.OpenAICompatAdapter(mk("openai", ctx_field="props").url, "llamacpp").digest("fake-oa")


# ---------------------------------------------------------------- discovery / loopback

def test_identify_and_discover_override(mk):
    assert T.discover(mk("ollama").url)[0].runtime == "ollama"
    e = T.discover(mk("openai", ctx_field="props").url)[0]
    assert (e.runtime, e.models) == ("llamacpp", ["fake-oa"])
    assert T.discover(mk("openai", runtime_extra={"max_model_len": 9}).url)[0].runtime == "vllm"
    assert T.discover(mk("openai", ctx_field="lmstudio").url)[0].runtime == "lmstudio"
    assert T.discover(mk("openai").url)[0].runtime == "openai"


def test_discover_scan_uses_model_discovery_and_probes_ports(mk, monkeypatch):
    from llm_router import model_discovery
    oll, oai = mk("ollama"), mk("openai", ctx_field="props")
    monkeypatch.setenv("OLLAMA_HOST", oll.url)
    monkeypatch.setattr(model_discovery, "serves_generate", lambda m: True, raising=False)
    monkeypatch.setattr(model_discovery, "_is_completion_model", lambda n: True)
    got = T.discover(ports=((oai.port, "llamacpp"), (1, "vllm")))
    assert [(e.runtime, e.models) for e in got] == [("ollama", ["fake-a:1b"]),
                                                    ("llamacpp", ["fake-oa"])]


def test_non_loopback_refused_and_never_dialled(monkeypatch):
    dialled = []
    monkeypatch.setattr(T.http.client.HTTPConnection, "connect",
                        lambda self: dialled.append(self.host))
    with pytest.raises(T.RuntimeError_):
        T.OllamaAdapter("http://192.0.2.1:11434")
    with pytest.raises(T.RuntimeError_):
        T.discover("http://example.com:8000")
    assert dialled == []
    assert T.is_loopback("localhost:1234") and T.is_loopback("http://127.0.0.1:8000/v1")
    assert not T.is_loopback("http://10.0.0.5:8000")


def test_only_loopback_clients_seen(mk):
    f = mk("ollama")
    T.OllamaAdapter(f.url).generate("fake-a:1b", "hi", timeout_s=5)
    T.discover(f.url)
    assert f.hosts == {"127.0.0.1"}


# ---------------------------------------------------------------- runner

def items(n, size=10):
    return [R.SimpleItem(f"i{k}", "p" * size) for k in range(n)]


def test_run_writes_0600_results(mk, tmp_path):
    f = mk("ollama")
    rn = R.Runner(T.OllamaAdapter(f.url), "fake-a:1b", items(4), tmp_path / "run", runtime="ollama")
    out = rn.run()
    assert out.status == "complete" and out.done == 4 and out.failures == 0
    rp = tmp_path / "run" / "results.jsonl"
    assert stat.S_IMODE(rp.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "run").stat().st_mode) == 0o700
    meta = json.loads((tmp_path / "run" / "meta.json").read_text())
    assert meta["digest"] == "sha256:abc123" and meta["settings_hash"].startswith("sha256:")
    assert len(rp.read_text().splitlines()) == 4


def test_resume_after_interruption(mk, tmp_path):
    f = mk("ollama")
    seen = []

    def boom(rec):
        seen.append(rec["item_id"])
        if len(seen) == 2:
            raise KeyboardInterrupt

    d = tmp_path / "run"
    first = R.Runner(T.OllamaAdapter(f.url), "fake-a:1b", items(6), d, on_result=boom, probe_n=1).run()
    assert first.status == "interrupted" and first.done == 2
    n_before = len(f.chat_bodies)
    second = R.Runner(T.OllamaAdapter(f.url), "fake-a:1b", items(6), d, probe_n=1).run()
    assert second.status == "complete" and second.done == 6
    assert len(f.chat_bodies) - n_before == 4          # only the missing items were generated
    ids = [json.loads(ln)["item_id"] for ln in (d / "results.jsonl").read_text().splitlines()]
    assert sorted(ids) == sorted(f"i{k}" for k in range(6))


def test_resume_ignores_torn_last_line(mk, tmp_path):
    f = mk("ollama")
    d = tmp_path / "run"
    R.Runner(T.OllamaAdapter(f.url), "fake-a:1b", items(3), d).run()
    rp = d / "results.jsonl"
    lines = rp.read_text().splitlines()
    rp.write_text("\n".join(lines[:2]) + "\n" + lines[2][:20])          # crash mid-write
    out = R.Runner(T.OllamaAdapter(f.url), "fake-a:1b", items(3), d).run()
    assert out.status == "complete" and out.done == 3


def test_resume_refuses_changed_model_or_settings(mk, tmp_path):
    f = mk("ollama", digest="aaa")
    d = tmp_path / "run"
    R.Runner(T.OllamaAdapter(f.url), "fake-a:1b", items(2), d).run()
    f.digest = "bbb"                                      # model replaced under the same name
    with pytest.raises(R.RunMismatch):
        R.Runner(T.OllamaAdapter(f.url), "fake-a:1b", items(2), d).run()


def test_early_stop_helper_bar_is_a_parameter():
    assert not R.early_stop_reached(70, 14, 56)           # 56 passes still reachable
    assert R.early_stop_reached(70, 15, 56)               # design §3: 15th failure
    assert R.early_stop_reached(140, 29, 112) and not R.early_stop_reached(140, 28, 112)
    assert not R.early_stop_reached(70, 70, None)         # bar unset -> never


def test_runner_early_stops_on_failures(mk, tmp_path):
    f = mk("openai", answer="")                           # every answer empty -> failure
    rn = R.Runner(T.OpenAICompatAdapter(f.url), "fake-oa", items(10), tmp_path / "r",
                  min_pass=8, probe_n=1)
    out = rn.run()
    assert out.status == "early_stop" and out.failures == 3 and out.done == 3
    assert len(f.chat_bodies) == 3


def test_runner_pass_all_no_early_stop(mk, tmp_path):
    f = mk("openai")
    out = R.Runner(T.OpenAICompatAdapter(f.url), "fake-oa", items(5), tmp_path / "r",
                   min_pass=4).run()
    assert out.status == "complete" and out.failures == 0


def test_timeout_item_recorded_no_retry(mk, tmp_path):
    f = mk("openai", delay=1.0)
    out = R.Runner(T.OpenAICompatAdapter(f.url), "fake-oa", items(2), tmp_path / "r",
                   base_timeout_s=0.3, probe_n=1).run()
    assert [r["status"] for r in out.records] == ["timeout", "timeout"] and out.failures == 2
    assert len(f.chat_bodies) == 2


def test_probe_rate_estimate_and_timeout_scaling(mk, tmp_path):
    f = mk("ollama", delay=0.05)
    rn = R.Runner(T.OllamaAdapter(f.url), "fake-a:1b", items(10, size=300), tmp_path / "r")
    rate = rn.probe_rate()
    assert rate is not None and rate.n == 3
    est = rn.estimate_remaining_s()
    assert est is not None and est > 0
    assert len(f.chat_bodies) == 3                        # probe items are real, kept
    rn.run()
    assert len(f.chat_bodies) == 10                       # ...and not regenerated
    assert R.item_timeout(None) == 240.0
    slow = R.Rate(3, 1.0, 1.0, 120.0, 100.0)
    assert R.item_timeout(slow) == 480.0
    assert R.item_timeout(R.Rate(3, 1.0, 1.0, 9999.0, 1.0)) == 960.0     # capped at 4x
    assert R.item_timeout(R.Rate(3, 1.0, 1.0, 5.0, 1.0)) == 240.0        # never below base
    assert R.estimate_seconds(items(2), None) is None                    # unknown stays None


def test_lock_held_refuses(mk, tmp_path):
    import fcntl
    lock = tmp_path / "ollama.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        f = mk("ollama")
        with pytest.raises(R.LockHeld):
            R.Runner(T.OllamaAdapter(f.url), "fake-a:1b", items(1), tmp_path / "r",
                     lock_path=lock).run()
        assert f.chat_bodies == []
    finally:
        os.close(fd)
    f2 = mk("ollama")
    out = R.Runner(T.OllamaAdapter(f2.url), "fake-a:1b", items(1), tmp_path / "r2",
                   lock_path=lock).run()
    assert out.status == "complete"


def test_no_hardcoded_model_names_in_source():
    import re
    from pathlib import Path
    for p in (Path(T.__file__), Path(R.__file__)):
        src = p.read_text()
        assert not re.search(r"\b(qwen|llama\d|gemma|mistral|phi\d|deepseek|gpt-)", src, re.I), p
