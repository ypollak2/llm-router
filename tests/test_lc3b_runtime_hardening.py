"""LC-3b: hardening of the local-check runtimes/runner (review of #663), against fake
loopback servers on ephemeral ports. No real Ollama, no non-loopback host."""
from __future__ import annotations

import json
import os
import socket
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from llm_router import model_discovery
from llm_router.local_check import runner as R
from llm_router.local_check import runtimes as T


class Srv:
    """Scriptable fake: ``fn(method, path, body) -> (code, payload)``.
    payload: dict/list -> JSON body, bytes -> raw body, None -> close without answering."""

    def __init__(self, fn):
        self.fn, self.calls = fn, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self, method):
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n) if n else b""
                body = json.loads(raw) if raw else None
                outer.calls.append((method, self.path))
                code, payload = outer.fn(method, self.path, body)
                if payload is None:
                    self.close_connection = True
                    self.connection.close()
                    return
                b = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def count(self, path):
        return sum(1 for _, p in self.calls if p == path)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def srv():
    made = []

    def _mk(fn):
        s = Srv(fn)
        made.append(s)
        return s
    yield _mk
    for s in made:
        s.close()


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch):
    monkeypatch.setattr(T, "PROBE_RETRY_DELAY_S", 0.0)


def ollama_fn(show_fail=0, ctx=4096, chat=None):
    """Ollama fake whose /api/show drops the first ``show_fail`` connections."""
    state = {"n": 0}

    def fn(method, path, body):
        if path == "/api/show":
            state["n"] += 1
            if state["n"] <= show_fail:
                return 0, None
            return 200, {"model_info": {"x.context_length": ctx}, "parameters": ""}
        if path == "/api/tags":
            return 200, {"models": [{"name": "m:1b", "digest": "abc"}]}
        if path == "/api/chat":
            return 200, chat or (json.dumps({"message": {"content": "hi"}, "done": False}) + "\n"
                                 + json.dumps({"done": True, "done_reason": "stop"}) + "\n").encode()
        return 404, {}
    return fn


def items(n):
    return [R.SimpleItem(f"i{k}", "p" * 10) for k in range(n)]


# ------------------------------------------------------------ probe failure caching

def test_probe_failure_not_cached_and_retried(srv):
    s = srv(ollama_fn(show_fail=2))               # first call: both attempts fail
    a = T.OllamaAdapter(s.url)
    with pytest.raises(T.ProbeFailed):
        a.context_length("m:1b")
    assert a.context_length("m:1b") == 4096        # not frozen as None
    assert a.context_length("m:1b") == 4096
    assert s.count("/api/show") == 3               # 2 failed attempts + 1 cached success


def test_single_transient_failure_absorbed_by_retry(srv):
    s = srv(ollama_fn(show_fail=1))
    assert T.OllamaAdapter(s.url).context_length("m:1b") == 4096


def test_settings_hash_does_not_drift_on_transient_failure(srv):
    ok = T.OllamaAdapter(srv(ollama_fn()).url).settings_hash("m:1b")
    flaky = T.OllamaAdapter(srv(ollama_fn(show_fail=2)).url)
    with pytest.raises(T.ProbeFailed):
        flaky.settings_hash("m:1b")                # never silently a ctx=None hash
    assert flaky.settings_hash("m:1b") == ok


def test_answered_without_ctx_is_none_and_cached(srv):
    s = srv(lambda m, p, b: (404, {}))             # runtime answered: no such model info
    a = T.OllamaAdapter(s.url)
    assert a.context_length("m") is None and a.context_length("m") is None
    assert s.count("/api/show") == 1


def test_openai_get_failure_not_cached(srv):
    state = {"n": 0}

    def fn(m, p, b):
        if p == "/props":
            state["n"] += 1
            if state["n"] <= 2:
                return 0, None
            return 200, {"default_generation_settings": {"n_ctx": 7000}}
        return 404, {}
    a = T.OpenAICompatAdapter(srv(fn).url, "llamacpp")
    with pytest.raises(T.ProbeFailed):
        a.context_length("x")
    assert a.context_length("x") == 7000


def test_digest_failure_raises_not_none(srv):
    s = srv(lambda m, p, b: (0, None))
    with pytest.raises(T.ProbeFailed):
        T.OllamaAdapter(s.url).digest("m:1b")


