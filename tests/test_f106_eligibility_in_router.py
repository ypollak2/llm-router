"""F106 / D-235: route_and_call skips a local model on a lane it measurably failed.

``local_eligibility.json`` lanes are built here from counts through ``eligibility`` itself, so the
stored grade is the rule's grade. No network and no Ollama: chain build and dispatch are stubbed.
Every test runs with LLM_ROUTER_HOME in tmp_path; the model names are fixtures, not real models.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from llm_router import eligibility, eligibility_routing as er, failopen, skip_causes
from llm_router.types import Complexity, LLMResponse, TaskType

BAD = "ollama/fixture-bad:7b"          # failed qa lane (powered)
OTHER = "ollama/fixture-other:3b"      # no entry in the file
GEMINI = "gemini/gemini-2.5-flash"


def _lane(k: int, kr: int, n: int, verdict: str) -> dict:
    ev = eligibility.evidence_from_counts(k, n, kr, n)
    return {"grade": eligibility.grade(ev), "verdict": verdict, "eligible": verdict == "eligible",
            "k": k, "n": n, "gap_pts": ev.gap_pts, "lb_pts": ev.lb_pts, "source": "p2.0-requal",
            "ref": {"k": kr, "n": n}}


FAILED = _lane(13, 96, 113, "failed")
ELIGIBLE = _lane(110, 111, 113, "eligible")
INCONCLUSIVE = _lane(27, 40, 74, "inconclusive")    # failed grade, power-limited verdict
NOT_INFORMATIVE = {"grade": "unknown", "verdict": "not_informative", "eligible": False,
                   "k": 0, "n": 0, "source": "p2.0-requal"}
PROBE = {**_lane(10, 12, 30, "inconclusive")}


def test_lane_fixtures_grade_as_intended():
    assert FAILED["grade"] == "failed"
    assert ELIGIBLE["grade"] == "eligible"
    assert INCONCLUSIVE["grade"] == "failed"
    assert PROBE["grade"] == "probe"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(h))
    monkeypatch.delenv("LLM_ROUTER_IGNORE_ELIGIBILITY", raising=False)
    er.reset_cache()
    failopen.reset_cache()
    yield h
    er.reset_cache()


def _write(home, data, model=BAD) -> None:
    path = home / "local_eligibility.json"
    body = data if isinstance(data, str) else json.dumps({model.split("/", 1)[1]: data})
    path.write_text(body)


def _qa(lane: dict) -> dict:
    return {"qa": {"simple": lane, "moderate": lane}}


CHAIN = [BAD, GEMINI, OTHER]


# ── unit: filter_chain ─────────────────────────────────────────────────────

def test_failed_qa_lane_drops_only_that_local_model(home):
    _write(home, _qa(FAILED))
    for tt in (TaskType.QUERY, TaskType.RESEARCH, TaskType.GENERATE, TaskType.ANALYZE):
        for tier in (Complexity.SIMPLE, Complexity.MODERATE):
            assert er.filter_chain(CHAIN, tt, tier) == [GEMINI, OTHER], (tt, tier)


@pytest.mark.parametrize("lane", [ELIGIBLE, INCONCLUSIVE, NOT_INFORMATIVE, PROBE],
                         ids=["eligible", "inconclusive", "not_informative", "probe"])
def test_non_failed_verdicts_leave_chain_unchanged(home, lane):
    _write(home, _qa(lane))
    assert er.filter_chain(CHAIN, TaskType.QUERY, Complexity.SIMPLE) == CHAIN


def test_stored_grade_inconsistent_with_counts_is_ignored(home):
    # A lane whose stored grade disagrees with its own counts is not trusted.
    _write(home, _qa({**ELIGIBLE, "grade": "failed", "verdict": "failed", "eligible": False}))
    assert er.filter_chain(CHAIN, TaskType.QUERY, Complexity.SIMPLE) == CHAIN


def test_missing_file_leaves_chain_unchanged_and_records_nothing(home):
    assert er.filter_chain(CHAIN, TaskType.QUERY, Complexity.SIMPLE) == CHAIN
    assert "CHZ-FO-ELIGIBILITY-ROUTING-UNREADABLE" not in failopen.snapshot().by_code


@pytest.mark.parametrize("body", [b"{not json", b"[1, 2]", b"\xff\xfe{"],
                         ids=["garbage", "list", "bad-utf8"])
def test_corrupt_file_leaves_chain_unchanged_and_records_failopen_class_only(home, body):
    (home / "local_eligibility.json").write_bytes(body)
    assert er.filter_chain(CHAIN, TaskType.QUERY, Complexity.SIMPLE) == CHAIN
    assert failopen.snapshot().by_code.get("CHZ-FO-ELIGIBILITY-ROUTING-UNREADABLE") == 1
    rows = [json.loads(x) for x in failopen.store_path().read_text().splitlines()]
    row = next(r for r in rows if r["c"] == "CHZ-FO-ELIGIBILITY-ROUTING-UNREADABLE")
    assert set(row) <= {"c", "ts", "e"} and row["e"].endswith("Error")   # class only, no text
    # Same file version: not re-parsed, not re-recorded.
    er.filter_chain(CHAIN, TaskType.QUERY, Complexity.SIMPLE)
    assert failopen.snapshot().by_code.get("CHZ-FO-ELIGIBILITY-ROUTING-UNREADABLE") == 1


@pytest.mark.parametrize("tt", [TaskType.CODE, TaskType.IMAGE, "tech_op", "summary"])
def test_task_type_without_a_failed_lane_is_unchanged(home, tt):
    # The file holds only a failed qa lane: code has its own (absent) lane, image and the
    # strings have no router lane at all.
    _write(home, _qa(FAILED))
    assert er.filter_chain(CHAIN, tt, Complexity.SIMPLE) == CHAIN


def test_mapping_is_qa_population_plus_code():
    assert er.TASK_LANE == {"query": "qa", "research": "qa", "generate": "qa", "analyze": "qa",
                            "coordinate": "qa", "introspect": "qa", "code": "code"}


def test_failed_code_lane_drops_local_for_code_only(home):
    _write(home, {"code": {"simple": FAILED, "moderate": FAILED}})
    assert er.filter_chain(CHAIN, TaskType.CODE, Complexity.MODERATE) == [GEMINI, OTHER]
    assert er.filter_chain(CHAIN, TaskType.QUERY, Complexity.MODERATE) == CHAIN


@pytest.mark.parametrize("tier", [Complexity.COMPLEX, Complexity.DEEP_REASONING])
def test_tiers_without_a_lane_are_unchanged(home, tier):
    _write(home, _qa(FAILED))
    assert er.filter_chain(CHAIN, TaskType.QUERY, tier) == CHAIN


def test_only_the_failed_tier_is_acted_on(home):
    _write(home, {"qa": {"simple": FAILED, "moderate": ELIGIBLE}})
    assert BAD not in er.filter_chain(CHAIN, TaskType.QUERY, Complexity.SIMPLE)
    assert er.filter_chain(CHAIN, TaskType.QUERY, Complexity.MODERATE) == CHAIN


def test_untagged_names_match_latest_and_non_local_is_never_matched(home):
    (home / "local_eligibility.json").write_text(json.dumps({"fixture-bad": _qa(FAILED)}))
    chain = ["ollama/fixture-bad:latest", "mystery/fixture-bad", GEMINI]  # F109: local non-ollama ids match too
    assert er.filter_chain(chain, TaskType.QUERY, Complexity.SIMPLE) == chain[1:]


def test_opt_out_env_restores_the_chain(home, monkeypatch):
    _write(home, _qa(FAILED))
    monkeypatch.setenv("LLM_ROUTER_IGNORE_ELIGIBILITY", "1")
    assert er.filter_chain(CHAIN, TaskType.QUERY, Complexity.SIMPLE) == CHAIN


def test_skip_is_recorded_with_lane_counts_and_source(home):
    _write(home, _qa(FAILED))
    skip_causes.begin()
    er.filter_chain(CHAIN, TaskType.ANALYZE, Complexity.MODERATE)
    assert skip_causes.entries_so_far() == [{
        "model": BAD, "reason": "failed_eligibility", "lane": "qa/moderate",
        "k": 13, "n": 113, "source": "p2.0-requal",
    }]
    assert skip_causes.FAILED_ELIGIBILITY in skip_causes.BUILD_REASONS


# ── cache ──────────────────────────────────────────────────────────────────

def test_cache_parses_once_per_file_version_and_invalidates_on_mtime(home, monkeypatch):
    calls = []
    real = er._parse
    monkeypatch.setattr(er, "_parse", lambda text: calls.append(1) or real(text))
    _write(home, _qa(FAILED))
    path = home / "local_eligibility.json"
    os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    for _ in range(5):
        assert BAD not in er.filter_chain(CHAIN, TaskType.QUERY, Complexity.SIMPLE)
    assert len(calls) == 1
    # A new file version (new mtime) must be re-read: the lane is now power-limited.
    _write(home, _qa({**FAILED, "verdict": "inconclusive"}))
    os.utime(path, ns=(1_700_000_001_000_000_000, 1_700_000_001_000_000_000))
    assert er.filter_chain(CHAIN, TaskType.QUERY, Complexity.SIMPLE) == CHAIN
    assert len(calls) == 2


# ── router: route_and_call ─────────────────────────────────────────────────

@pytest.fixture
def env(home, mock_env, temp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_PROVIDER_RESET_PATH", str(tmp_path / "provider_reset.json"))
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    monkeypatch.setenv("LLM_ROUTER_PROMOTE_GATE", "off")
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")
    return temp_db


def _resp(model: str) -> LLMResponse:
    return LLMResponse(content="an answer long enough to be usable", model=model, input_tokens=42,
                       output_tokens=12, cost_usd=0.0, latency_ms=10.0,
                       provider=model.split("/", 1)[0])


async def _route(chain, *, task=TaskType.QUERY, serve: str | None = None, override=None,
                 call_kw: dict | None = None):
    """Route with a stubbed chain; every model fails except ``serve``. Returns models tried.
    ``call_kw`` replaces the default ``complexity_hint="simple"`` (e.g. an llm_route shape)."""
    from llm_router.router import route_and_call

    tried: list[str] = []

    async def _build(*a, **k):
        return list(chain)

    async def _call_text(model, *a, **k):
        tried.append(model)
        if model != serve:
            raise RuntimeError("boom")
        return _resp(model)

    with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.router._build_and_filter_chain", _build), \
         patch("llm_router.router._call_text", _call_text):
        kw = {"complexity_hint": "simple"} if call_kw is None else call_kw
        call = route_and_call(task, "In one sentence, what does `wc -l` do?",
                              model_override=override, **kw)
        if serve is None:
            with pytest.raises(Exception):  # noqa: B017,PT011 - every model fails by design
                await call
        else:
            await call
    return tried


@pytest.mark.asyncio
async def test_router_never_tries_the_failed_local_model(env, home):
    _write(home, _qa(FAILED))
    tried = await _route(CHAIN)
    assert BAD not in tried
    assert GEMINI in tried and OTHER in tried


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "eligible", "not_informative"])
async def test_router_chain_unchanged_without_a_failed_lane(env, home, state):
    if state != "missing":
        _write(home, _qa(ELIGIBLE if state == "eligible" else NOT_INFORMATIVE))
    tried = await _route(CHAIN)
    assert set(tried) == set(CHAIN)


@pytest.mark.asyncio
async def test_router_task_type_of_another_lane_unchanged(env, home):
    _write(home, _qa(FAILED))
    tried = await _route(CHAIN, task=TaskType.CODE)
    assert set(tried) == set(CHAIN)


@pytest.mark.asyncio
async def test_router_explicit_override_is_left_alone(env, home):
    _write(home, _qa(FAILED))
    tried = await _route([BAD], override=BAD)
    assert tried and tried[0] == BAD


@pytest.mark.asyncio
async def test_router_empty_after_skip_names_the_cause(env, home):
    from llm_router.router import NoRoutableModelsError, route_and_call

    _write(home, _qa(FAILED))

    async def _build(*a, **k):
        return [BAD]

    with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.router._build_and_filter_chain", _build):
        with pytest.raises(NoRoutableModelsError, match="LLM_ROUTER_IGNORE_ELIGIBILITY"):
            await route_and_call(TaskType.QUERY, "In one sentence, what does `wc -l` do?",
                                 complexity_hint="simple")


@pytest.mark.asyncio
async def test_reason_is_in_the_ledger_and_in_why(env, home, tmp_path):
    from llm_router import why

    _write(home, _qa(FAILED))
    await _route([BAD, GEMINI], serve=GEMINI)
    con = sqlite3.connect(str(env))
    con.row_factory = sqlite3.Row
    try:
        (row,) = con.execute("SELECT * FROM routing_decisions").fetchall()
    finally:
        con.close()
    skipped = json.loads(row["skipped_json"])
    assert skipped == [{"model": BAD, "reason": "failed_eligibility", "lane": "qa/simple",
                        "k": 13, "n": 113, "source": "p2.0-requal"}]
    assert row["served_by_last_resort"] == 0     # a build-time drop, not a loop skip

    base = tmp_path / "why"
    base.mkdir()
    shutil.copy(env, base / "usage.db")
    recs, _ = why.last_decisions(1, base=base)
    text = why.render(recs[0])
    assert f"{BAD}: failed_eligibility (qa/simple 13/113, p2.0-requal)" in text
    assert "wc -l" not in text and "wc -l" not in json.dumps(recs)


@pytest.mark.asyncio
async def test_emergency_budget_chain_is_filtered_too(env, home):
    from llm_router.router import route_and_call
    from llm_router.types import RoutingProfile

    _write(home, _qa(FAILED))
    tried: list[str] = []

    async def _build(task_type, profile, *a, **k):
        return [BAD, OTHER] if profile == RoutingProfile.BUDGET else [GEMINI]

    async def _call_text(model, *a, **k):
        tried.append(model)
        raise RuntimeError("boom")

    with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.router._build_and_filter_chain", _build), \
         patch("llm_router.router._call_text", _call_text):
        with pytest.raises(Exception):  # noqa: B017,PT011 - every model fails by design
            await route_and_call(TaskType.QUERY, "In one sentence, what does `wc -l` do?",
                                 complexity_hint="moderate")
    assert GEMINI in tried and OTHER in tried   # the emergency chain ran
    assert BAD not in tried


# ── repair round 1 (rv-634) ────────────────────────────────────────────────

def _llm_route_kw(complexity: str) -> dict:
    """The shape MCP llm_route passes: an explicit profile, the classifier's complexity in
    classification_data, no complexity_hint (tools/routing.py)."""
    from llm_router.types import RoutingProfile

    return {"profile": RoutingProfile.PREMIUM,
            "classification_data": {"complexity": complexity, "classifier_type": "fixture"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("complexity", ["complex", "deep_reasoning"])
async def test_llm_route_shape_complex_call_keeps_the_chain(env, home, complexity):
    # rv-634 must-fix: with profile= the resolved complexity stays MODERATE; the lane tier must
    # come from classification_data, so a complex call reads no lane and nothing is dropped.
    _write(home, _qa(FAILED))
    tried = await _route(CHAIN, call_kw=_llm_route_kw(complexity))
    assert set(tried) == set(CHAIN)


@pytest.mark.asyncio
async def test_llm_route_shape_simple_call_reads_the_simple_lane(env, home):
    # Only the simple lane failed: a simple llm_route call must read it (not moderate).
    _write(home, {"qa": {"simple": FAILED, "moderate": ELIGIBLE}})
    tried = await _route(CHAIN, call_kw=_llm_route_kw("simple"))
    assert BAD not in tried
    assert GEMINI in tried and OTHER in tried


@pytest.mark.asyncio
async def test_llm_route_shape_moderate_call_reads_the_moderate_lane(env, home):
    _write(home, {"qa": {"simple": FAILED, "moderate": ELIGIBLE}})
    tried = await _route(CHAIN, call_kw=_llm_route_kw("moderate"))
    assert set(tried) == set(CHAIN)


@pytest.mark.asyncio
async def test_llm_route_shape_emergency_chain_uses_the_call_tier(env, home):
    from llm_router.router import route_and_call
    from llm_router.types import RoutingProfile

    _write(home, _qa(FAILED))
    tried: list[str] = []

    async def _build(task_type, profile, *a, **k):
        return [BAD, OTHER] if profile == RoutingProfile.BUDGET else [GEMINI]

    async def _call_text(model, *a, **k):
        tried.append(model)
        raise RuntimeError("boom")

    with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.router._build_and_filter_chain", _build), \
         patch("llm_router.router._call_text", _call_text):
        with pytest.raises(Exception):  # noqa: B017,PT011 - every model fails by design
            await route_and_call(TaskType.QUERY, "In one sentence, what does `wc -l` do?",
                                 **_llm_route_kw("complex"))
    assert BAD in tried and OTHER in tried   # complex call: the emergency chain is not filtered


def test_lane_complexity_order():
    from llm_router.router import _lane_complexity

    m, s_, cx = Complexity.MODERATE, Complexity.SIMPLE, Complexity.COMPLEX
    assert _lane_complexity("simple", {"complexity": "complex"}, m) == s_     # hint wins
    assert _lane_complexity(cx, None, m) == cx
    assert _lane_complexity(None, {"complexity": "complex"}, m) == cx         # then classifier
    assert _lane_complexity(None, {"complexity": "bogus"}, s_) == s_          # then c
    assert _lane_complexity("bogus", {"complexity": "complex"}, m) == m       # bad hint -> c
    assert _lane_complexity(None, {}, s_) == s_
    assert _lane_complexity(None, None, s_) == s_


@pytest.mark.asyncio
async def test_filter_raises_keeps_chain_and_records_failopen(env, home, monkeypatch):
    _write(home, _qa(FAILED))

    def _boom(*a, **k):
        raise RuntimeError("filter bug")

    monkeypatch.setattr(er, "filter_chain", _boom)
    tried = await _route(CHAIN)
    assert set(tried) == set(CHAIN)
    assert failopen.snapshot().by_code.get("CHZ-FO-ROUTER-ELIGIBILITY-FILTER") == 1


def test_stat_error_records_failopen_once_per_path_and_class(home, monkeypatch):
    code = "CHZ-FO-ELIGIBILITY-ROUTING-UNREADABLE"
    blocker = home / "not-a-dir"
    blocker.write_text("x")
    # A real non-ENOENT OSError: a path component is a file (NotADirectoryError).
    monkeypatch.setattr(er, "eligibility_path", lambda: blocker / "local_eligibility.json")
    for _ in range(3):
        assert er.filter_chain(CHAIN, TaskType.QUERY, Complexity.SIMPLE) == CHAIN
    assert failopen.snapshot().by_code.get(code) == 1

    class _Denied:
        def stat(self):
            raise PermissionError(13, "denied")

        def __str__(self):
            return str(home / "denied" / "local_eligibility.json")

    monkeypatch.setattr(er, "eligibility_path", lambda: _Denied())
    for _ in range(3):
        assert er.filter_chain(CHAIN, TaskType.QUERY, Complexity.SIMPLE) == CHAIN
    assert failopen.snapshot().by_code.get(code) == 2   # a new (path, class): once more


def test_source_is_capped_in_the_skip_record_and_in_why(home):
    from llm_router import why

    _write(home, _qa({**FAILED, "source": "s" * 500}))
    skip_causes.begin()
    er.filter_chain(CHAIN, TaskType.QUERY, Complexity.SIMPLE)
    (entry,) = skip_causes.entries_so_far()
    assert entry["source"] == "s" * er.SOURCE_MAX
    note = why._lane_note({"lane": "qa/simple", "k": 1, "n": 2, "source": "s" * 500})
    assert note == f" (qa/simple 1/2, {'s' * er.SOURCE_MAX})"


@pytest.mark.asyncio
async def test_daily_cap_with_eligibility_drop_names_both_causes(env, home, monkeypatch):
    from llm_router.repo_config import RepoConfig
    from llm_router.router import route_and_call
    from llm_router.types import BudgetExceededError

    _write(home, _qa(FAILED))
    monkeypatch.setenv("LLM_ROUTER_ENFORCE", "hard")
    repo_cfg = RepoConfig(daily_caps={"query": 0.0001}, enforce="hard")

    async def _build(*a, **k):
        return [BAD, GEMINI]

    with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.cost.get_daily_spend_by_task_type", new_callable=AsyncMock,
               return_value=9999.0), \
         patch("llm_router.repo_config.effective_config", return_value=repo_cfg), \
         patch("llm_router.policy.load_org_policy", return_value=None), \
         patch("llm_router.router._build_and_filter_chain", _build):
        with pytest.raises(BudgetExceededError) as ei:
            await route_and_call(TaskType.QUERY, "In one sentence, what does `wc -l` do?",
                                 complexity_hint="simple")
    msg = str(ei.value)
    assert "daily limit" in msg                       # the cap
    assert BAD in msg and "F106" in msg               # and the eligibility drop
    assert "LLM_ROUTER_IGNORE_ELIGIBILITY" in msg
