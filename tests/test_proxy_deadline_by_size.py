"""Size-aware local deadline (llm_router.proxy.deadline, LLM_ROUTER_DEADLINE_BY_SIZE)."""

from __future__ import annotations

import json

import pytest

from llm_router.proxy import deadline
from tests.test_proxy_local_mode import Backend, _first_call, _ollama, _overrides_readable, _post, env  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in (deadline.ENV_FLAG, deadline.ENV_CAP, deadline.ENV_RATE):
        monkeypatch.delenv(k, raising=False)
    deadline._cache.update(at=0.0, path=None, rows=[])
    yield
    deadline._cache.update(at=0.0, path=None, rows=[])


def _ledger(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _row(n, s, decision="served"):
    return {"decision": decision, "backend_usage": {"prompt_tokens": n, "prompt_eval_s": s}}


def test_flag_default_off_and_on_values(monkeypatch):
    assert deadline.enabled() is False
    monkeypatch.setenv(deadline.ENV_FLAG, "on")
    assert deadline.enabled() is True
    monkeypatch.setenv(deadline.ENV_FLAG, "0")
    assert deadline.enabled() is False


def test_fallback_rate_hits_both_measured_points():
    assert deadline.predicted_prefill_s(60_000, [])[0] == pytest.approx(93.0, rel=0.01)
    assert deadline.predicted_prefill_s(30_000, [])[0] == pytest.approx(31.0, rel=0.02)
    assert deadline.predicted_prefill_s(30_000, [])[1] == "default"


def test_fewer_than_five_rows_uses_the_default():
    rows = [(30_000, 5.0)] * 4
    assert deadline.predicted_prefill_s(30_000, rows)[1] == "default"


def test_rate_from_ledger_rows_median_of_nearest(tmp_path):
    p = tmp_path / "l.jsonl"
    _ledger(p, [_row(30_000, 20.0)] * 6 + [_row(10_000, 99.0, "fallback"), _row(500, 9.0), {"x": 1}])
    samples = deadline.recent_samples(p)
    assert len(samples) == 6                      # fallback row, tiny row and junk row ignored
    pred, source = deadline.predicted_prefill_s(30_000, samples)
    assert source == "ledger" and pred == pytest.approx(20.0)
    # 60k from 30k rows: scaled by the super-linear exponent, not by 2
    assert deadline.predicted_prefill_s(60_000, samples)[0] == pytest.approx(20.0 * 2 ** 1.585)


def test_missing_ledger_gives_no_samples(tmp_path):
    assert deadline.recent_samples(tmp_path / "none.jsonl") == []


def test_cap_applies_and_env_overrides(monkeypatch):
    assert deadline.compute(10_000_000)["deadline_s"] == deadline.DEFAULT_CAP_S == 480.0
    monkeypatch.setenv(deadline.ENV_CAP, "600")
    assert deadline.compute(10_000_000)["deadline_s"] == 600.0
    monkeypatch.setenv(deadline.ENV_CAP, "garbage")
    assert deadline.cap_s() == deadline.DEFAULT_CAP_S


def test_rate_env_overrides_default(monkeypatch):
    monkeypatch.setenv(deadline.ENV_RATE, "1290")
    assert deadline.predicted_prefill_s(60_000, [])[0] == pytest.approx(46.5, rel=0.01)


def test_deadline_is_monotonic_in_size():
    sizes = [1_000, 5_000, 12_000, 25_000, 30_000, 60_000, 100_000, 128_000, 400_000]
    ds = [deadline.compute(n, [])["deadline_s"] for n in sizes]
    assert ds == sorted(ds) and ds[0] < ds[-1]
    assert ds[sizes.index(60_000)] == pytest.approx(8 + 1.5 * 93, abs=0.5)


def test_compute_reports_its_fields():
    d = deadline.compute(30_000, [])
    assert set(d) == {"deadline_s", "predicted_prefill_s", "rate_source", "est_prompt_tokens"}


# ── integration through the proxy (local-agent mode fixture) ─────────────────


class HedgedBackend(Backend):
    hedge_s = 8.0


async def test_flag_off_leaves_budget_hedge_and_row_unchanged(env):  # noqa: F811
    backend = HedgedBackend(_ollama("hi"))
    r = await _post(env.app(backend, step_budget_s=30.0), _first_call())
    assert r.status_code == 200
    assert backend.hedge_s == 8.0
    (row,) = env.rows()
    assert row["decision"] == "served"
    assert "deadline" not in row


async def test_flag_on_sets_hedge_and_ledger_fields(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv(deadline.ENV_FLAG, "1")
    monkeypatch.setattr(deadline, "recent_samples", lambda *a, **k: [])
    backend = HedgedBackend(_ollama("hi"))
    r = await _post(env.app(backend, step_budget_s=30.0), _first_call())
    assert r.status_code == 200
    (row,) = env.rows()
    d = row["deadline"]
    assert d["rate_source"] == "default" and d["base_step_budget_s"] == 30.0
    assert d["step_budget_s"] >= 30.0 and d["hedge_s"] == backend.hedge_s >= 8.0
    assert d["predicted_prefill_s"] > 0 and d["deadline_s"] >= 8.0


async def test_flag_on_never_shortens_and_a_hedge_that_is_off_stays_off(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv(deadline.ENV_FLAG, "1")
    monkeypatch.setattr(deadline, "recent_samples", lambda *a, **k: [])
    backend = Backend(_ollama("hi"))      # no hedge_s attribute: hedge off
    await _post(env.app(backend, step_budget_s=120.0), _first_call())
    (row,) = env.rows()
    assert row["deadline"]["step_budget_s"] == 120.0 and row["deadline"]["hedge_s"] is None
    assert not hasattr(backend, "hedge_s")