def test_runner_refuses_to_start_when_ctx_unknown(srv, tmp_path):
    s = srv(ollama_fn(show_fail=99))
    with pytest.raises(R.RunStartError):
        R.Runner(T.OllamaAdapter(s.url), "m:1b", items(2), tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_runner_init_network_failure_is_run_start_error(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(R.RunStartError):
        R.Runner(T.OllamaAdapter(f"http://127.0.0.1:{port}"), "m", items(1), tmp_path / "r")


# ------------------------------------------------------------ validation / error mapping

@pytest.mark.parametrize("url", ["ftp://localhost", "file:///etc/passwd", "gopher://127.0.0.1"])
def test_scheme_must_be_http_or_https(url):
    with pytest.raises(ValueError):
        T.normalize_base(url)
    assert T.is_loopback(url) is False
    with pytest.raises(T.RuntimeError_):
        T.OllamaAdapter(url)
    assert T.discover(url) == []
    assert T.normalize_base("https://localhost:1/v1") == "https://localhost:1"


@pytest.mark.parametrize("url", ["http://127.0.0.1:abc", "http://127.0.0.1:99999", "http://127.0.0.1:0"])
def test_bad_port_never_raises_from_probes(url):
    assert T._json_request(url, "GET", "/api/tags") is None
    assert T.identify(url) is None
    assert T.discover(url) == []
    if url.endswith(("abc", "99999")):
        with pytest.raises(T.RuntimeError_):
            T.OllamaAdapter(url)


def test_generate_maps_bad_endpoint_and_probe_failure_to_error(srv):
    a = T.OpenAICompatAdapter("http://127.0.0.1:1")
    a.base_url = "http://127.0.0.1:abc"             # bypass ctor to reach the request path
    r = a.generate("m", "hi", timeout_s=1)
    assert r.status == "error" and "ValueError" in r.error
    # _request() probes ctx over the network: its failure is an error result too
    o = T.OllamaAdapter(srv(ollama_fn(show_fail=99)).url)
    r = o.generate("m:1b", "hi", timeout_s=2)
    assert r.status == "error" and "ProbeFailed" in r.error


def test_generate_catches_unexpected_exception(srv, monkeypatch):
    a = T.OllamaAdapter(srv(ollama_fn()).url)
    monkeypatch.setattr(a, "_parse_stream", lambda resp: (_ for _ in ()).throw(KeyError("boom")))
    r = a.generate("m:1b", "hi", timeout_s=2)
    assert r.status == "error" and "KeyError" in r.error


def test_http_error_status_and_garbage_stream_are_results(srv):
    r = T.OllamaAdapter(srv(lambda m, p, b: (200, {"model_info": {}}) if p == "/api/show"
                            else (500, b"nope")).url).generate("m", "hi", timeout_s=2)
    assert r.status == "error" and "500" in r.error
    junk = srv(ollama_fn(chat=b"{not json\n"))
    assert T.OllamaAdapter(junk.url).generate("m:1b", "hi", timeout_s=2).status == "error"


def test_non_dict_json_stream_lines_ignored(srv):
    chat = (b'[1, 2]\n"str"\n42\nnull\n'
            + json.dumps({"message": {"content": "ok"}, "done": False}).encode() + b"\n"
            + json.dumps({"message": "bad-shape", "done": True, "done_reason": "stop"}).encode() + b"\n")
    r = T.OllamaAdapter(srv(ollama_fn(chat=chat)).url).generate("m:1b", "hi", timeout_s=2)
    assert r.status == "ok" and r.text == "ok"

    sse = (b"data: [1]\n\ndata: 7\n\n"
           b'data: {"choices": ["x", {"delta": "oops"}, {"delta": {"content": "yes"}}], "usage": 5}\n\n'
           b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n')

    def fn(m, p, b):
        return (200, sse) if p == "/v1/chat/completions" else (404, {})
    r = T.OpenAICompatAdapter(srv(fn).url).generate("m", "hi", timeout_s=2)
    assert r.status == "ok" and r.text == "yes"


# ------------------------------------------------------------ truncated-empty

def test_truncated_with_no_text_is_empty_and_a_failure(srv):
    chat = (json.dumps({"message": {"content": "<think>endless"}, "done": False}) + "\n"
            + json.dumps({"done": True, "done_reason": "length"}) + "\n").encode()
    r = T.OllamaAdapter(srv(ollama_fn(chat=chat)).url).generate("m:1b", "hi", timeout_s=2)
    assert r.status == "empty" and r.text == ""
    assert R.default_is_failure(r.to_dict()) is True
    assert R.default_is_failure({"status": "truncated", "text": "  "}) is True
    assert R.default_is_failure({"status": "truncated", "text": "cut"}) is False
    assert R.default_is_failure({"status": "ok", "text": "x"}) is False
    assert R.default_is_failure({"status": "timeout"}) is True


# ------------------------------------------------------------ discover

def test_discover_allow_remote_is_threaded_and_default_refuses(monkeypatch):
    with pytest.raises(T.RuntimeError_):
        T.discover("http://example.invalid:11434")  # remote without opt-in is refused
    seen = {}

    def fake_identify(base, *, allow_remote=False):
        seen["identify"] = allow_remote
        return "vllm"

    def fake_list(base, rt, *, allow_remote=False):
        seen["list"] = allow_remote
        return ["m"]
    monkeypatch.setattr(T, "identify", fake_identify)
    monkeypatch.setattr(T, "list_models", fake_list)
    out = T.discover("example.invalid:8000/v1", allow_remote=True)
    assert out == [T.Endpoint("vllm", "http://example.invalid:8000", ["m"])]
    assert seen == {"identify": True, "list": True}


def test_probe_ollama_names_drop_embedding_models(monkeypatch):
    monkeypatch.setattr(model_discovery, "_ollama_url", lambda: "http://127.0.0.1:1")
    monkeypatch.setattr(model_discovery, "probe_ollama",
                        lambda: ["qwen:7b", "nomic-embed-text", "BGE-large", "all-minilm"])
    found = T.discover(ports=())
    assert [e.models for e in found] == [["qwen:7b"]]


# ------------------------------------------------------------ run dir

def test_chmod_only_on_dir_the_runner_creates(tmp_path):
    pre = tmp_path / "existing"
    pre.mkdir()
    os.chmod(pre, 0o755)
    R.RunStore(pre).open({"a": 1})
    assert stat.S_IMODE(pre.stat().st_mode) == 0o755          # not ours: untouched
    new = tmp_path / "fresh"
    R.RunStore(new).open({"a": 1})
    assert stat.S_IMODE(new.stat().st_mode) == 0o700
    assert stat.S_IMODE((new / "meta.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((new / "results.jsonl").stat().st_mode) == 0o600


def test_meta_write_is_atomic(tmp_path, monkeypatch):
    d = tmp_path / "run"
    st = R.RunStore(d)
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(R.RunDirError):
        st.open({"a": 1})
    assert not (d / "meta.json").exists()                     # never a torn meta.json
    assert [p.name for p in d.iterdir()] == []                # temp cleaned up
    monkeypatch.setattr(os, "replace", real)
    st.open({"a": 1})
    assert json.loads((d / "meta.json").read_text()) == {"a": 1}
    assert [p.name for p in d.iterdir() if p.name.endswith(".tmp")] == []


@pytest.mark.parametrize("junk", ["", "{\"a\": ", "[1]", "\x00\x01"])
def test_torn_meta_is_a_clear_run_mismatch(tmp_path, junk):
    d = tmp_path / "run"
    R.RunStore(d).open({"a": 1})
    (d / "meta.json").write_text(junk)
    with pytest.raises(R.RunMismatch, match="meta.json"):
        R.RunStore(d).open({"a": 1})


def test_symlinked_run_files_refused(tmp_path):
    target = tmp_path / "victim"
    target.write_text("keep")
    d = tmp_path / "run"
    d.mkdir()
    (d / "results.jsonl").symlink_to(target)
    with pytest.raises(R.RunDirError, match="symlink"):
        R.RunStore(d).open({"a": 1})
    assert target.read_text() == "keep"

    d2 = tmp_path / "run2"
    st = R.RunStore(d2)
    st.open({"a": 1})
    os.unlink(d2 / "results.jsonl")
    (d2 / "results.jsonl").symlink_to(target)
    with pytest.raises(R.RunDirError, match="symlink"):
        st.append({"item_id": "x"})                            # O_NOFOLLOW on the append path
    with pytest.raises(R.RunDirError, match="symlink"):
        st.load()
    assert target.read_text() == "keep"


def test_symlink_refused_without_o_nofollow(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_NOFOLLOW", 0)                     # e.g. Windows
    target = tmp_path / "victim"
    target.write_text("keep")
    st = R.RunStore(tmp_path / "run")
    st.open({"a": 1})
    os.unlink(tmp_path / "run" / "results.jsonl")
    (tmp_path / "run" / "results.jsonl").symlink_to(target)
    with pytest.raises(R.RunDirError, match="symlink"):
        st.append({"item_id": "x"})
    with pytest.raises(R.RunDirError, match="symlink"):
        st.load()
    assert target.read_text() == "keep"


# ------------------------------------------------------------ resume early stop

def test_resume_past_early_stop_runs_no_more_items(srv, tmp_path):
    s = srv(ollama_fn())
    rn = R.Runner(T.OllamaAdapter(s.url), "m:1b", items(5), tmp_path / "run", min_pass=4,
                  probe_n=1)
    rn.store.open(rn.meta)
    for k in range(2):                                         # 5 - 2 failures < 4: out of reach
        rn.store.append({"item_id": f"i{k}", "status": "error"})
    out = rn.run()
    assert out.status == "early_stop" and out.done == 2
    assert s.count("/api/chat") == 0                           # no extra item was run
    # probe_rate honours it too
    R.Runner(T.OllamaAdapter(s.url), "m:1b", items(5), tmp_path / "run", min_pass=4,
             probe_n=9).probe_rate()
    assert s.count("/api/chat") == 0


# ------------------------------------------------------------ wrong-shape 200 bodies

BAD_BODIES = [[1], {"models": [1]}, {"models": "x"}, {"data": [1]}, {"data": "x"},
              {"data": [{"id": 5}]}, {"models": [{"name": 5, "digest": 5}]}, "str", b"null", 7,
              {"models": {"a": []}, "data": {"b": 1}, "model_info": [1], "parameters": [1],
               "default_generation_settings": 3},
              {"model_info": {"x": [1]}, "parameters": 5, "meta": 4},
              {"data": [{"id": "m", "meta": 5, "loaded_context_length": "x"}],
               "default_generation_settings": [1]},
              {"data": [{"id": "m", "max_model_len": [1], "size": {"a": []}}]}]


@pytest.mark.parametrize("body", BAD_BODIES, ids=lambda b: str(b)[:40])
def test_wrong_shape_200_never_raises(srv, body):
    s = srv(lambda m, p, b: (200, body))
    T.identify(s.url)
    for rt in ("ollama", "lmstudio", "vllm", "llamacpp", "openai"):
        assert isinstance(T.list_models(s.url, rt), list)
    a = T.OllamaAdapter(s.url)
    a.context_length("m")
    a.digest("m")
    for rt in ("lmstudio", "vllm", "llamacpp", "openai"):
        o = T.OpenAICompatAdapter(s.url, runtime=rt)
        o.context_length("m")
        o.digest("m")
        o.settings_hash("m")
    a.settings_hash("m")
    a.max_prompt_chars("m")
    found = T.discover(s.url)
    assert isinstance(found, list)


@pytest.mark.parametrize("body", [[1], "str", b"null", 7, {"models": "x"}, {"data": "x"},
                                  {"data": {"id": "m"}}, {"x": 1}])
def test_wrong_shape_is_not_a_runtime(srv, body):
    s = srv(lambda m, p, b: (200, body))
    assert T.identify(s.url) is None
    assert T.discover(s.url) == []


def test_wrong_shape_values_are_none_or_empty(srv):
    s = srv(lambda m, p, b: (200, {"models": [1], "data": [1]}))
    assert T.list_models(s.url, "ollama") == [] and T.list_models(s.url, "openai") == []
    assert T.OllamaAdapter(s.url).digest("m") is None
    assert T.OllamaAdapter(s.url).context_length("m") is None
    for rt in ("lmstudio", "vllm", "llamacpp"):
        o = T.OpenAICompatAdapter(s.url, runtime=rt)
        assert o.context_length("m") is None and o.digest("m") is None


# ------------------------------------------------------------ status codes

@pytest.mark.parametrize("code", [500, 503, 429, 408])
def test_transient_status_is_probe_failed_not_cached(srv, code):
    state = {"n": 0}

    def fn(m, p, b):
        if p == "/api/show":
            state["n"] += 1
            if state["n"] <= 2:
                return code, {}
            return 200, {"model_info": {"x.context_length": 2048}, "parameters": ""}
        return 404, {}
    s = srv(fn)
    a = T.OllamaAdapter(s.url)
    with pytest.raises(T.ProbeFailed):
        a.context_length("m")                      # first call: both attempts hit the status
    assert a._show == {}                           # nothing cached
    assert a.context_length("m") == 2048
    assert s.count("/api/show") == 3


def test_503_on_show_makes_settings_hash_raise(srv):
    a = T.OllamaAdapter(srv(lambda m, p, b: (503, {})).url)
    with pytest.raises(T.ProbeFailed):
        a.settings_hash("m")
    with pytest.raises(T.ProbeFailed):
        a.max_prompt_chars("m")


@pytest.mark.parametrize("code", [400, 401, 403, 404, 405])
def test_4xx_is_a_real_none_and_cached(srv, code):
    s = srv(lambda m, p, b: (code, {}))
    a = T.OllamaAdapter(s.url)
    assert a.context_length("m") is None
    assert a.context_length("m") is None
    assert s.count("/api/show") == 1               # cached: an answer, not a failure


def test_runner_refuses_start_on_503(srv, tmp_path):
    a = T.OllamaAdapter(srv(lambda m, p, b: (503, {})).url)
    with pytest.raises(R.RunStartError):
        R.Runner(a, "m", items(1), tmp_path / "run")
    assert not (tmp_path / "run").exists()
