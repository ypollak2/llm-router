"""LC-12e repair (review of #713): renamed cloud models, the eligibility-helper crash path, and the capped,
windowed fail-closed counter.

* A cloud model copied or re-created under a local-looking name (``ollama cp gpt-oss:120b-cloud mine``, a Modelfile
  ``FROM``) carries ``remote_host`` / ``remote_model`` in its ``/api/tags`` row and ``/api/show`` answer.
  local-check reads the answers it already fetched and refuses before any generate call, judge or file write.
* ``prefer_passed`` / ``passed_model_leads`` raising on the pass-only text-job path is a counted fail-CLOSED
  refusal with an accurate cause, not a fail-open event that reads as "not in the router's chain".
* ``text_job_fail_closed.jsonl`` is written through ``capped_log``; the doctor counts the last 7 days only.

Tmp homes, fake ``/api/*`` answers (``runtimes._strict_json``), fake provider layer (``router._call_text``,
``providers.call_llm`` guarded); no network, no model.
"""
# ruff: noqa: F811  (the `world` / `e2e` fixtures are imported and used as arguments)
from __future__ import annotations

import asyncio
import json
import time

import pytest

from llm_router import counter_registry, failopen, paths, providers
from llm_router import eligibility_routing as er
from llm_router import router
from llm_router.commands import local_check as LCC
from llm_router.local_check import display, routing as lr, runtimes
from llm_router.resolver import inventory
from llm_router.resolver.inventory import _cloud_marker, is_ollama_cloud_model, ollama_remote_marker
from tests.test_lc10i_wiring import deps, passing, run
from tests.test_lc12 import FLAG, call_job, passed, world  # noqa: F401
from tests.test_lc12c import CLOUD, LOCAL, e2e  # noqa: F401
from tests.test_lc8_routing import BARE, DIGEST, JOB

URL = "http://127.0.0.1:11434"
MINE = "mine:latest"
REMOTE = {"remote_host": "https://ollama.com:443", "remote_model": "gpt-oss:120b"}


