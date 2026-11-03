"""W7 router side: infrastructure outcomes must not demote a lane (F15), the structured sink of
``_llm_text_job`` (F8), and the lane diagnosis behind the "No passed local-check lane" message (F16).

Everything is stubbed: ``route_and_call`` is a fake, digests and Ollama settings are fixtures, HOME and
LLM_ROUTER_HOME are tmp dirs. No model, no Ollama, no network.
"""
from __future__ import annotations

import asyncio
import errno
import json
import random
import socket
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_router import capped_log, failopen, paths
from llm_router import eligibility_routing as er
from llm_router import text_job_ledger as L
from llm_router.local_check import routing as lr
from llm_router.resolver import eligibility_store as store
from llm_router.text_jobs import CHECK_CODES, TextJobResult, check_output
from llm_router.tools import text as T
from llm_router.types import DeadlineExceeded, LLMResponse, WallClockExceeded
from tests.test_lc8_routing import BARE, DIGEST, JOB, LOCAL, NOW, PASS70, PROV, SHA

FLAG = "LLM_ROUTER_LOCAL_TEXT_JOBS"
DIFF = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-a\n+b\n"
GOOD = "app: set x to 1 in app.py"
MEASURED = 8192
PARITY = {**PROV, "ctx": MEASURED, "think": False}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router_home"))
    for name in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "LLM_ROUTER_IGNORE_ELIGIBILITY", FLAG):
        monkeypatch.delenv(name, raising=False)
    er.reset_cache()
    yield tmp_path / "router_home"
    er.reset_cache()


def attempts() -> list[dict]:
    return capped_log.read_dicts(paths.state_path(L.LOCAL_ATTEMPTS_FILENAME))


def finals() -> list[dict]:
    return capped_log.read_dicts(paths.state_path(L.LEDGER_FILENAME))


