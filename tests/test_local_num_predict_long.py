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

