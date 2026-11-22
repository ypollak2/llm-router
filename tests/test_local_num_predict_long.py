"""D-582a: 4096 output tokens for long organic main-thread continuations, 2048 for everything else."""

from __future__ import annotations

import pytest

from llm_router.proxy import local_mode as lm
from tests.test_proxy_local_mode import Backend, _first_call, _ollama, _overrides_readable, _post, env  # noqa: F401  (fixtures)

BIG = lm.LONG_PROMPT_EST + 1


def test_constants():
    assert (lm.LOCAL_NUM_PREDICT, lm.LOCAL_NUM_PREDICT_LONG, lm.LONG_PROMPT_EST) == (2048, 4096, 60_000)


def test_all_conditions_met_gets_long_cap():
    assert lm.num_predict_for_step("organic", True, "continuation", BIG) == 4096


@pytest.mark.parametrize("kind", ["research", "harness", "headless", None])
def test_not_organic_keeps_default(kind):
    assert lm.num_predict_for_step(kind, True, "continuation", BIG) == 2048


@pytest.mark.parametrize("main", [False, None])
def test_not_main_thread_keeps_default(main):
    assert lm.num_predict_for_step("organic", main, "continuation", BIG) == 2048


@pytest.mark.parametrize("cls", ["turn_first", "subagent_turn", "subagent_first", "side_call", "unknown", None])
def test_not_continuation_keeps_default(cls):
    assert lm.num_predict_for_step("organic", True, cls, BIG) == 2048


def test_prompt_boundary_is_strictly_above_60000():
    assert lm.num_predict_for_step("organic", True, "continuation", 60_000) == 2048
    assert lm.num_predict_for_step("organic", True, "continuation", 60_001) == 4096
    assert lm.num_predict_for_step("organic", True, "continuation", None) == 2048


def test_prompt_cap_does_not_depend_on_the_long_cap(monkeypatch):
    monkeypatch.setattr(lm.local_models, "raised_window", lambda: 131_072)
    before = lm.prompt_cap_tokens()
    monkeypatch.setattr(lm, "LOCAL_NUM_PREDICT_LONG", 8192)
    assert lm.prompt_cap_tokens() == before
    # cap + the long reply still fits the window
    assert before + lm.LOCAL_NUM_PREDICT_LONG <= 131_072


async def test_ledger_row_records_num_predict(env):  # noqa: F811
    r = await _post(env.app(Backend(_ollama("hi"))), _first_call())
    assert r.status_code == 200
    (row,) = env.rows()
    assert row["num_predict"] == lm.LOCAL_NUM_PREDICT



# --- integration: try_serve -> make_backend -> the constructed backend -------------------------------------

import httpx  # noqa: E402

from llm_router.proxy import server as ps  # noqa: E402
from tests.test_proxy_local_mode import Breaker, _call, _next_turn, _served_blocks  # noqa: E402


def _recording_backend(built: list):
    class Recorded(Backend):
        def __init__(self, model, http, **kw):
            super().__init__(_ollama("", [_call("Read", {"file_path": "/work/repo/a.py"})]), _ollama("Done."))
            built.append(kw.get("num_predict"))
    return Recorded


async def _serve(env, monkeypatch, *, kind, est, serve="local-agent"):  # noqa: F811
    from llm_router import session_kind

    built: list = []
    monkeypatch.setattr(ps, "BACKENDS", {"ollama/": _recording_backend(built)})
    monkeypatch.setattr(session_kind, "kind_of", lambda sid: kind)
    monkeypatch.setattr(lm, "estimate_prompt_tokens", lambda payload: est)
    monkeypatch.setattr(lm, "prompt_cap_tokens", lambda: 10**6)
    cfg = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=env.ledger, kill_switch=env.kill,
                         model="ollama/fake:1", trim="none", serve=serve, hedge_s=None, warm_up=False)
    app = ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(env.up)), breaker_fn=Breaker())
    first = await _post(app, _first_call())
    assert first.status_code == 200
    r = await _post(app, dict(_next_turn(_first_call(), {"content": _served_blocks(first)}), stream=False))
    assert r.status_code == 200
    return built, env.rows()


async def test_qualifying_step_builds_backend_with_4096_and_records_it(env, monkeypatch):  # noqa: F811
    built, rows = await _serve(env, monkeypatch, kind="organic", est=BIG)
    first, row = rows
    assert first["step_class"] == "turn_first" and first["num_predict"] == 2048
    assert row["step_class"] == "continuation" and row["is_main_thread"] is True
    assert row["num_predict"] == 4096 and built == [2048, 4096]


async def test_non_qualifying_control_builds_backend_with_2048(env, monkeypatch):  # noqa: F811
    built, rows = await _serve(env, monkeypatch, kind="research", est=BIG)
    assert rows[-1]["step_class"] == "continuation"
    assert [r["num_predict"] for r in rows] == [2048, 2048] and built == [2048, 2048]


async def test_boundary_estimate_control_builds_2048(env, monkeypatch):  # noqa: F811
    built, rows = await _serve(env, monkeypatch, kind="organic", est=lm.LONG_PROMPT_EST)
    assert rows[-1]["step_class"] == "continuation" and rows[-1]["num_predict"] == 2048 and built == [2048, 2048]


async def test_num_predict_absent_when_local_mode_off(env, monkeypatch):  # noqa: F811
    built, rows = await _serve(env, monkeypatch, kind="organic", est=BIG, serve="off")
    assert rows and built == []   # nothing was served locally
    assert all("num_predict" not in r for r in rows)