def write_attempts(outcomes: list[str], *, model: str = LOCAL, job: str = JOB, t0: float | None = None) -> None:
    """Append sidecar rows the way ``record_local_attempts`` writes them (ts after the lane's ``since``)."""
    t0 = time.time() + 5 if t0 is None else t0
    path = paths.state_path(L.LOCAL_ATTEMPTS_FILENAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for i, o in enumerate(outcomes):
            fh.write(json.dumps({"ts": t0 + i * 0.001, "job": job, "model": model, "outcome": o}) + "\n")


def exhausted(cause: BaseException, *models: str) -> RuntimeError:
    """The router's chain-exhaustion error: ``RuntimeError ... from last_error`` with the failures listed."""
    models = models or (LOCAL,)
    lines = "\n".join(f"  {i + 1}. {m}: {type(cause).__name__}: {cause}" for i, m in enumerate(models))
    err = RuntimeError(f"All models failed for generate/balanced. Last error: {cause}.\nChain failures:\n{lines}\n")
    err.__cause__ = cause
    err.chain_attempts = list(models)
    return err


def litellm_exc(name: str, message: str = "x", **extra) -> Exception:
    from litellm import exceptions as lx
    return getattr(lx, name)(message=message, model="m", llm_provider="ollama", **extra)


# ------------------------------------------------------------------ 1. classification
CLASSIFICATION = [
    # infrastructure, positively identified
    ("builtin TimeoutError", lambda: TimeoutError("queued"), True),
    ("asyncio.TimeoutError", lambda: asyncio.TimeoutError(), True),
    ("router WallClockExceeded", lambda: WallClockExceeded("cap", cap_seconds=1.0, elapsed_seconds=2.0), True),
    ("router DeadlineExceeded", lambda: DeadlineExceeded("late", deadline_monotonic=1.0), True),
    ("ConnectionRefusedError", lambda: ConnectionRefusedError(errno.ECONNREFUSED, "refused"), True),
    ("ConnectionResetError", lambda: ConnectionResetError(), True),
    ("BrokenPipeError", lambda: BrokenPipeError(), True),
    ("socket.gaierror", lambda: socket.gaierror(8, "nodename nor servname"), True),
    ("OSError host unreachable", lambda: OSError(errno.EHOSTUNREACH, "no route"), True),
    ("litellm Timeout", lambda: litellm_exc("Timeout"), True),
    ("litellm APIConnectionError", lambda: litellm_exc("APIConnectionError"), True),
    ("litellm ServiceUnavailableError", lambda: litellm_exc("ServiceUnavailableError"), True),
    ("litellm InternalServerError", lambda: litellm_exc("InternalServerError"), True),
    ("litellm BadGatewayError", lambda: litellm_exc("BadGatewayError"), True),
    ("litellm 404 model not found", lambda: litellm_exc("NotFoundError", "model 'q:1b' not found, try pulling it"), True),
    ("APIError with a 503 status", lambda: litellm_exc("APIError", status_code=503), True),
    ("the MCP door's attempt cap", lambda: __import__("llm_router.mcp_door", fromlist=["x"]).AttemptCapExceeded(LOCAL, 30.0), True),
    ("exhaustion error caused by a Timeout", lambda: exhausted(litellm_exc("Timeout")), True),
    ("exhaustion error two levels deep", lambda: _chained(RuntimeError("outer"), RuntimeError("mid"), TimeoutError()), True),
    # not infrastructure: stays a quality-side `error`
    ("plain RuntimeError", lambda: RuntimeError("boom"), False),
    ("ValueError", lambda: ValueError("bad"), False),
    ("KeyError", lambda: KeyError("k"), False),
    ("FileNotFoundError", lambda: FileNotFoundError(errno.ENOENT, "missing"), False),
    ("PermissionError", lambda: PermissionError(errno.EACCES, "denied"), False),
    ("a message that only SAYS timeout", lambda: RuntimeError("connection refused: request timed out"), False),
    ("litellm 404 that is not a model", lambda: litellm_exc("NotFoundError", "route not found"), False),
    ("litellm AuthenticationError", lambda: litellm_exc("AuthenticationError"), False),
    ("litellm RateLimitError", lambda: litellm_exc("RateLimitError"), False),
    ("litellm BadRequestError", lambda: litellm_exc("BadRequestError"), False),
    ("litellm ContextWindowExceededError", lambda: litellm_exc("ContextWindowExceededError"), False),
    ("APIError with a 400 status", lambda: litellm_exc("APIError", status_code=400), False),
    ("exhaustion error caused by a ValueError", lambda: exhausted(ValueError("garbage")), False),
    ("a cancellation is not a failure", lambda: asyncio.CancelledError(), False),
    ("no exception", lambda: None, False),
]


def _chained(*excs: BaseException) -> BaseException:
    for outer, inner in zip(excs, excs[1:]):
        outer.__cause__ = inner
    return excs[0]


@pytest.mark.parametrize("label,make,expected", CLASSIFICATION, ids=[c[0] for c in CLASSIFICATION])
def test_classification_table(label, make, expected):
    assert L.is_infra_error(make()) is expected


def test_the_table_has_both_classes_and_is_not_vacuous():
    assert sum(1 for _l, _m, e in CLASSIFICATION if e) >= 15 and sum(1 for _l, _m, e in CLASSIFICATION if not e) >= 10


def test_a_cause_cycle_terminates():
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    assert L.is_infra_error(a) is False


def test_a_classifier_fault_reads_as_not_infra(monkeypatch):
    monkeypatch.setattr(L, "_exc_is_infra", MagicMock(side_effect=ValueError("broken")))
    assert L.is_infra_error(TimeoutError()) is False


# ------------------------------------------------------------------ 2. what record_local_attempts writes
def test_infra_failure_of_the_only_local_model_is_recorded_as_infra():
    err = exhausted(litellm_exc("Timeout", "timed out"))
    assert L.record_local_attempts(job=JOB, served_model="", chain_attempts=err.chain_attempts,
                                   accepted=False, exc=err) == [LOCAL]
    assert [(r["model"], r["outcome"]) for r in attempts()] == [(LOCAL, "infra")]


def test_a_non_infra_failure_stays_error():
    err = exhausted(ValueError("garbage"))
    L.record_local_attempts(job=JOB, served_model="", chain_attempts=err.chain_attempts, accepted=False, exc=err)
    assert [r["outcome"] for r in attempts()] == ["error"]


def test_a_rescued_failure_without_an_error_stays_error():
    L.record_local_attempts(job=JOB, served_model="gemini/g", chain_attempts=[LOCAL, "gemini/g"], accepted=True)
    assert [r["outcome"] for r in attempts()] == ["error"]


def test_each_local_model_is_classified_on_its_own_error():
    """The exception's cause is only the LAST error: model A's garbage must not be excused by B's timeout."""
    other = "ollama/lc8-other:3b"
    err = RuntimeError(
        "All models failed.\nChain failures:\n"
        f"  1. {LOCAL}: ValueError: garbage\n  2. {other}: Timeout: timed out\n")
    err.__cause__ = litellm_exc("Timeout")
    L.record_local_attempts(job=JOB, served_model="", chain_attempts=[LOCAL, other], accepted=False, exc=err)
    assert {r["model"]: r["outcome"] for r in attempts()} == {LOCAL: "error", other: "infra"}


def test_a_model_with_no_chain_line_uses_the_exception_level_answer():
    err = TimeoutError("cut off mid-dispatch")
    err.chain_attempts = [LOCAL]
    L.record_local_attempts(job=JOB, served_model="", chain_attempts=[LOCAL], accepted=False, exc=err)
    assert [r["outcome"] for r in attempts()] == ["infra"]


def test_accepted_and_rejected_rows_are_unchanged():
    L.record_local_attempts(job=JOB, served_model=LOCAL, chain_attempts=[LOCAL], accepted=True)
    L.record_local_attempts(job=JOB, served_model=LOCAL, chain_attempts=[LOCAL], accepted=False)
    assert [r["outcome"] for r in attempts()] == ["ok", "rejected"]


# ------------------------------------------------------------------ 3. the demotion window
@pytest.fixture
def lane_on():
    lr.enable(BARE, JOB, "local_check")


def test_forty_infra_rows_never_demote(lane_on):
    write_attempts(["infra"] * 40)
    assert lr.demotion_check(LOCAL, JOB) is False and lr.is_enabled(BARE, JOB)


def test_infra_rows_do_not_fill_the_window(lane_on):
    write_attempts(["rejected"] * 19 + ["infra"] * 100)
    assert lr.demotion_check(LOCAL, JOB) is False          # 19 real attempts: the window is not full
    write_attempts(["rejected"])
    assert lr.demotion_check(LOCAL, JOB) is True           # the 20th real attempt fills it, all bad


def test_seven_rejected_of_twenty_still_demotes_and_six_does_not(lane_on):
    write_attempts(["ok"] * 14 + ["rejected"] * 6)
    assert lr.demotion_check(LOCAL, JOB) is False and lr.is_enabled(BARE, JOB)
    write_attempts(["rejected"])
    assert lr.demotion_check(LOCAL, JOB) is True
    e = lr.entry(BARE, JOB)
    assert e["enabled"] is False and e["reason"] == lr.DEMOTE_REASON


def test_mixed_window_counts_only_real_attempts(lane_on):
    outcomes: list[str] = []
    for i in range(14):
        outcomes += ["ok", "infra", "infra"]
    for i in range(6):
        outcomes += ["rejected", "infra"]
    write_attempts(outcomes)
    assert lr.demotion_check(LOCAL, JOB) is False          # 6 of the last 20 real attempts
    write_attempts(["infra", "error", "infra"])
    assert lr.demotion_check(LOCAL, JOB) is True           # the error is a real failure: 7 of the last 20


def test_infra_rows_do_not_push_quality_rows_out_of_the_window(lane_on):
    write_attempts(["ok"] * 14 + ["rejected"] * 6 + ["infra"] * 50)
    assert lr.demotion_check(LOCAL, JOB) is False
    write_attempts(["rejected"])
    assert lr.demotion_check(LOCAL, JOB) is True


def test_quality_error_still_counts_exactly_as_before(lane_on):
    write_attempts(["ok"] * 13 + ["error"] * 7)
    assert lr.demotion_check(LOCAL, JOB) is True


def test_rows_written_before_infra_existed_read_as_before(lane_on):
    path = paths.state_path(L.LOCAL_ATTEMPTS_FILENAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time() + 5
    rows = [{"ts": t0 + i, "job": JOB, "model": LOCAL, "outcome": "ok" if i < 13 else "rejected"} for i in range(20)]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert lr.demotion_check(LOCAL, JOB) is True


# ------------------------------------------------------------------ 4. end to end through _llm_text_job
def wire(monkeypatch, route, *, lane_available: bool | None = True):
    monkeypatch.setattr(T, "route_and_call", route)
    monkeypatch.setattr(T, "_announce_routing", AsyncMock())
    monkeypatch.setattr(T, "_read_hook_route_directive", lambda: None)
    if lane_available is not None:
        monkeypatch.setattr(er, "passed_lane_available", lambda *a, **k: lane_available)


def response(content: str, model: str = LOCAL) -> LLMResponse:
    return LLMResponse(content=content, model=model, input_tokens=1, output_tokens=1, cost_usd=0.0,
                       latency_ms=1.0, provider=model.split("/", 1)[0], chain_attempts=[model])


async def run(sink: dict | None = None, *, job: str = JOB, text: str = DIFF, hint: str | None = None) -> str:
    return await T._llm_text_job(job, text, object(), hint, sink)


async def test_queueing_timeouts_record_infra_and_never_demote_the_lane(monkeypatch, lane_on):
    async def route(*a, **k):
        raise exhausted(litellm_exc("Timeout", "queued behind another request"))

    wire(monkeypatch, route)                                   # flag unset: the passed local model alone
    last_sink: dict = {}
    for _ in range(40):
        last_sink = {}
        out = await run(last_sink)
    assert "status=fallback, attempts=1" in out and "Do this job yourself." in out
    assert [r["outcome"] for r in attempts()] == ["infra"] * 40
    assert lr.is_enabled(BARE, JOB)
    assert last_sink["status"] == "fallback" and last_sink["fallback"] == "local_failed" and last_sink["infra"] is True
    assert last_sink["attempts"] == 1 and last_sink["model"] == LOCAL


async def test_a_real_local_error_still_demotes_through_the_whole_path(monkeypatch, lane_on):
    async def route(*a, **k):
        raise exhausted(ValueError("the model server returned garbage"))

    wire(monkeypatch, route)
    sink: dict = {}
    for _ in range(20):
        sink = {}
        await run(sink)
    assert [r["outcome"] for r in attempts()] == ["error"] * 20
    assert not lr.is_enabled(BARE, JOB) and lr.entry(BARE, JOB)["reason"] == lr.DEMOTE_REASON
    assert sink["infra"] is False and sink["fallback"] == "local_failed"


async def test_route_error_with_the_flag_on_fills_the_sink_before_it_propagates(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    err = exhausted(litellm_exc("ServiceUnavailableError", "server busy"))

    async def route(*a, **k):
        raise err

    wire(monkeypatch, route, lane_available=None)
    sink: dict = {}
    with pytest.raises(RuntimeError) as caught:
        await run(sink)
    assert caught.value is err
    assert sink["status"] == "fallback" and sink["fallback"] == "local_failed" and sink["infra"] is True
    assert [r["outcome"] for r in attempts()] == ["infra"] and not finals()


@pytest.mark.parametrize("exc,infra", [
    (er.PassedModelNotInChain("not in the chain"), False),
    (er.LiveCheckRefused("digest changed"), False),
    (er.LiveCheckRefused("ollama did not answer"), True),
    (er.LiveCheckRefused("model not listed"), True),
])
async def test_a_refusal_is_lane_refused_and_writes_no_local_attempt(monkeypatch, exc, infra):
    async def route(*a, **k):
        raise exc

    wire(monkeypatch, route)
    sink: dict = {}
    out = await run(sink)
    assert "status=fallback, attempts=0" in out
    assert sink["fallback"] == "lane_refused" and sink["infra"] is infra and sink["attempts"] == 0
    assert not attempts()


# ------------------------------------------------------------------ 5. cancellation records nothing
async def _cancel_mid_route(monkeypatch, sink: dict, *, first_answers: tuple[str, ...] = ()):
    entered = asyncio.Event()
    queue = list(first_answers)

    async def route(*a, **k):
        if queue:
            return response(queue.pop(0))
        entered.set()
        await asyncio.Event().wait()

    wire(monkeypatch, route)
    task = asyncio.create_task(run(sink))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancel_in_the_first_attempt_records_no_row_anywhere(monkeypatch, lane_on):
    monkeypatch.setenv(FLAG, "1")
    sink: dict = {}
    await _cancel_mid_route(monkeypatch, sink)
    assert sink == {} and not attempts() and not finals()
    assert lr.is_enabled(BARE, JOB)


async def test_cancel_with_the_flag_unset_records_no_row_anywhere(monkeypatch, lane_on):
    sink: dict = {}
    await _cancel_mid_route(monkeypatch, sink)
    assert sink == {} and not attempts() and not finals()


async def test_cancel_after_a_rejected_attempt_leaves_the_job_excluded(monkeypatch, lane_on):
    """Attempt 1 was judged (a real `rejected` row); the cancelled attempt adds no quality row, and the job gets an
    `infra` marker so the monitor never counts the half-finished job as failed (D-367)."""
    monkeypatch.setenv(FLAG, "1")
    sink: dict = {}
    await _cancel_mid_route(monkeypatch, sink, first_answers=("WIP",))
    rows = attempts()
    assert [r["outcome"] for r in rows] == ["rejected", "infra"]
    assert len({r["jid"] for r in rows}) == 1 and [r["try"] for r in rows] == [1, 2]
    assert not finals() and sink == {}
    assert lr._recent_outcomes(LOCAL, JOB, None, None) == []


@pytest.mark.timing
async def test_the_hooks_wait_for_timeout_cancels_without_a_quality_row(monkeypatch, lane_on):
    entered = asyncio.Event()

    async def route(*a, **k):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setenv(FLAG, "1")
    wire(monkeypatch, route)
    sink: dict = {}
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(run(sink), timeout=1.0)
    assert entered.is_set(), "the timeout fired before the route was reached: the test would be vacuous"
    assert sink == {} and not attempts() and not finals()


# ------------------------------------------------------------------ 6. the structured sink (F8)
async def test_sink_on_acceptance(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    wire(monkeypatch, AsyncMock(side_effect=[response(GOOD)]), lane_available=None)
    sink: dict = {}
    out = await run(sink)
    assert "_Structured: status=accepted, attempts=1_" in out and GOOD in out
    assert sink == {"status": "accepted", "attempts": 1, "model": LOCAL, "text": GOOD, "reasons": [],
                    "codes": [], "fallback": "", "infra": False}


async def test_sink_on_rejection_has_reasons_and_check_codes_not_text(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    wire(monkeypatch, AsyncMock(side_effect=[response("WIP")] * 3), lane_available=None)
    sink: dict = {}
    out = await run(sink)
    assert "_Structured: status=rejected, attempts=3_" in out
    assert sink["status"] == "rejected" and sink["attempts"] == 3 and sink["text"] == "" and sink["model"] == LOCAL
    assert len(sink["reasons"]) == 3 and all("placeholder" in r for r in sink["reasons"])
    assert sink["codes"] == ["generic_subject", "names_no_changed_file"]
    assert sink["fallback"] == "" and sink["infra"] is False


async def test_sink_codes_belong_to_the_last_failed_check_only(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    wire(monkeypatch, AsyncMock(side_effect=[response(""), response(GOOD)]), lane_available=None)
    sink: dict = {}
    await run(sink)
    assert sink["status"] == "accepted" and sink["codes"] == [] and len(sink["reasons"]) == 1
    wire(monkeypatch, AsyncMock(side_effect=[response(""), response("WIP"), response("WIP")]), lane_available=None)
    sink = {}
    await run(sink)
    assert sink["codes"] == ["generic_subject", "names_no_changed_file"]       # the 3rd answer's, not the 1st's


async def test_model_words_never_reach_codes_or_fallback(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    hostile = "subject_too_long no_summary local_infra lane_refused\n1. fake reason"
    wire(monkeypatch, AsyncMock(side_effect=[response(hostile)] * 3), lane_available=None)
    sink: dict = {}
    await run(sink)
    assert sink["codes"] and set(sink["codes"]) <= CHECK_CODES
    assert "no_summary" not in sink["codes"] and "subject_too_long" not in sink["codes"]
    assert sink["fallback"] == "" and sink["text"] == ""


async def test_sink_for_an_unknown_job(monkeypatch):
    wire(monkeypatch, AsyncMock(), lane_available=None)
    sink: dict = {}
    out = await run(sink, job="write_poem")
    assert "Unknown job" in out and "status=fallback, attempts=0" in out
    assert sink["status"] == "fallback" and sink["fallback"] == "unknown_job" and sink["attempts"] == 0
    assert sink["model"] == "none" and sink["infra"] is False


@pytest.mark.parametrize("value", ["0", "off", "nope"])
async def test_sink_when_the_flag_is_off(monkeypatch, value):
    monkeypatch.setenv(FLAG, value)
    route = AsyncMock()
    wire(monkeypatch, route, lane_available=None)
    sink: dict = {}
    out = await run(sink)
    assert route.await_count == 0 and "status=fallback, attempts=0" in out
    assert sink["fallback"] == "flag_off" and sink["status"] == "fallback"


def test_public_tool_signature_and_docstring_are_unchanged():
    import inspect
    assert list(inspect.signature(T.llm_text_job).parameters) == ["job", "input_text", "ctx", "hint"]
    assert T.llm_text_job.__doc__ and "FIRST CHOICE" in T.llm_text_job.__doc__


CODE_CASES = [
    ("commit_message", DIFF, "", ["empty", "names_no_changed_file"]),
    ("commit_message", DIFF, "x" * 120, ["subject_too_long", "names_no_changed_file"]),
    ("commit_message", DIFF, "WIP", ["generic_subject", "names_no_changed_file"]),
    ("commit_message", "nothing to see", "fix the thing", ["could_not_identify_files"]),
    ("pr_description", DIFF, "short", ["too_short", "no_summary", "no_test_plan", "names_no_changed_file"]),
    ("pr_description", DIFF, "## Summary\nChanges app.py in a good way.\n", ["no_test_plan"]),
    ("summarize_output", "FAILED tests/test_a.py:7: AssertionError\nexit code 2\n", "ok",
     ["too_short", "dropped_file_refs", "dropped_exit_codes", "dropped_error_signal"]),
]


@pytest.mark.parametrize("job,source,candidate,codes", CODE_CASES)
def test_every_check_names_its_failures_with_fixed_codes(job, source, candidate, codes):
    result = check_output(job, source, candidate)
    assert result.ok is False and result.codes == codes and set(result.codes) <= CHECK_CODES


def test_a_passing_check_has_no_codes_and_the_dataclass_stays_compatible():
    assert check_output("commit_message", DIFF, GOOD).codes == []
    assert TextJobResult("t", True) == TextJobResult(text="t", ok=True, reason=None, codes=[])


# ------------------------------------------------------------------ 7. lane diagnosis and the message (F16)
def lane(home, prov=None) -> None:
    store.record_local_check(BARE, {JOB: PASS70}, prov or PARITY, path=home / "local_eligibility.json")


def prereg_lane(home) -> None:
    p = {"grade": "passed", "verdict": "eligible", "eligible": True, "k": 60, "n": 70, "source": "prereg",
         "prereg": "PREREG-Q37", "manifest_sha256": SHA}
    store.record_competence(BARE, "text_job", {JOB: p}, path=home / "local_eligibility.json")


def diagnose(digests=None, *, ctx=MEASURED, input_len=1000, think=False) -> str:
    return er.passed_lane_diagnosis(JOB, input_len, NOW, digests={BARE: DIGEST} if digests is None else digests,
                                    ctx_for=lambda m: ctx, think=think)


def available(digests=None, *, ctx=MEASURED, input_len=1000) -> bool:
    return er.passed_lane_available(JOB, input_len, NOW, digests={BARE: DIGEST} if digests is None else digests,
                                    ctx_for=lambda m: ctx, think=False)


def test_no_enabled_lane_is_none(home):
    assert diagnose() == "none" and available() is False


def test_enabled_but_never_passed_is_none(home):
    lr.enable(BARE, JOB, "local_check")
    assert diagnose() == "none" and available() is False


def test_a_verified_passed_lane_is_ok_and_agrees_with_passed_lane_available(home):
    lane(home)
    lr.enable(BARE, JOB, "local_check")
    assert diagnose() == "ok" and available() is True


@pytest.mark.parametrize("digests", [{}, {BARE: "sha256:" + "e" * 16}], ids=["no digest cache", "digest changed"])
def test_a_digest_that_cannot_be_verified_is_unverified(home, digests):
    lane(home)
    lr.enable(BARE, JOB, "local_check")
    assert diagnose(digests) == "unverified" and available(digests) is False


def test_a_lane_whose_local_check_pass_is_stale_is_stale(home):
    lane(home)
    prereg_lane(home)
    lr.enable(BARE, JOB, "local_check")
    assert diagnose({BARE: "sha256:" + "e" * 16}) == "stale"


def test_a_passed_lane_with_no_digest_to_check_past_30_days_is_stale(home):
    lane(home)
    lr.enable(BARE, JOB, "local_check")
    late = NOW + 40 * 86400
    assert er.passed_lane_diagnosis(JOB, 1000, late, digests={}, ctx_for=lambda m: MEASURED, think=False) == "stale"
    assert er.passed_lane_diagnosis(JOB, 1000, NOW, digests={}, ctx_for=lambda m: MEASURED, think=False) == "unverified"


@pytest.mark.parametrize("kw", [{"ctx": 4096}, {"input_len": 10 ** 7}, {"think": True}],
                         ids=["ctx below measured", "input over the limit", "thinking on"])
def test_settings_that_withhold_a_verified_lane_are_settings(home, kw):
    lane(home)
    lr.enable(BARE, JOB, "local_check")
    assert diagnose(**kw) == "settings"


def test_the_gap_nearest_to_usable_wins_across_models(home):
    lane(home)
    lr.enable(BARE, JOB, "local_check")
    lr.enable("lc8-nopass:1b", JOB, "local_check")
    assert diagnose({}) == "unverified"


def test_a_disabled_lane_is_none(home):
    lane(home)
    lr.enable(BARE, JOB, "local_check")
    lr.disable(BARE, JOB, lr.DEMOTE_REASON)
    assert diagnose() == "none"


def test_the_opt_out_is_none(home, monkeypatch):
    lane(home)
    lr.enable(BARE, JOB, "local_check")
    monkeypatch.setenv("LLM_ROUTER_IGNORE_ELIGIBILITY", "1")
    assert diagnose() == "none"


def test_diagnosis_never_raises_and_records_the_fault(home, monkeypatch):
    lane(home)
    lr.enable(BARE, JOB, "local_check")
    boom = RuntimeError("store unreadable")
    monkeypatch.setattr(er, "_routable", MagicMock(side_effect=boom))
    record = MagicMock()
    monkeypatch.setattr(failopen, "record", record)
    assert diagnose() == "none"
    record.assert_called_once_with("CHZ-FO-ELIGIBILITY-PASSED-DIAGNOSIS", boom)


def test_diagnosis_does_not_write_anything(home):
    lane(home)
    lr.enable(BARE, JOB, "local_check")
    before = sorted(p.name for p in home.iterdir())
    diagnose({})
    assert sorted(p.name for p in home.iterdir()) == before


async def test_message_for_no_lane_is_todays_and_sink_says_no_lane(monkeypatch):
    wire(monkeypatch, AsyncMock(), lane_available=False)
    monkeypatch.setattr(er, "passed_lane_diagnosis", lambda *a, **k: "none")
    sink: dict = {}
    out = await run(sink)
    assert "No passed local-check lane covers commit_message" in out
    assert "enabled and passed" not in out and "status=fallback, attempts=0" in out
    assert sink["fallback"] == "no_lane" and sink["infra"] is False


@pytest.mark.parametrize("diagnosis,clause", [
    ("unverified", "its model digest could not be verified"),
    ("stale", "its local-check pass is stale"),
    ("settings", "route-time settings or the input size do not match"),
])
async def test_message_for_an_enabled_but_unusable_lane_names_the_cause(monkeypatch, diagnosis, clause):
    route = AsyncMock()
    wire(monkeypatch, route, lane_available=False)
    monkeypatch.setattr(er, "passed_lane_diagnosis", lambda *a, **k: diagnosis)
    sink: dict = {}
    out = await run(sink)
    assert "No passed local-check lane covers commit_message" in out        # the substring stays
    assert "A lane for commit_message is enabled and passed, but " in out and clause in out
    assert route.await_count == 0 and "status=fallback, attempts=0" in out
    assert sink["fallback"] == "lane_unverified" and sink["status"] == "fallback"


async def test_unverified_digest_end_to_end_with_stubbed_digests(home, monkeypatch):
    lane(home)
    lr.enable(BARE, JOB, "local_check")
    monkeypatch.setattr("llm_router.local_check.display.inventory", lambda: ([BARE], {}))   # Ollama unreachable
    monkeypatch.setattr(T, "time", SimpleNamespace(time=lambda: NOW))
    wire(monkeypatch, AsyncMock(), lane_available=None)
    sink: dict = {}
    out = await run(sink)
    assert "model digest could not be verified" in out and sink["fallback"] == "lane_unverified"


async def test_no_lane_at_all_end_to_end(home, monkeypatch):
    monkeypatch.setattr("llm_router.local_check.display.inventory", lambda: ([], {}))
    wire(monkeypatch, AsyncMock(), lane_available=None)
    sink: dict = {}
    out = await run(sink)
    assert "No passed local-check lane" in out and "enabled and passed" not in out
    assert sink["fallback"] == "no_lane"


# ------------------------------------------------------------------ 8. D-367: the monitor counts JOBS
class Clock:
    """Strictly increasing row timestamps, after the lane's `since` (second resolution)."""

    def __init__(self) -> None:
        self.t = time.time() + 5

    def tick(self) -> float:
        self.t += 0.001
        return self.t


def put(clock: Clock, jid: str | None, outcome: str, *, try_no: int | None = None, more: bool = False,
        model: str = LOCAL, job: str = JOB) -> None:
    row: dict = {"ts": clock.tick(), "job": job, "model": model, "outcome": outcome}
    if jid is not None:
        row["jid"] = jid
        if try_no is not None:
            row["try"] = try_no
        if more:
            row["more"] = True
    path = paths.state_path(L.LOCAL_ATTEMPTS_FILENAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def put_job(clock: Clock, jid: str, kind: str, tries: int = 1) -> list[str]:
    """The rows `_llm_text_job` writes for a job that is `ok` on try `tries`, or `failed` on all 3 tries."""
    outcomes = ["rejected"] * 3 if kind == "failed" else ["rejected"] * (tries - 1) + ["ok"]
    for i, o in enumerate(outcomes, 1):
        put(clock, jid, o, try_no=i, more=(o == "rejected" and i < 3))
    return outcomes


def verdicts() -> list[str]:
    return lr._recent_outcomes(LOCAL, JOB, None, None)


def test_a_job_accepted_on_a_retry_is_ok_and_one_that_failed_every_try_is_failed(home):
    c = Clock()
    put_job(c, "j1", "ok", 2)
    put_job(c, "j2", "failed")
    put_job(c, "j3", "ok", 1)
    assert verdicts() == ["ok", "failed", "ok"]


def test_job_rules_table(home):
    c = Clock()
    put(c, "a", "ok", try_no=1)                                            # ok
    put(c, "b", "rejected", try_no=1, more=True)                           # ok on a retry
    put(c, "b", "ok", try_no=2)
    put(c, "c", "rejected", try_no=1)                                      # rejected then error: failed
    put(c, "c", "error", try_no=2)
    put(c, "d", "error", try_no=1)                                         # a local error alone: failed
    put(c, "e", "infra", try_no=1)                                         # infra only: not counted
    put(c, "f", "rejected", try_no=1, more=True)                           # infra marker: not counted
    put(c, "f", "infra", try_no=2)
    put(c, "g", "infra", try_no=1)                                         # infra then ok: ok
    put(c, "g", "ok", try_no=2)
    put(c, "h", "rejected", try_no=1, more=True)                           # no verdict yet: not counted
    assert verdicts() == ["ok", "ok", "failed", "failed", "ok"]


def test_an_in_flight_job_is_counted_once_its_verdict_is_written(home):
    c = Clock()
    put(c, "x", "rejected", try_no=1, more=True)
    assert verdicts() == []
    put(c, "x", "ok", try_no=2)
    assert verdicts() == ["ok"]


def test_seven_failed_jobs_in_twenty_demote_and_six_do_not(home, lane_on):
    c = Clock()
    for i in range(14):
        put_job(c, f"o{i}", "ok", 1 + i % 3)
    for i in range(6):
        put_job(c, f"f{i}", "failed")
    assert len(verdicts()) == 20 and lr.demotion_check(LOCAL, JOB) is False and lr.is_enabled(BARE, JOB)
    put_job(c, "f6", "failed")                       # the window slides: 7 failed of the last 20
    assert lr.demotion_check(LOCAL, JOB) is True and not lr.is_enabled(BARE, JOB)


def test_retries_inside_ok_jobs_do_not_count_against_the_lane(home, lane_on):
    c = Clock()
    for i in range(20):
        put_job(c, f"r{i}", "ok", 3)                 # 40 rejected attempts, 20 ok jobs
    assert lr.demotion_check(LOCAL, JOB) is False and verdicts() == ["ok"] * 20


def test_infra_only_jobs_never_fill_the_window(home, lane_on):
    c = Clock()
    for i in range(100):
        put(c, f"i{i}", "infra", try_no=1)
    for i in range(19):
        put_job(c, f"f{i}", "failed")
    assert len(verdicts()) == 19 and lr.demotion_check(LOCAL, JOB) is False
    put_job(c, "f19", "failed")
    assert lr.demotion_check(LOCAL, JOB) is True


def test_a_file_of_old_rows_without_jid_behaves_as_before(home, lane_on):
    c = Clock()
    for o in ["ok"] * 14 + ["rejected"] * 6:
        put(c, None, o)
    assert verdicts() == ["ok"] * 14 + ["failed"] * 6 and lr.demotion_check(LOCAL, JOB) is False
    put(c, None, "error")
    assert lr.demotion_check(LOCAL, JOB) is True      # 7 of 20, one row per job


def test_old_and_new_rows_mix_in_file_order(home):
    c = Clock()
    put(c, None, "rejected")
    put(c, None, "infra")
    put_job(c, "n1", "ok", 2)
    put(c, None, "ok")
    assert verdicts() == ["failed", "ok", "ok"]


def test_each_model_is_judged_on_its_own_rows(home):
    c = Clock()
    put(c, "m", "error", try_no=1, model=LOCAL)                          # the local model errored ...
    put(c, "m", "ok", try_no=1, model="ollama/lc8-other:3b")             # ... and another local model answered
    assert verdicts() == ["failed"]
    assert lr._recent_outcomes("ollama/lc8-other:3b", JOB, None, None) == ["ok"]


def _sequence(name: str) -> list[tuple[str, int]]:
    """The four real runs, as jobs: (kind, tries to the first ok) or ("failed", 3). Order is shuffled by the caller."""
    spec = {
        "A": ([("ok", 1)] * 14 + [("ok", 2)] * 17 + [("ok", 3)] * 3 + [("failed", 3)] * 6),
        "B0": ([("ok", 1)] * 15 + [("ok", 2)] * 20 + [("ok", 3)] * 1 + [("failed", 3)] * 4),
        "B1": ([("ok", 1)] * 35 + [("ok", 2)] * 4 + [("failed", 3)] * 1),
        "B2": ([("ok", 1)] * 31 + [("ok", 2)] * 8 + [("failed", 3)] * 1),
    }[name]
    order = list(spec)
    random.Random(0).shuffle(order)
    return order


def _per_attempt_rule(attempt_outcomes: list[str]) -> int | None:
    """The rule before D-367, computed from the same attempt rows: the attempt number at which more than 30% of the
    last 20 attempts were not ok (needs a full window), else None."""
    for end in range(lr.DEMOTE_WINDOW, len(attempt_outcomes) + 1):
        window = attempt_outcomes[end - lr.DEMOTE_WINDOW:end]
        if sum(1 for o in window if o != "ok") / lr.DEMOTE_WINDOW > lr.DEMOTE_RATE:
            return end
    return None


@pytest.mark.parametrize("name,failed_jobs,old_rule", [
    ("A", 6, 20), ("B0", 4, 20), ("B1", 1, None), ("B2", 1, "computed"),
])
def test_real_sequences_replayed_as_jobs(home, lane_on, name, failed_jobs, old_rule):
    jobs = _sequence(name)
    assert len(jobs) == 40 and sum(1 for k, _t in jobs if k == "failed") == failed_jobs
    c = Clock()
    attempt_rows: list[str] = []
    seen_max = 0
    for i, (kind, tries) in enumerate(jobs):
        attempt_rows += put_job(c, f"{name}{i}", kind, tries)
        v = verdicts()
        if len(v) == lr.DEMOTE_WINDOW:
            seen_max = max(seen_max, v.count("failed"))
        assert lr.demotion_check(LOCAL, JOB) is False, f"job {i + 1} demoted the lane"
    assert lr.is_enabled(BARE, JOB)
    # the maximum failed-jobs count in any 20-job window, from the monitor's own view and from the plain sequence
    plain = [k for k, _t in jobs]
    by_hand = max(plain[s:s + 20].count("failed") for s in range(len(plain) - 19))
    assert seen_max == by_hand and seen_max <= failed_jobs <= 6
    # the change: the per-attempt rule, computed from the same rows
    old = _per_attempt_rule(attempt_rows)
    if old_rule == "computed":
        assert old is None or old >= lr.DEMOTE_WINDOW
    else:
        assert old == old_rule, f"{name}: per-attempt rule gave {old}"


def test_the_per_attempt_rule_would_have_demoted_a_and_b0_and_not_b1():
    got = {}
    for name in ("A", "B0", "B1"):
        rows: list[str] = []
        for kind, tries in _sequence(name):
            rows += ["rejected"] * 3 if kind == "failed" else ["rejected"] * (tries - 1) + ["ok"]
        got[name] = _per_attempt_rule(rows)
    assert got == {"A": 20, "B0": 20, "B1": None}


async def test_every_row_of_one_call_shares_one_jid_and_a_new_call_gets_a_new_one(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    wire(monkeypatch, AsyncMock(side_effect=[response("WIP"), response(GOOD), response("WIP"), response("WIP"),
                                              response("WIP")]), lane_available=None)
    await run()
    await run()
    rows = attempts()
    assert [(r["outcome"], r["try"], r.get("more", False)) for r in rows] == [
        ("rejected", 1, True), ("ok", 2, False),
        ("rejected", 1, True), ("rejected", 2, True), ("rejected", 3, False)]
    first, second = {r["jid"] for r in rows[:2]}, {r["jid"] for r in rows[2:]}
    assert len(first) == 1 and len(second) == 1 and first != second
    assert all(len(j) == 12 for j in first | second)
    assert lr._recent_outcomes(LOCAL, "commit_message", None, None) == ["ok", "failed"]


async def test_a_route_error_after_a_rejected_try_counts_by_its_own_rows(monkeypatch, lane_on):
    """The failed try wrote its own `error` row, so the job has its verdict: failed (not an unfinished job)."""
    monkeypatch.setenv(FLAG, "1")
    wire(monkeypatch, AsyncMock(side_effect=[response("WIP"), exhausted(ValueError("garbage"))]), lane_available=None)
    with pytest.raises(RuntimeError):
        await run()
    rows = attempts()
    assert [r["outcome"] for r in rows] == ["rejected", "error"] and len({r["jid"] for r in rows}) == 1
    assert verdicts() == ["failed"]


async def test_an_error_that_leaves_the_try_unjudged_marks_the_job_unfinished(monkeypatch, lane_on):
    """Try 1 rejected, then the route is refused before any model ran: no row for try 2 -> an infra marker."""
    wire(monkeypatch, AsyncMock(side_effect=[response("WIP"), er.PassedModelNotInChain("gone")]))
    sink: dict = {}
    await run(sink)
    rows = attempts()
    assert [r["outcome"] for r in rows] == ["rejected", "infra"] and rows[1]["try"] == 2
    assert sink["fallback"] == "lane_refused" and verdicts() == []


# ------------------------------------------------------------------ 9. F18: the per-attempt cap
def slow_then(*answers: str):
    """A route stub whose first call hangs (until the cap cancels it, as the router would annotate) and whose later
    calls return `answers` in order; after those, it hangs again."""
    queue = list(answers)
    state = {"calls": 0, "prompts": []}

    async def route(*args, **kwargs):
        state["calls"] += 1
        state["prompts"].append(args[1])
        if state["calls"] > 1 and queue:
            return response(queue.pop(0))
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError as cancelled:
            cancelled.chain_attempts = [LOCAL]
            raise

    return route, state


CAP = 0.1


@pytest.mark.timing
async def test_one_slow_attempt_then_a_fast_good_one_is_accepted(monkeypatch, lane_on):
    route, state = slow_then(GOOD)
    wire(monkeypatch, route)
    sink: dict = {}
    out = await T._llm_text_job(JOB, DIFF, object(), None, sink, attempt_timeout_s=CAP)
    assert state["calls"] == 2 and "_Structured: status=accepted, attempts=2_" in out and GOOD in out
    assert "rejected" not in state["prompts"][1]            # a timeout is not fed back as a rejected answer
    assert sink["status"] == "accepted" and sink["reasons"] == ["attempt timed out"] and sink["infra"] is False
    rows = attempts()
    assert [(r["outcome"], r["try"]) for r in rows] == [("infra", 1), ("ok", 2)] and len({r["jid"] for r in rows}) == 1
    assert verdicts() == ["ok"] and [r["status"] for r in finals()] == ["accepted"]


@pytest.mark.timing
async def test_all_attempts_slow_is_an_infra_fallback_with_no_quality_rows(monkeypatch, lane_on):
    route, state = slow_then()
    wire(monkeypatch, route)
    sink: dict = {}
    out = await T._llm_text_job(JOB, DIFF, object(), None, sink, attempt_timeout_s=CAP)
    assert state["calls"] == 3
    assert "_Structured: status=fallback, attempts=3_" in out and "Do this job yourself." in out and "Rejected" not in out
    assert sink["status"] == "fallback" and sink["fallback"] == "local_failed" and sink["infra"] is True
    assert sink["attempts"] == 3 and sink["reasons"] == ["attempt timed out"] * 3 and sink["text"] == ""
    assert [r["outcome"] for r in attempts()] == ["infra"] * 3
    assert verdicts() == [] and [(r["status"], r["attempts"]) for r in finals()] == [("fallback", 3)]
    assert lr.demotion_check(LOCAL, JOB) is False and lr.is_enabled(BARE, JOB)


@pytest.mark.timing
async def test_a_timed_out_attempt_then_two_rejections_is_a_fallback_not_a_rejection(monkeypatch, lane_on):
    route, state = slow_then("WIP", "WIP")
    wire(monkeypatch, route)
    sink: dict = {}
    out = await T._llm_text_job(JOB, DIFF, object(), None, sink, attempt_timeout_s=CAP)
    assert state["calls"] == 3 and "status=fallback" in out and "Rejected" not in out
    assert sink["fallback"] == "local_failed" and sink["infra"] is True and sink["codes"] == []
    assert [r["outcome"] for r in attempts()] == ["infra", "rejected", "rejected"]
    assert verdicts() == [] and [r["status"] for r in finals()] == ["fallback"]


async def test_a_timeout_the_router_raised_is_not_our_cap(monkeypatch, lane_on):
    """A WallClockExceeded from inside route_and_call is a route error (fallback, no retry), even with a cap set."""
    route = AsyncMock(side_effect=WallClockExceeded("cap", cap_seconds=1.0, elapsed_seconds=2.0))
    wire(monkeypatch, route)
    sink: dict = {}
    out = await T._llm_text_job(JOB, DIFF, object(), None, sink, attempt_timeout_s=30.0)
    assert route.await_count == 1 and "local-only routing did not complete" in out
    assert sink["fallback"] == "local_failed" and sink["infra"] is True


async def test_no_cap_means_no_timeout_wrapper(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    wire(monkeypatch, AsyncMock(side_effect=[response(GOOD)]), lane_available=None)
    for cap in (None, 0, -1.0):
        monkeypatch.setattr(T, "route_and_call", AsyncMock(side_effect=[response(GOOD)]))
        sink: dict = {}
        out = await T._llm_text_job(JOB, DIFF, object(), None, sink, attempt_timeout_s=cap)
        assert "status=accepted, attempts=1" in out