@pytest.fixture(autouse=True)
def _no_provider_call(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("providers.call_llm must not be reached")
    monkeypatch.setattr(providers, "call_llm", boom)


def _drain():
    asyncio.run(router.drain_bg_tasks(timeout_s=5.0))


# ----------------------------------------------------------------------------- the predicate (nits)
@pytest.mark.parametrize("name", ["x-cloud", "x-cloud:latest", "ollama/x-cloud", "ollama/x-cloud:latest", "cloud", "localhost:5000/lib/m-cloud"])
def test_untagged_name_means_latest_so_both_forms_agree(name):
    """Ollama resolves ``x-cloud`` to ``x-cloud:latest``: the two forms are one model and get one answer."""
    assert is_ollama_cloud_model(name) is False


@pytest.mark.parametrize("name", ["x:cloud", "x:120b-cloud", "localhost:5000/lib/m:cloud"])
def test_a_cloud_tag_is_cloud(name):
    assert is_ollama_cloud_model(name) is True


@pytest.mark.parametrize("name", ["ollama/", "OLLAMA_CHAT/"])
def test_a_bare_provider_prefix_is_an_empty_name_and_fails_closed(name):
    """Pins the prefix strip: without it, ``ollama/`` is a non-empty untagged name and reads as local."""
    assert is_ollama_cloud_model(name) is True


def test_remote_marker_reads_either_answer_and_makes_no_request():
    assert ollama_remote_marker({"remote_host": "h"}, {}) == "/api/tags reports remote_host"
    assert ollama_remote_marker({}, {"remote_model": "m"}) == "/api/show reports remote_model"
    assert ollama_remote_marker(None, None) is None and ollama_remote_marker({}, {"remote_host": ""}) is None
    assert _cloud_marker(MINE, {}, {"remote_host": "h"}) == "/api/show reports remote_host"


# ----------------------------------------------------------------------------- local-check nits
def test_enable_refuses_a_cloud_model_and_changes_nothing(tmp_path):
    d, sink, _ = deps(tmp_path)
    assert run(["enable", JOB, "--model", "gpt-oss:120b-cloud"], d) == LCC.EXIT_REFUSED
    assert "never routes as local" in sink.etext and "routing: on" not in sink.text
    assert not lr.is_enabled("gpt-oss:120b-cloud", JOB)
    d, sink, _ = deps(tmp_path, inventory=lambda: ["glm-4.6:cloud"])  # the only inventory model is cloud
    assert run(["enable", JOB], d) == LCC.EXIT_REFUSED and not lr.is_enabled("glm-4.6:cloud", JOB)
    assert run(["disable", JOB, "--model", "glm-4.6:cloud"], d) == LCC.EXIT_OK  # switching off stays allowed


def test_picker_lists_cloud_models_as_excluded(tmp_path):
    eps = [runtimes.Endpoint("ollama", URL, ["gpt-oss:120b-cloud", "qwen3.5:9b"])]
    d, sink, seen = deps(tmp_path, passing(), endpoints=eps, interactive=True, answers=["1"])
    assert run(["--dry-run"], d) == LCC.EXIT_OK
    t = sink.text
    menu = t[t.index("Local models found:"):t.index("Excluded")]
    assert "1. qwen3.5:9b" in menu and "cloud" not in menu
    assert "Excluded (Ollama cloud-hosted; prompts leave this machine): gpt-oss:120b-cloud" in t
    assert "Model:      qwen3.5:9b" in t and seen.adapter.calls == 0


def test_picker_with_only_cloud_models_refuses(tmp_path):
    eps = [runtimes.Endpoint("ollama", URL, ["gpt-oss:120b-cloud", "glm-4.6:cloud"])]
    d, sink, seen = deps(tmp_path, passing(), endpoints=eps, interactive=True, answers=["1"])
    assert run(["--dry-run"], d) == LCC.EXIT_NO_RUNTIME
    assert "only Ollama cloud-hosted models found" in sink.etext and seen.adapter_kw == []


# ----------------------------------------------------------------------------- item 1: renamed cloud model
class _Api:
    """Fake Ollama /api answers through ``runtimes._strict_json``; records every request."""

    def __init__(self, tags_extra: dict | None = None, show_extra: dict | None = None):
        self.tags_extra, self.show_extra, self.requests = tags_extra or {}, show_extra or {}, []

    def __call__(self, base_url, method, path, body=None, allow_remote=False):
        self.requests.append((method, path))
        if path == "/api/tags":
            return {"models": [{"name": MINE, "model": MINE, "digest": "abc", **self.tags_extra}]}
        if path == "/api/show":
            return {"parameters": "num_ctx 262144", "capabilities": ["completion"], **self.show_extra}
        raise AssertionError(f"unexpected request {method} {path}")


def _ollama(monkeypatch, api: _Api) -> runtimes.OllamaAdapter:
    monkeypatch.setattr(runtimes, "_strict_json", api)
    ad = runtimes.OllamaAdapter(URL)
    ad.generated = 0

    def gen(*a, **k):
        ad.generated += 1
        raise AssertionError("no generate call on a refused model")
    monkeypatch.setattr(ad, "generate", gen)
    return ad


def test_adapter_marker_uses_the_answers_already_fetched(monkeypatch):
    api = _Api(show_extra=REMOTE)
    ad = _ollama(monkeypatch, api)
    ad.digest(MINE), ad.settings_hash(MINE), ad.max_prompt_chars(MINE)
    before = list(api.requests)
    assert ad.remote_marker(MINE) == "/api/show reports remote_host"
    assert api.requests == before  # no request of its own


@pytest.mark.parametrize("where", ["tags", "show"])
def test_local_check_refuses_a_renamed_cloud_model(tmp_path, monkeypatch, where):
    api = _Api(tags_extra=REMOTE if where == "tags" else None, show_extra=REMOTE if where == "show" else None)
    ad = _ollama(monkeypatch, api)
    d, sink, seen = deps(tmp_path, ad, endpoints=[runtimes.Endpoint("ollama", URL, [MINE])])
    assert not is_ollama_cloud_model(MINE)  # the name says local
    assert run(["--yes", "--model", MINE], d) == LCC.EXIT_REFUSED
    assert f"Refused: {MINE} is served off this machine (/api/{where} reports remote_host)" in sink.etext
    assert ad.generated == 0                                        # no benchmark call
    assert sorted(set(api.requests)) == [("GET", "/api/tags"), ("POST", "/api/show")]  # the probes build_plan always makes
    assert seen.api_calls == [] and seen.cli_calls == [] and seen.judge.calls == 0  # no judge
    assert not (tmp_path / "runs").exists() and not (tmp_path / "elig.json").exists()  # nothing written
    assert not lr.is_enabled(MINE, JOB)


def test_same_answers_without_remote_keys_are_not_refused(tmp_path, monkeypatch):
    """Control: identical fakes minus the remote keys reach the plan screen, so the refusal above is the metadata."""
    ad = _ollama(monkeypatch, _Api())
    d, sink, _ = deps(tmp_path, ad, endpoints=[runtimes.Endpoint("ollama", URL, [MINE])])
    assert run(["--dry-run", "--model", MINE], d) == LCC.EXIT_OK
    assert "served off this machine" not in sink.etext and "Dry run: no model call was made" in sink.text


# ----------------------------------------------------------------------------- item 3: helper crash on the pass-only path
def _inventory_breaks_after_first_call(monkeypatch):
    """The opt-in gate (passed_lane_available) reads the inventory fine; the eligibility step's read raises."""
    n = {"calls": 0}

    def inv():
        n["calls"] += 1
        if n["calls"] > 1:
            raise RuntimeError("inventory cache unreadable")
        return [BARE], {BARE: DIGEST}
    monkeypatch.setattr(display, "inventory", inv)
    return n


def test_inventory_crash_in_the_eligibility_step_is_a_fail_closed_refusal(e2e, monkeypatch):
    passed(e2e)
    n = _inventory_breaks_after_first_call(monkeypatch)
    failopen.clear()
    monkeypatch.setattr(router, "_pending_spend", 5.0)
    out = call_job()
    _drain()
    assert n["calls"] >= 2 and e2e.state["tried"] == []
    assert "the local-only eligibility step failed for commit_message (RuntimeError)" in out
    assert "not in the router's chain" not in out and "no cloud model was called" in out
    assert er.fail_closed_counts() == {er.FC_ELIGIBILITY_STEP: 1}
    failopen.reset_cache()
    by = failopen.snapshot().by_code
    assert "CHZ-FO-ELIGIBILITY-PREFER-PASSED" not in by and "CHZ-FO-ELIGIBILITY-PASSED-LEADS" not in by
    assert router._pending_spend == pytest.approx(5.0, abs=1e-12)


def test_helpers_raise_only_on_the_pass_only_path(world, monkeypatch):
    passed(world)
    monkeypatch.setattr(display, "inventory", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    failopen.clear()
    # Not pass-only: the fail-open codes and return values are unchanged.
    assert er.prefer_passed([LOCAL, *CLOUD], JOB, 10, time.time()) == [LOCAL, *CLOUD]
    assert er.passed_model_leads([LOCAL], JOB, 10, time.time()) is False
    failopen.reset_cache()
    by = failopen.snapshot().by_code
    assert by.get("CHZ-FO-ELIGIBILITY-PREFER-PASSED") == 1 and by.get("CHZ-FO-ELIGIBILITY-PASSED-LEADS") == 1
    assert er.fail_closed_counts() == {}
    tok = er.set_require_passed_first()
    try:
        with pytest.raises(er.EligibilityStepFailed):
            er.prefer_passed([LOCAL, *CLOUD], JOB, 10, time.time())
        with pytest.raises(er.EligibilityStepFailed):
            er.passed_model_leads([LOCAL], JOB, 10, time.time())
    finally:
        er.reset_require_passed_first(tok)
    assert er.fail_closed_counts() == {er.FC_ELIGIBILITY_STEP: 2}


def test_flag_on_inventory_crash_stays_fail_open(e2e, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    passed(e2e)
    monkeypatch.setattr(display, "inventory", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    failopen.clear()
    call_job()
    _drain()
    failopen.reset_cache()
    assert failopen.snapshot().by_code.get("CHZ-FO-ELIGIBILITY-PREFER-PASSED") == 1
    assert er.fail_closed_counts() == {}


def test_cut_site_eligibility_failure_releases_the_reservation_and_counts_once(e2e, monkeypatch):
    passed(e2e)
    real = er.passed_model_leads

    def leads(models, *a, **k):
        if len(models) == 1:  # the cut site
            er.record_fail_closed(er.FC_ELIGIBILITY_STEP)
            raise er.EligibilityStepFailed("the eligibility step failed (RuntimeError); local-only routing refuses")
        return real(models, *a, **k)
    monkeypatch.setattr(er, "passed_model_leads", leads)
    monkeypatch.setattr(router, "_pending_spend", 5.0)
    out = call_job()
    _drain()
    assert e2e.state["tried"] == [] and "the local-only eligibility step failed" in out
    assert router._pending_spend == pytest.approx(5.0, abs=1e-12)
    assert er.fail_closed_counts() == {er.FC_ELIGIBILITY_STEP: 1}  # not also counted as a cut-check failure


def test_cut_site_cloud_check_import_failure_is_a_refusal_with_no_leak(e2e, monkeypatch):
    """Nit: the predicate is read inside the cut-site try, so an error there releases the reservation."""
    passed(e2e)
    armed = {"on": False}
    real_pred, real_drop = inventory.is_ollama_cloud_model, router._drop_failed_eligibility

    def pred(m):
        if armed["on"]:
            raise ImportError("inventory broke")
        return real_pred(m)

    def drop(*a, **k):
        out = real_drop(*a, **k)
        armed["on"] = True
        return out
    monkeypatch.setattr(inventory, "is_ollama_cloud_model", pred)
    monkeypatch.setattr(router, "_drop_failed_eligibility", drop)
    monkeypatch.setattr(router, "_pending_spend", 5.0)
    out = call_job()
    _drain()
    assert e2e.state["tried"] == [] and "status=fallback, attempts=0" in out
    assert router._pending_spend == pytest.approx(5.0, abs=1e-12)
    assert er.fail_closed_counts() == {er.FC_CUT_CHECK: 1}


# ----------------------------------------------------------------------------- item 2: cap and window
def _store():
    return paths.state_path(er.FC_STORE_NAME)


def _write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_old_refusals_age_out_of_the_doctor_window(world):
    now = time.time()
    _write_rows(_store(), [{"c": er.FC_CUT_CHECK, "ts": now - er.FC_WINDOW_S - 60}])
    assert er.fail_closed_counts() == {}  # readable, but outside the window: zero, not unknown
    r = counter_registry.read_one("text_job_fail_closed")
    assert r.value == 0.0 and not r.alarming and "in the last 7 days" in r.detail
    _write_rows(_store(), [{"c": er.FC_ELIGIBILITY_STEP, "ts": now - 60}])
    assert er.fail_closed_counts() == {er.FC_ELIGIBILITY_STEP: 1}
    assert er.fail_closed_counts(now=now + er.FC_WINDOW_S) == {}
    assert counter_registry.read_one("text_job_fail_closed").alarming


def test_store_is_capped_through_capped_log(world, monkeypatch):
    monkeypatch.setattr(er, "FC_MAX_BYTES", 400)
    for _ in range(60):
        er.record_fail_closed(er.FC_ELIGIBILITY_STEP, RuntimeError("x"))
    live, old = _store(), _store().with_name(er.FC_STORE_NAME + ".1")
    row = len((json.dumps({"c": er.FC_ELIGIBILITY_STEP, "ts": round(time.time(), 3), "e": "RuntimeError"},
                          separators=(",", ":")) + "\n").encode())
    assert old.exists() and old.stat().st_size <= 400 + row
    assert not live.exists() or live.stat().st_size <= 400 + row  # the last write may just have rotated it
    assert not live.with_name(er.FC_STORE_NAME + ".2").exists()  # two generations only
    n = er.fail_closed_counts()[er.FC_ELIGIBILITY_STEP]
    assert 0 < n < 60  # the oldest rows were dropped by rotation


def test_an_oversized_legacy_store_is_tail_read(world, monkeypatch):
    """A file written before the cap (no rotation) is read only to the cap: the read stays bounded."""
    monkeypatch.setattr(er, "FC_MAX_BYTES", 1000)
    now = time.time()
    _write_rows(_store(), [{"c": er.FC_CUT_CHECK, "ts": now - 1} for _ in range(500)])
    row = len(json.dumps({"c": er.FC_CUT_CHECK, "ts": now - 1}) + "\n")
    n = er.fail_closed_counts()[er.FC_CUT_CHECK]
    assert n <= (1000 + 4096) // row + 1 and n < 500
