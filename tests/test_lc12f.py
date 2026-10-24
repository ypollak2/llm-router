"""LC-12f: the route-time remote flag and the pre-dispatch live check close the copy-over window; #713 review nits.

The window: a user passes local-check for ``mine:latest`` (digest A), then runs
``ollama cp gpt-oss:120b-cloud mine:latest``. Until ``discovery.json`` refreshes, the route-time digest check
still sees A. Covered here:

* discovery writers store ``remote`` (from ``remote_host`` / ``remote_model``) and the current digest per model;
* a cached ``remote: true`` makes a model unroutable at route time (no network);
* a pass-only text job re-reads the model's live ``/api/tags`` row (one localhost GET, short timeout) and hands
  back when the digest differs from the pass, remote metadata is present, or the daemon does not answer;
* with ``LLM_ROUTER_LOCAL_TEXT_JOBS=1`` ordinary routing is unchanged (no live check).

Tmp homes, fake ``/api/tags`` answers, fake HTTP connections, ``providers.call_llm`` guarded; no network, no model.
"""
# ruff: noqa: F811  (the `world` / `e2e` fixtures are imported and used as arguments)
from __future__ import annotations

import asyncio
import builtins
import http.client
import io
import json
import socket
import time

import pytest

from llm_router import counter_registry, discover, model_discovery, paths, providers
from llm_router import discovery_rescan as scan
from llm_router import eligibility_routing as er
from llm_router import router
from llm_router.judge_select import tag_metadata
from llm_router.local_check import display
from tests.test_discovery_rescan import fake_seats
from tests.test_lc12 import FLAG, call_job, passed, world  # noqa: F401
from tests.test_lc12c import CLOUD, LOCAL, e2e  # noqa: F401
from tests.test_lc8_routing import BARE, DIGEST, JOB, NOW

REAL_LIVE_TAGS = er._live_tags
REMOTE = {"remote_host": "https://ollama.com:443", "remote_model": "gpt-oss:120b"}
OTHER_DIGEST = "sha256:" + "b" * 16


