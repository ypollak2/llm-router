"""LC-12e: Ollama cloud models (``-cloud`` / ``:cloud`` tag) never count as local.

They are served off-machine through the local Ollama daemon, so a local-check PASS must not be written for one,
the eligibility path must not treat one as routable even when an older ``local_routing.json`` says passed, and the
D-277 local-only cut must refuse one. Also: the cut-site check fails closed on a raise (N1), the eligibility-step
crash says so (N2), and fail-closed refusals have their own counter (N3).

Tmp homes, fake digests, fake provider layer (``router._call_text``); no network, no model.
"""
# ruff: noqa: F811  (the `world` / `e2e` fixtures are imported and used as arguments)
from __future__ import annotations

import pytest

from llm_router import counter_registry, failopen
from llm_router import eligibility_routing as er
from llm_router import router
from llm_router.commands import local_check as LCC
from llm_router.local_check import display, routing as lr, runtimes
from llm_router.resolver import eligibility_store as store
from llm_router.resolver.inventory import _cloud_marker, is_ollama_cloud_model
from tests.test_lc10i_wiring import deps, passing, run
from tests.test_lc12 import FLAG, call_job, passed, world  # noqa: F401
from tests.test_lc12c import CLOUD, LOCAL, e2e  # noqa: F401
from tests.test_lc8_routing import BARE, DIGEST, JOB, NOW, PASS70, PROV

CLOUD_BARE = "gpt-oss:120b-cloud"
CLOUD_LOCAL = f"ollama/{CLOUD_BARE}"


# ----------------------------------------------------------------------------- the predicate
@pytest.mark.parametrize("name", [
    "ollama/gpt-oss:120b-cloud", "qwen3-coder:480b-cloud", "ollama/foo:cloud", "OLLAMA/x:Cloud",
    "ollama_chat/glm-4.6:cloud", "deepseek-v3.1:671B-CLOUD", "kimi-k2-cloud",  # untagged name ending -cloud: fail closed
])
def test_cloud_names_are_detected(name):
    assert is_ollama_cloud_model(name) is True


@pytest.mark.parametrize("name", [
    "ollama/qwen3.6:35b", "ollama/cloudy-model:7b", "cloudy-model:7b", "ollama/cloud-coder:7b", "qwen3.5:latest", "llama3",
])
def test_local_names_are_not_cloud(name):
    """A name that merely CONTAINS "cloud" is not a marker: only the tag (after the last ':') decides."""
    assert is_ollama_cloud_model(name) is False


@pytest.mark.parametrize("bad", ["", None, 7])
def test_unreadable_id_fails_closed(bad):
    assert is_ollama_cloud_model(bad) is True


def test_inventory_marker_reuses_the_predicate():
    assert _cloud_marker("foo:cloud", {}, {}) == "model tag names a cloud variant"
    assert _cloud_marker("cloudy-model:7b", {}, {}) is None
    assert _cloud_marker("x:7b", {"remote_host": "https://ollama.com"}, {}) == "/api/tags reports remote_host"


# ----------------------------------------------------------------------------- (a) local-check
def test_local_check_on_a_cloud_model_refuses_and_writes_no_pass(tmp_path):
    d, sink, seen = deps(tmp_path, passing(), endpoints=[runtimes.Endpoint("ollama", "http://127.0.0.1:11434", [CLOUD_BARE])])
    assert run(["--yes", "--model", CLOUD_BARE], d) == LCC.EXIT_REFUSED
    assert "cloud-hosted Ollama model; local-check measures local models only" in sink.etext
    assert seen.adapter.calls == 0 and seen.adapter_kw == []  # no adapter built, no model call
    assert seen.api_calls == [] and seen.cli_calls == []      # no judge
    assert not (tmp_path / "elig.json").exists()               # no lane recorded
    assert not lr.is_enabled(CLOUD_BARE, JOB) and "routing: on" not in sink.text


def test_local_check_on_a_local_model_still_passes(tmp_path):
    """Control for the refusal above: the same deps with a local name run and pass."""
    d, sink, _ = deps(tmp_path, passing(), endpoints=[runtimes.Endpoint("ollama", "http://127.0.0.1:11434", ["qwen3.5:9b"])])
    assert run(["--yes", "--model", "qwen3.5:9b"], d) == LCC.EXIT_OK
    assert "routing: on for commit_message" in sink.text


# ----------------------------------------------------------------------------- (b) eligibility, stale file
def _stale_cloud_pass(world, monkeypatch) -> None:
    """A local_routing.json + lane written before LC-12e: the cloud model is enabled and passed, digest verified."""
    store.record_local_check(CLOUD_BARE, {JOB: PASS70}, PROV, path=world.home / "local_eligibility.json")
    lr.enable(CLOUD_BARE, JOB, "local_check")
    monkeypatch.setattr(display, "inventory", lambda: ([BARE, CLOUD_BARE], {BARE: DIGEST, CLOUD_BARE: DIGEST}))


def test_stale_cloud_pass_is_not_routable(world, monkeypatch):
    _stale_cloud_pass(world, monkeypatch)
    assert lr.is_enabled(CLOUD_BARE, JOB)  # the file really says enabled
    assert er.passed_model_leads([CLOUD_LOCAL, *CLOUD], JOB, 1000, NOW) is False
    assert er.passed_lane_available(JOB, 1000, NOW) is False
    assert er.prefer_passed([CLOUD[0], CLOUD_LOCAL], JOB, 1000, NOW) == [CLOUD[0], CLOUD_LOCAL]


