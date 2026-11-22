"""PR-3 cls_embed: the backend is off by default, guarded, budgeted, text-free. A fake HTTP server only; the real
Ollama servers are never contacted."""
from __future__ import annotations

import http.server
import json
import threading
import time

import pytest

from llm_router import failopen
from llm_router.proxy import cls_embed

CANARY = "CANARY-SECRET-TEXT-xyzzy-91"


class _Fake:
    def __init__(self, vec=(1.0, 0.0), delay=0.0, status=200):
        self.vec, self.delay, self.status = list(vec), delay, status
        self.bodies: list[dict] = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D102
                pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length", 0))
                outer.bodies.append(json.loads(self.rfile.read(n)))
                time.sleep(outer.delay)
                self.send_response(outer.status)
                self.end_headers()
                try:
                    self.wfile.write(json.dumps({"embeddings": [outer.vec]}).encode())
                except OSError:
                    pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _artifact(path, dim=2, tamper=False):
    raw = {"embedding_model": "m", "dim": dim, "temperature": 0.1, "confidence_floor": 0.6,
           "heads": {"local": [[1.0] + [0.0] * (dim - 1)], "claude": [[0.0, 1.0] + [0.0] * (dim - 2)]},
           "provenance": {"n": 0}}
    raw["sha256"] = cls_embed._canonical_sha(raw)
    if tamper:
        raw["temperature"] = 0.2
    path.write_text(json.dumps(raw))
    return raw["sha256"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_CLS_EMBED", "shadow")
    monkeypatch.delenv("LLM_ROUTER_LOCAL_MODEL_URL", raising=False)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", "http://127.0.0.1:11434")
    art = tmp_path / "head.json"
    monkeypatch.setenv("LLM_ROUTER_CLS_EMBED_ARTIFACT", str(art))
    cls_embed.reset_state()
    fake = _Fake()
    monkeypatch.setenv("LLM_ROUTER_CLS_EMBED_URL", fake.url)
    yield tmp_path, art, fake
    fake.close()
    cls_embed.reset_state()


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_CLS_EMBED", raising=False)
    assert not cls_embed.enabled()
    assert cls_embed.classify("x").source == "disabled"
    assert cls_embed.warm_up() == "disabled"
    monkeypatch.setenv("LLM_ROUTER_CLS_EMBED", "on")
    assert not cls_embed.enabled()


def test_decides_and_keep_alive_sent(env):
    _, art, fake = env
    sha = _artifact(art)
    v = cls_embed.classify("hello")
    assert (v.tier, v.source, v.artifact_sha) == ("local", "embed", sha)
    assert v.decided and v.score is not None and v.score > 0.9
    assert fake.bodies[0]["keep_alive"] == "30m" and fake.bodies[0]["model"] == "nomic-embed-text"
    fake.vec = [0.0, 1.0]
    assert cls_embed.classify("hello").tier == "claude"


def test_below_floor_abstains(env):
    _, art, fake = env
    _artifact(art)
    fake.vec = [1.0, 1.0]
    v = cls_embed.classify("hello")
    assert v.tier is None and v.source == "below_floor"


@pytest.mark.parametrize("url", ["http://127.0.0.1:11435", "http://localhost:11435", "http://10.0.0.5:11434",
                                 "http://example.com:11434", "http://127.0.0.1:9", ""])
def test_guard_refuses(env, monkeypatch, url):
    _, art, fake = env
    _artifact(art)
    if url == "http://127.0.0.1:9":  # equals main_url: the main model's server is never the embed server
        monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL_URL", url)
        monkeypatch.setenv("LLM_ROUTER_CLS_EMBED_URL", url)
    elif url:
        monkeypatch.setenv("LLM_ROUTER_CLS_EMBED_URL", url)
    else:  # empty -> default url (:11434) is allowed; point main at it to prove the main-url refusal
        monkeypatch.delenv("LLM_ROUTER_CLS_EMBED_URL")
        monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL_URL", "http://127.0.0.1:11434")
    assert not cls_embed.url_ok(cls_embed.embed_url())
    assert cls_embed.classify("hello").source == "guard"
    assert fake.bodies == []


def test_guard_allows_loopback_non_main(env, monkeypatch):
    assert not cls_embed.url_ok("http://127.0.0.1:11434")  # one-server setup: embed would share the main server
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL_URL", "http://127.0.0.1:11435")
    assert cls_embed.url_ok("http://127.0.0.1:11434")


@pytest.mark.timing
def test_timeout_abstains_within_budget(env, monkeypatch):
    _, art, fake = env
    _artifact(art)
    fake.delay = 1.0
    t0 = time.monotonic()
    v = cls_embed.classify("hello")
    assert v.source == "timeout" and v.tier is None
    assert time.monotonic() - t0 < 0.6


def test_cold_first_call_over_budget_then_warm(env, monkeypatch):
    _, art, fake = env
    _artifact(art)
    monkeypatch.setattr("llm_router.toolkit.adapters.ollama.ensure_fits", lambda *a, **k: None)
    fake.delay = 0.4  # cold load
    assert cls_embed.classify("x").source == "timeout"
    assert cls_embed.warm_up() == "ok"  # warm-up has its own long timeout
    fake.delay = 0.0
    assert cls_embed.classify("x").source == "embed"
    assert fake.bodies[-2]["input"] == "warm-up"


def test_warm_up_refused_by_ensure_fits(env, monkeypatch):
    _, art, fake = env

    def boom(*a, **k):
        raise RuntimeError("no memory")

    monkeypatch.setattr("llm_router.toolkit.adapters.ollama.ensure_fits", boom)
    assert cls_embed.warm_up() == "refused"
    assert fake.bodies == []


def test_warm_up_guard_and_kill(env, monkeypatch):
    tmp, _, fake = env
    monkeypatch.setenv("LLM_ROUTER_CLS_EMBED_URL", "http://127.0.0.1:11435")
    assert cls_embed.warm_up() == "guard"
    monkeypatch.setenv("LLM_ROUTER_CLS_EMBED_URL", fake.url)
    (tmp / "cls_embed.off").write_text("")
    assert cls_embed.warm_up() == "killed"
    assert fake.bodies == []


def test_kill_file_per_call(env):
    tmp, art, fake = env
    _artifact(art)
    assert cls_embed.classify("a").source == "embed"
    (tmp / "cls_embed.off").write_text("")
    assert cls_embed.classify("a").source == "killed"
    n = len(fake.bodies)
    assert cls_embed.classify("a").source == "killed" and len(fake.bodies) == n


def test_no_artifact_abstains_without_embedding(env):
    _, art, fake = env
    v = cls_embed.classify("hello")
    assert v.source == "no_artifact" and v.tier is None and fake.bodies == []


def test_hash_mismatch_abstains_and_failopen(env):
    _, art, fake = env
    _artifact(art, tamper=True)
    before = failopen.snapshot().by_code.get(cls_embed.FO_ARTIFACT, 0)
    v = cls_embed.classify("hello")
    assert v.source == "bad_artifact" and fake.bodies == []
    assert failopen.snapshot().by_code.get(cls_embed.FO_ARTIFACT, 0) == before + 1
    cls_embed.classify("hello")  # same file version: counted once
    assert failopen.snapshot().by_code.get(cls_embed.FO_ARTIFACT, 0) == before + 1


def test_dim_mismatch_abstains(env):
    _, art, fake = env
    _artifact(art)
    fake.vec = [1.0, 0.0, 0.0]
    assert cls_embed.classify("x").source == "dim"


def test_breaker_opens_after_five_errors(env, monkeypatch):
    _, art, fake = env
    _artifact(art)
    fake.status = 500
    for _ in range(5):
        assert cls_embed.classify("x").source == "error"
    n = len(fake.bodies)
    assert cls_embed.classify("x").source == "breaker" and len(fake.bodies) == n
    t = cls_embed._clock() + 61
    monkeypatch.setattr(cls_embed, "_clock", lambda: t)
    fake.status = 200
    assert cls_embed.classify("x").source != "breaker"


def test_queue_full_abstains(env):
    _, art, fake = env
    _artifact(art)
    fake.delay = 0.15
    out: list = []
    ts = [threading.Thread(target=lambda: out.append(cls_embed.classify("x"))) for _ in range(7)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    srcs = [v.source for v in out]
    assert "queue_full" in srcs and max(v.queue_depth for v in out) <= cls_embed.MAX_IN_FLIGHT


def test_no_text_in_verdict_or_failopen(env):
    tmp, art, fake = env
    _artifact(art)
    fake.status = 500
    vs = [cls_embed.classify(CANARY), cls_embed.classify(CANARY)]
    fake.status, fake.vec = 200, [1.0, 0.0]
    vs.append(cls_embed.classify(CANARY))
    for v in vs:
        assert CANARY not in json.dumps(v.as_record()) and CANARY not in repr(v)
    assert CANARY not in failopen.store_path().read_text() if failopen.store_path().exists() else True
    assert set(vs[-1].as_record()) == {"cls_emb_tier", "cls_emb_source", "cls_emb_ms", "cls_emb_score",
                                       "cls_emb_margin", "cls_emb_artifact_sha", "cls_emb_queue_depth"}


def test_empty_text_abstains(env):
    _, art, fake = env
    _artifact(art)
    assert cls_embed.classify("  ").source == "no_text" and fake.bodies == []


def test_does_not_import_chat_classifier():
    import inspect
    src = inspect.getsource(cls_embed)
    assert "import local_classifier" not in src and "from llm_router.local_classifier" not in src