@pytest.fixture(autouse=True)
def _no_provider_call(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("providers.call_llm must not be reached")
    monkeypatch.setattr(providers, "call_llm", boom)


def _drain():
    asyncio.run(router.drain_bg_tasks(timeout_s=5.0))


def _row(name=BARE, digest=DIGEST, **extra):
    return {"name": name, "model": name, "digest": digest, **extra}


def _cache_entry(name):
    return json.loads(model_discovery._cache_path().read_text())["models"][f"ollama/{name}"]


# ----------------------------------------------------------------------------- 1. discovery.json gets `remote`
class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_probe_stores_remote_and_digest_from_the_tags_row(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    rows = {"models": [_row("mine:latest", "abc", **REMOTE), _row("qwen3.5:9b", "def")]}
    seen = []

    def urlopen(req, timeout):
        seen.append(req.full_url)
        return _Resp(json.dumps(rows).encode())
    monkeypatch.setattr(model_discovery.urllib.request, "urlopen", urlopen)
    model_discovery._write_cache(model_discovery.probe_ollama())
    assert seen and seen[0].endswith("/api/tags")
    mine, qwen = _cache_entry("mine:latest"), _cache_entry("qwen3.5:9b")
    assert mine["remote"] is True and mine["digest"] == "abc"
    assert qwen["remote"] is False and qwen["digest"] == "def"
    assert mine["model_id"] == "ollama/mine:latest" and mine["provider_tier"] == "local"  # old fields kept
    # Re-created as a local model: the flag is overwritten, not left at true by the merge.
    rows["models"][0] = _row("mine:latest", "ghi")
    model_discovery._write_cache(model_discovery.probe_ollama())
    assert _cache_entry("mine:latest")["remote"] is False and _cache_entry("mine:latest")["digest"] == "ghi"


def test_tag_metadata_remote_is_always_a_bool():
    assert tag_metadata({"name": "m", "remote_model": "x"})["remote"] is True
    assert tag_metadata({"name": "m", "remote_host": ""})["remote"] is False
    assert tag_metadata({})["remote"] is False


def test_discover_writer_stores_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    discover._update_discovery_cache([_row("mine:latest", "abc", **REMOTE)])
    assert _cache_entry("mine:latest")["remote"] is True


def test_rescan_rewrites_the_digest_and_remote_flag_it_saw(tmp_path, monkeypatch):
    """A digest change triggers the rescan rewrite; it must not keep the old digest under a fresh cached_at."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("LLM_ROUTER_OLLAMA_MODEL", "OLLAMA_MODELS", "OLLAMA_BUDGET_MODELS"):
        monkeypatch.delenv(name, raising=False)
    model_discovery._last_probe_meta.clear()

    def rescan_run(rows, now=10000):
        return scan.rescan(force=True, probe=lambda: rows, detect=lambda: fake_seats(now), now=now)
    rescan_run([{"name": "mine:latest", "digest": "aaa"}])
    assert _cache_entry("mine:latest")["digest"] == "aaa" and _cache_entry("mine:latest")["remote"] is False
    rescan_run([{"name": "mine:latest", "digest": "bbb", **REMOTE}], now=10001)
    assert _cache_entry("mine:latest")["digest"] == "bbb" and _cache_entry("mine:latest")["remote"] is True


# ----------------------------------------------------------------------------- 2. route-time refusal (cached flag)
def _write_discovery(home, remote):
    (home / "discovery.json").write_text(json.dumps({
        "cached_at": time.time(),
        "models": {f"ollama/{BARE}": {"model_id": f"ollama/{BARE}", "digest": DIGEST, "remote": remote}},
    }))


@pytest.mark.parametrize("remote", [True, False])
def test_route_time_refuses_a_model_the_cache_marks_remote(world, remote):
    passed(world)
    _write_discovery(world.home, remote)
    assert display.cached_remote(BARE) is remote and display.cached_remote(f"ollama/{BARE}") is remote
    chain = [*CLOUD, LOCAL]
    assert er.prefer_passed(chain, JOB, 10, NOW) == (chain if remote else [LOCAL, *CLOUD])
    assert er.passed_lane_available(JOB, 10, NOW) is (not remote)
    assert er.passed_model_leads([LOCAL], JOB, 10, NOW) is (not remote)


def test_route_time_remote_flag_reads_no_network(world, monkeypatch):
    passed(world)
    _write_discovery(world.home, True)

    def no_net(*a, **k):
        raise AssertionError("route-time check must not make a request")
    monkeypatch.setattr(http.client.HTTPConnection, "request", no_net)
    monkeypatch.setattr(model_discovery.urllib.request, "urlopen", no_net)
    assert er.passed_lane_available(JOB, 10, NOW) is False


def test_unreadable_discovery_is_not_remote(world):
    (world.home / "discovery.json").write_text("{not json")
    assert display.cached_remote(BARE) is False


# ----------------------------------------------------------------------------- 3. the copy-over window
@pytest.fixture
def live(e2e, monkeypatch):
    """The live /api/tags answer the pre-dispatch check reads; starts matching the pass (digest A, local)."""
    state = {"rows": [_row()], "calls": 0}

    def tags():
        state["calls"] += 1
        return {"models": state["rows"]}
    monkeypatch.setattr(er, "_live_tags", tags)
    e2e.live = state
    return e2e


def test_live_row_matching_the_pass_is_served(live):
    passed(live)
    out = call_job()
    _drain()
    assert live.state["tried"] == [LOCAL] and "status=accepted" in out and live.live["calls"] == 1


@pytest.mark.parametrize("rows,why", [
    ([_row(digest=OTHER_DIGEST)], "digest changed"),
    ([_row(digest="b" * 16)], "digest changed"),          # an unprefixed digest is normalised, still B
    ([_row(**REMOTE)], "remote metadata"),
    ([_row("other:1b")], "model not listed"),
])
def test_copy_over_hands_back_with_no_model_call(live, monkeypatch, rows, why):
    """Pass bound to digest A; the cache still says A (route-time checks pass); the live row says otherwise."""
    passed(live)
    assert er.passed_model_leads([LOCAL], JOB, 10, time.time()) is True  # the cached view alone would route
    live.live["rows"] = rows
    monkeypatch.setattr(router, "_pending_spend", 5.0)
    out = call_job()
    _drain()
    assert live.state["tried"] == []
    assert f"the passed local model no longer matches its pass for {JOB}" in out and why in out
    assert "no cloud model was called" in out and "status=fallback, attempts=0" in out
    assert router._pending_spend == pytest.approx(5.0, abs=1e-12)
    assert er.fail_closed_counts() == {er.FC_LIVE_CHECK: 1}


class _FakeConn:
    made: list = []
    exc: BaseException = socket.timeout("timed out")

    def __init__(self, host, port, timeout=None):
        _FakeConn.made.append((host, port, timeout))

    def request(self, *a, **k):
        raise _FakeConn.exc

    def close(self):
        pass


@pytest.mark.parametrize("exc", [socket.timeout("timed out"), ConnectionRefusedError("refused")])
def test_live_check_timeout_or_error_hands_back(live, monkeypatch, exc):
    passed(live)
    monkeypatch.setattr(er, "_live_tags", REAL_LIVE_TAGS)
    monkeypatch.delenv("OLLAMA_API_BASE", raising=False)
    _FakeConn.made, _FakeConn.exc = [], exc
    monkeypatch.setattr(http.client, "HTTPConnection", _FakeConn)
    monkeypatch.setattr(router, "_pending_spend", 5.0)
    out = call_job()
    _drain()
    assert _FakeConn.made == [("localhost", 11434, er.LIVE_CHECK_TIMEOUT_S)]
    assert live.state["tried"] == [] and "ollama did not answer" in out and "status=fallback, attempts=0" in out
    assert router._pending_spend == pytest.approx(5.0, abs=1e-12)


def test_live_check_refuses_a_non_loopback_daemon_without_connecting(live, monkeypatch):
    passed(live)
    monkeypatch.setattr(er, "_live_tags", REAL_LIVE_TAGS)
    monkeypatch.setenv("OLLAMA_API_BASE", "http://ollama.example.com:11434")
    _FakeConn.made = []
    monkeypatch.setattr(http.client, "HTTPConnection", _FakeConn)
    out = call_job()
    _drain()
    assert _FakeConn.made == [] and live.state["tried"] == [] and "ollama did not answer" in out


def test_live_check_asks_the_daemon_litellm_uses(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_BASE", "http://127.0.0.1:12345")
    _FakeConn.made, _FakeConn.exc = [], socket.timeout("timed out")
    monkeypatch.setattr(http.client, "HTTPConnection", _FakeConn)
    assert REAL_LIVE_TAGS() is None
    assert _FakeConn.made == [("127.0.0.1", 12345, er.LIVE_CHECK_TIMEOUT_S)]


def test_prereg_lane_gets_the_remote_check_only(world, monkeypatch):
    """A pre-registered lane is keyed by name: a different digest is not a refusal, remote metadata is."""
    monkeypatch.setattr(er, "_passed_lane", lambda *a, **k: ({"grade": "passed"}, "prereg", None))
    monkeypatch.setattr(er, "_live_tags", lambda: {"models": [_row(digest=OTHER_DIGEST)]})
    assert er.live_check_refusal(LOCAL, JOB, NOW) is None
    monkeypatch.setattr(er, "_live_tags", lambda: {"models": [_row(**REMOTE)]})
    assert er.live_check_refusal(LOCAL, JOB, NOW) == "remote metadata"


def test_live_check_crash_is_an_eligibility_step_failure(live, monkeypatch):
    passed(live)

    def boom(*a, **k):
        raise KeyError("lane store broke")
    monkeypatch.setattr(er, "live_check_refusal", boom)
    monkeypatch.setattr(router, "_pending_spend", 5.0)
    out = call_job()
    _drain()
    assert live.state["tried"] == [] and f"the local-only eligibility step failed for {JOB} (KeyError)" in out
    assert router._pending_spend == pytest.approx(5.0, abs=1e-12)
    assert er.fail_closed_counts() == {er.FC_CUT_CHECK: 1}


def test_flag_on_routing_is_unchanged_and_makes_no_live_check(e2e, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    passed(e2e)

    def no_live():
        raise AssertionError("the live check is for the pass-only path only")
    monkeypatch.setattr(er, "_live_tags", no_live)
    out = call_job()
    _drain()
    assert e2e.state["tried"][0] == LOCAL and "status=accepted" in out  # the full chain, as before LC-12f
    assert er.fail_closed_counts() == {}


# ----------------------------------------------------------------------------- nits
def test_nit2_a_row_exactly_at_the_cutoff_counts(world):
    now = 10_000_000.0
    cutoff = now - er.FC_WINDOW_S
    store = paths.state_path(er.FC_STORE_NAME)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("".join(json.dumps(r) + "\n" for r in (
        {"c": er.FC_CUT_CHECK, "ts": cutoff},              # exactly at the cutoff: counts (ts >= cutoff)
        {"c": er.FC_ELIGIBILITY_STEP, "ts": cutoff - 0.001},  # just before: does not
    )))
    assert er.fail_closed_counts(now=now) == {er.FC_CUT_CHECK: 1}
    assert er.fail_closed_counts(now=now + 0.001) == {}


def test_nit5_cut_site_generic_error_raises_eligibility_step_failed(e2e, monkeypatch):
    passed(e2e)
    real = er.passed_model_leads

    def leads(models, *a, **k):
        if len(models) == 1:  # the cut site
            raise ValueError("unexpected")
        return real(models, *a, **k)
    monkeypatch.setattr(er, "passed_model_leads", leads)
    monkeypatch.setattr(router, "_pending_spend", 5.0)
    out = call_job()
    _drain()
    assert e2e.state["tried"] == [] and f"the local-only eligibility step failed for {JOB} (ValueError)" in out
    assert "not in the router's chain" not in out
    assert router._pending_spend == pytest.approx(5.0, abs=1e-12)
    assert er.fail_closed_counts() == {er.FC_CUT_CHECK: 1}


def test_nit4_permission_error_reads_as_unreadable(world, monkeypatch):
    store = paths.state_path(er.FC_STORE_NAME)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"c": er.FC_CUT_CHECK, "ts": time.time()}) + "\n")
    real_open = builtins.open

    def guarded(file, *a, **k):
        if str(file).endswith(er.FC_STORE_NAME):
            raise PermissionError(13, "Permission denied", str(file))
        return real_open(file, *a, **k)
    monkeypatch.setattr(builtins, "open", guarded)
    r = counter_registry.read_one("text_job_fail_closed")
    assert r.value is None and "unreadable (PermissionError)" in (r.unknown_reason or "")
    assert "unparseable" not in (r.unknown_reason or "")


def test_nit4_garbage_store_still_reads_as_unparseable(world):
    store = paths.state_path(er.FC_STORE_NAME)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("not json\n")
    r = counter_registry.read_one("text_job_fail_closed")
    assert r.value is None and "unparseable" in (r.unknown_reason or "")