def test_same_stale_file_with_a_local_model_is_routable(world, monkeypatch):
    """Control: the identical setup for a local name is routable, so the False above is the cloud check."""
    passed(world)
    assert er.passed_model_leads([LOCAL, *CLOUD], JOB, 1000, NOW) is True
    assert er.passed_lane_available(JOB, 1000, NOW) is True


def test_stale_cloud_pass_text_job_hands_back_with_no_model_call(e2e, monkeypatch):
    _stale_cloud_pass(e2e, monkeypatch)
    e2e.state["chain"] = [CLOUD_LOCAL, *CLOUD]
    monkeypatch.setattr(router, "_pending_spend", 5.0)
    out = call_job()
    router_drain()
    assert e2e.state["tried"] == []
    assert "status=fallback, attempts=0" in out and "Do this job yourself." in out
    assert router._pending_spend == pytest.approx(5.0, abs=1e-12)


# ----------------------------------------------------------------------------- (c) the D-277 cut
def test_cut_refuses_a_cloud_survivor_even_when_every_earlier_guard_says_yes(e2e, monkeypatch):
    """Defense in depth: with the opt-in gate and the eligibility step forced to accept the cloud model, the final
    cut still refuses it through the released-reservation path."""
    _stale_cloud_pass(e2e, monkeypatch)
    e2e.state["chain"] = [CLOUD_LOCAL, *CLOUD]
    monkeypatch.setattr(er, "passed_lane_available", lambda *a, **k: True)
    # The pre-LC-12e guards: the cloud model is moved to the front and accepted as the passed lead.
    monkeypatch.setattr(er, "prefer_passed", lambda models, *a, **k: [CLOUD_LOCAL] + [m for m in models if m != CLOUD_LOCAL])
    monkeypatch.setattr(er, "passed_model_leads", lambda models, *a, **k: bool(models) and models[0] == CLOUD_LOCAL)
    seen = {}
    real = router._drop_failed_eligibility

    def spy(*a, **k):
        out = real(*a, **k)
        seen["head"], seen["pending"] = out[0], router._pending_spend
        return out

    monkeypatch.setattr(router, "_drop_failed_eligibility", spy)
    monkeypatch.setattr(router, "_pending_spend", 5.0)
    out = call_job()
    router_drain()
    assert seen["head"] == CLOUD_LOCAL and seen["pending"] > 5.0  # it reached the cut holding a reservation
    assert e2e.state["tried"] == []
    assert "status=fallback, attempts=0" in out and "no cloud model was called" in out
    assert router._pending_spend == pytest.approx(5.0, abs=1e-12)


# ----------------------------------------------------------------------------- N1 / N2 / N3
def test_cut_site_check_raising_fails_closed_with_no_leak(e2e, monkeypatch):
    """N1: the cut-site passed_model_leads raising is a refusal, the reservation is released once, and it is counted."""
    passed(e2e)
    real = er.passed_model_leads

    def leads(models, *a, **k):
        if len(models) == 1:  # the cut site checks [_pass_model]; the eligibility step checks the whole chain
            raise RuntimeError("cut check broke")
        return real(models, *a, **k)

    monkeypatch.setattr(er, "passed_model_leads", leads)
    monkeypatch.setattr(router, "_pending_spend", 5.0)
    out = call_job()
    router_drain()
    assert e2e.state["tried"] == []
    assert "status=fallback, attempts=0" in out and "Do this job yourself." in out
    assert router._pending_spend == pytest.approx(5.0, abs=1e-12)
    assert er.fail_closed_counts() == {er.FC_CUT_CHECK: 1}


def test_eligibility_step_crash_says_the_step_failed_and_counts_fail_closed(e2e, monkeypatch):
    """N2 + N3: the cause names the failed step; the refusal is a fail-CLOSED count, not a fail-open one."""
    passed(e2e)
    monkeypatch.setattr(er, "note_bypass", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    failopen.clear()
    out = call_job()
    router_drain()
    assert e2e.state["tried"] == []
    assert "the local-only eligibility step failed for commit_message (RuntimeError)" in out
    assert "not in the router's chain" not in out
    assert er.fail_closed_counts() == {er.FC_ELIGIBILITY_STEP: 1}
    failopen.reset_cache()
    assert "CHZ-FO-ROUTER-ELIGIBILITY-FILTER" not in failopen.snapshot().by_code


def test_flag_on_eligibility_crash_is_still_a_fail_open_count(e2e, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    passed(e2e)
    monkeypatch.setattr(er, "note_bypass", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    failopen.clear()
    call_job()
    router_drain()
    assert e2e.state["tried"]
    failopen.reset_cache()
    assert failopen.snapshot().by_code.get("CHZ-FO-ROUTER-ELIGIBILITY-FILTER") == 1
    assert er.fail_closed_counts() == {}


def test_fail_closed_counter_reaches_the_registry(world):
    assert er.fail_closed_counts() == {}
    assert counter_registry.read_one("text_job_fail_closed").value == 0.0
    er.record_fail_closed(er.FC_ELIGIBILITY_STEP, RuntimeError("probe"))
    r = counter_registry.read_one("text_job_fail_closed")
    assert r.value == 1.0 and r.alarming
    assert any(ln.startswith("text_job_fail_closed: 1") for ln in counter_registry.render_lines())


def test_unreadable_fail_closed_store_is_unknown_not_zero(world):
    from llm_router import paths
    p = paths.state_path(er.FC_STORE_NAME)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json\n", encoding="utf-8")
    assert er.fail_closed_counts() is None
    assert counter_registry.read_one("text_job_fail_closed").value is None


def router_drain():
    import asyncio
    asyncio.run(router.drain_bg_tasks(timeout_s=5.0))

