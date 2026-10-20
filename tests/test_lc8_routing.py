"""LC-8: routing for passed local-check lanes (D-260), the routing switch, and the demotion monitor.

Tmp homes only (LLM_ROUTER_HOME -> tmp_path); fake digests and fake Ollama settings; no network, no model.
Model names are fixtures.
"""
from __future__ import annotations

import ast
import calendar
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_router import eligibility_routing as er
from llm_router.local_check import routing as lr
from llm_router.local_check import runtimes as rt
from llm_router.resolver import eligibility_store as store

LOCAL = "ollama/lc8-fx:9b"
BARE = "lc8-fx:9b"
OTHER = "ollama/lc8-other:3b"
GEMINI = "gemini/gemini-2.5-flash"
CLAUDE = "anthropic/claude-sonnet-5"
CHAIN = [GEMINI, OTHER, LOCAL, CLAUDE]
JOB = "commit_message"
DIGEST = "sha256:" + "a" * 16
SHA = "b" * 64
NOW = calendar.timegm(time.strptime("2026-10-20", "%Y-%m-%d")) + 3600
CTX = 8192
SETTINGS = rt.settings_hash(temperature=rt.TEMPERATURE, ctx=CTX, max_tokens=rt.MAX_TOKENS, think=False)
LIMIT = rt.max_prompt_chars(CTX)
PROV = {"bank_version": "v1", "bank_sha256": SHA, "settings_hash": SETTINGS, "model_digest": DIGEST,
        "runtime": "ollama", "run_id": "r1", "date": "2026-10-20",
        "scope": {"max_prompt_chars": LIMIT}}
PASS70 = {"k": 60, "n": 70, "verdict": "PASS"}
FAIL70 = {"k": 41, "n": 70, "verdict": "NOT_PASSED"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_IGNORE_ELIGIBILITY", raising=False)
    er.reset_cache()
    yield tmp_path
    er.reset_cache()


def lane_file(home: Path, result=PASS70, prov=None) -> Path:
    path = home / "local_eligibility.json"
    store.record_local_check(BARE, {JOB: result}, prov or PROV, path=path)
    return path


def pick(models=CHAIN, input_len=1000, digests=None, ctx=CTX, think=False, job=JOB, now=NOW):
    return er.prefer_passed(models, job, input_len, now,
                            digests={BARE: DIGEST} if digests is None else digests,
                            ctx_for=lambda m: ctx, think=think)


@pytest.fixture
def ready(home):
    """A fresh passed lane with routing enabled: every condition holds."""
    lane_file(home)
    lr.enable(BARE, JOB, "local_check")
    return home


# ------------------------------------------------------------------ 1. local_routing.json
def test_enable_disable_roundtrip_and_schema(home):
    assert not lr.is_enabled(BARE, JOB)
    e = lr.enable(BARE, JOB, "local_check")
    data = json.loads((home / "local_routing.json").read_text())
    assert data == {BARE: {JOB: {"enabled": True, "since": e["since"], "source": "local_check"}}}
    assert lr.is_enabled(BARE, JOB)
    lr.disable(BARE, JOB, "manual")
    d = json.loads((home / "local_routing.json").read_text())[BARE][JOB]
    assert d["enabled"] is False and d["reason"] == "manual" and d["source"] == "local_check"
    assert not lr.is_enabled(BARE, JOB)


def test_file_is_private_and_display_reads_it(home):
    from llm_router.local_check import display
    lr.enable(BARE, JOB, "manual")
    lr.enable(BARE, "pr_description", "manual")
    lr.disable(BARE, "pr_description", "manual")
    assert (home / "local_routing.json").stat().st_mode & 0o077 == 0
    assert display.routing_state() == {(BARE, JOB): True, (BARE, "pr_description"): False}


def test_bad_source_refused(home):
    with pytest.raises(ValueError):
        lr.enable(BARE, JOB, "auto")


def test_latest_spelling_is_normalised(home):
    lr.enable("tagless", JOB, "manual")
    assert lr.is_enabled("tagless:latest", JOB) and lr.is_enabled("ollama/tagless", JOB)
    lr.enable("tagged:latest", JOB, "manual")
    assert lr.is_enabled("tagged", JOB)


def test_on_pass_enables_local_check_lanes_by_default(home):
    out = lr.on_pass(BARE, ["commit_message", "pr_description"])
    assert out == {"commit_message": "enabled", "pr_description": "enabled"}
    assert lr.entry(BARE, "pr_description")["source"] == "local_check"
    assert lr.on_pass(BARE, ["commit_message"]) == {"commit_message": "already_on"}


def test_on_pass_never_reenables_a_disabled_lane(home):
    lr.enable(BARE, JOB, "local_check")
    lr.disable(BARE, JOB, "manual")
    assert lr.on_pass(BARE, [JOB]) == {JOB: "kept_off"}
    assert not lr.is_enabled(BARE, JOB)
    lr.enable(BARE, JOB, "manual")  # only an explicit enable does
    assert lr.is_enabled(BARE, JOB)


def test_prereg_only_lane_is_not_auto_enabled(home):
    """D-258: a pre-registered lane is record-only. Nothing but an explicit enable switches it on."""
    path = home / "local_eligibility.json"
    lane = {"grade": "passed", "verdict": "eligible", "eligible": True, "k": 60, "n": 70, "source": "prereg",
            "prereg": "PREREG-Q37", "manifest_sha256": SHA}
    store.record_competence(BARE, "text_job", {JOB: lane}, path=path)
    assert store.effective_text_job_lane(BARE, JOB, DIGEST, NOW, path=path).source == "prereg"
    assert not lr.is_enabled(BARE, JOB)
    assert pick() == CHAIN  # a passed prereg lane without enable: unchanged
    assert not (home / "local_routing.json").exists()
    lr.enable(BARE, JOB, "prereg")  # explicit
    assert pick()[0] == LOCAL


# ------------------------------------------------------------------ 2. prefer_passed
def test_local_first_when_all_conditions_hold(ready):
    assert pick() == [LOCAL, GEMINI, OTHER, CLAUDE]  # others keep their relative order
    assert pick(models=[LOCAL, GEMINI]) == [LOCAL, GEMINI]


def test_unchanged_when_routing_not_enabled(home):
    lane_file(home)
    assert pick() == CHAIN
    lr.enable(BARE, JOB, "local_check")
    lr.disable(BARE, JOB, "manual")
    assert pick() == CHAIN


def test_unchanged_when_lane_not_passed(home):
    lane_file(home, FAIL70)
    lr.enable(BARE, JOB, "manual")
    assert pick() == CHAIN


def test_unchanged_when_no_lane(home):
    lr.enable(BARE, JOB, "manual")
    assert pick() == CHAIN


def test_unchanged_when_other_job_enabled_only(ready):
    assert pick(job="pr_description") == CHAIN
    assert pick(job="summarize_output") == CHAIN


def test_unchanged_when_digest_changed(ready):
    assert pick(digests={BARE: "sha256:" + "c" * 16}) == CHAIN


def test_digest_missing_means_no_preference_even_inside_30_days(home):
    """LC-8b: routing needs a VERIFIED digest match; the 30-day rule is for display only."""
    lane_file(home, prov={**PROV, "model_digest": DIGEST})
    lr.enable(BARE, JOB, "local_check")
    assert pick(digests={}, now=NOW) == CHAIN
    assert pick(digests={}, now=NOW + 31 * 86400) == CHAIN


def test_unchanged_when_input_over_scope(ready):
    assert pick(input_len=LIMIT)[0] == LOCAL
    assert pick(input_len=LIMIT + 1) == CHAIN


def test_unchanged_when_lane_has_no_scope(home):
    prov = {k: v for k, v in PROV.items() if k != "scope"}
    lane_file(home, prov=prov)
    lr.enable(BARE, JOB, "local_check")
    assert pick() == CHAIN


def test_unchanged_when_settings_differ_or_unknown(ready):
    assert pick(ctx=16384) == CHAIN          # num_ctx differs from the lane's
    assert pick(think=True) == CHAIN         # thinking on at route time
    assert pick(ctx=None) == CHAIN           # unknown ctx
    assert pick(ctx=0) == CHAIN


def test_unchanged_when_model_not_in_chain_or_not_ollama(ready):
    assert pick(models=[GEMINI, OTHER, CLAUDE]) == [GEMINI, OTHER, CLAUDE]  # never adds a model
    lr.enable("gemini-2.5-flash", JOB, "manual")
    assert pick(models=[GEMINI]) == [GEMINI]


def test_unchanged_when_opted_out_or_bad_input(ready, monkeypatch):
    assert pick(input_len=-1) == CHAIN
    assert pick(models=[]) == []
    monkeypatch.setenv("LLM_ROUTER_IGNORE_ELIGIBILITY", "1")
    assert pick() == CHAIN


def test_latest_normalisation_in_lane_lookup(home):
    store.record_local_check("lc8-fx:latest", {JOB: PASS70}, PROV, path=home / "local_eligibility.json")
    lr.enable("lc8-fx", JOB, "local_check")
    out = er.prefer_passed(["x/y", "ollama/lc8-fx"], JOB, 10, NOW, digests={"lc8-fx:latest": DIGEST},
                           ctx_for=lambda m: CTX, think=False)
    assert out[0] == "ollama/lc8-fx"


def test_never_raises(ready, monkeypatch):
    monkeypatch.setattr(lr, "is_enabled", lambda *a, **k: 1 / 0)
    assert pick() == CHAIN


# ------------------------------------------------------------------ 2b. router integration (D-116 / F106 / F112)
def _drop(chain, ctxvar):
    from llm_router.router import _drop_failed_eligibility
    from llm_router.types import Complexity, TaskType
    tok = er.set_text_job_context(*ctxvar) if ctxvar else None
    try:
        return _drop_failed_eligibility(chain, TaskType.GENERATE, Complexity.SIMPLE, "verified_text_job")
    finally:
        if tok is not None:
            er.reset_text_job_context(tok)


def test_router_step_prefers_only_inside_text_job_context(ready, monkeypatch):
    monkeypatch.setattr(er, "_route_time_ctx", lambda m: CTX)
    monkeypatch.setattr(er, "_route_time_think", lambda: False)
    monkeypatch.setattr("llm_router.local_check.display.inventory", lambda: ([BARE], {BARE: DIGEST}))
    assert _drop(CHAIN, (JOB, 100))[0] == LOCAL
    assert _drop(CHAIN, None) == CHAIN                                   # exempt, but not a text-job context


def test_router_step_other_exempt_reasons_and_plain_calls_never_prefer(ready, monkeypatch):
    from llm_router.router import _drop_failed_eligibility
    from llm_router.types import Complexity, TaskType
    monkeypatch.setattr(er, "_route_time_ctx", lambda m: CTX)
    monkeypatch.setattr(er, "_route_time_think", lambda: False)
    monkeypatch.setattr("llm_router.local_check.display.inventory", lambda: ([BARE], {BARE: DIGEST}))
    tok = er.set_text_job_context(JOB, 10)
    try:
        assert _drop_failed_eligibility(CHAIN, TaskType.GENERATE, Complexity.SIMPLE, "validated_edit") == CHAIN
        assert _drop_failed_eligibility(CHAIN, TaskType.GENERATE, Complexity.SIMPLE) == CHAIN
    finally:
        er.reset_text_job_context(tok)


def test_f106_and_f112_still_apply(home, monkeypatch):
    """A failed qa lane is dropped for a plain call (F106) and kept-and-noted for the text job (F112);
    the preference never adds a model the earlier steps removed."""
    from tests.test_f106_eligibility_in_router import BAD, FAILED, _qa, _write
    from llm_router import skip_causes
    from llm_router.router import _drop_failed_eligibility
    from llm_router.types import Complexity, TaskType
    _write(home, _qa(FAILED))
    chain = [BAD, GEMINI]
    assert BAD not in _drop_failed_eligibility(chain, TaskType.GENERATE, Complexity.SIMPLE)
    skip_causes.begin()
    tok = er.set_text_job_context(JOB, 10)
    try:
        assert _drop_failed_eligibility(chain, TaskType.GENERATE, Complexity.SIMPLE, "verified_text_job") == chain
    finally:
        er.reset_text_job_context(tok)
    assert [e["reason"] for e in skip_causes.entries_so_far()] == ["eligibility_bypassed"]
    # D-116: a chain with the local model already stripped stays without it even when enabled.
    lane_file(home)
    lr.enable(BARE, JOB, "local_check")
    assert pick(models=[GEMINI, CLAUDE]) == [GEMINI, CLAUDE]


def test_llm_text_job_sets_and_resets_the_context(home, monkeypatch):
    import asyncio
    from llm_router.tools import text as T
    from llm_router.types import LLMResponse
    seen = {}

    async def fake_route(*a, **k):
        seen["ctx"] = er._text_job_ctx.get()
        seen["reason"] = k.get("eligibility_exempt_reason")
        return LLMResponse(content="feat: touch src/a.py", model="ollama/x", input_tokens=1, output_tokens=1,
                           cost_usd=0.0, latency_ms=1.0, provider="ollama")

    async def noop(*a, **k):
        return None

    class Ctx: ...

    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "1")
    monkeypatch.setattr(T, "route_and_call", fake_route)
    monkeypatch.setattr(T, "_announce_routing", noop)
    diff = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a\n+b\n"
    asyncio.run(T.llm_text_job(JOB, diff, Ctx(), hint="ab"))
    assert seen["ctx"] == (JOB, len(diff) + 2) and seen["reason"] == "verified_text_job"
    assert er._text_job_ctx.get() is None


# ------------------------------------------------------------------ 3. demotion monitor
def ledger(home: Path, statuses: list[str], model: str = LOCAL, job: str = JOB, t0: float | None = None) -> Path:
    """Per-attempt LOCAL outcomes (LC-8b sidecar); 'accepted' reads as ok, anything else is a failed attempt."""
    path = home / "text_job_local_attempts.jsonl"
    t0 = t0 if t0 is not None else time.time()
    with open(path, "a") as fh:
        for i, s in enumerate(statuses):
            fh.write(json.dumps({"ts": t0 + i, "job": job, "model": model,
                                 "outcome": "ok" if s == "accepted" else s}) + "\n")
    return path


def test_demotes_at_7_of_20_and_not_at_6(home):
    lr.enable(BARE, JOB, "local_check")
    t0 = time.time() + 5
    ledger(home, ["accepted"] * 14 + ["rejected"] * 6, t0=t0)
    assert lr.demotion_check(LOCAL, JOB) is False and lr.is_enabled(BARE, JOB)   # 6 of 20
    ledger(home, ["rejected"], t0=t0 + 100)                                      # window slides: 7 of the last 20
    assert lr.demotion_check(LOCAL, JOB) is True
    e = lr.entry(BARE, JOB)
    assert e["enabled"] is False and e["reason"] == "demoted: fallbacks"
    assert lr.doctor_lines() and "demoted" in lr.doctor_lines()[0] and JOB in lr.doctor_lines()[0]


def test_needs_a_full_window_and_ignores_other_models_jobs_and_old_rows(home):
    lr.enable(BARE, JOB, "local_check")
    t0 = time.time() + 5
    ledger(home, ["rejected"] * 19, t0=t0)
    assert lr.demotion_check(LOCAL, JOB) is False                      # 19 calls: window not full
    ledger(home, ["rejected"] * 20, model=OTHER, t0=t0)
    ledger(home, ["rejected"] * 20, job="pr_description", t0=t0)
    assert lr.demotion_check(LOCAL, JOB) is False
    ledger(home, ["rejected"] * 20, t0=time.time() - 86400)            # before enabling: ignored
    assert lr.demotion_check(LOCAL, JOB) is False and lr.is_enabled(BARE, JOB)


def test_rejected_counts_but_a_disabled_lane_is_untouched_and_never_reenabled(home):
    ledger(home, ["rejected"] * 20, t0=time.time() + 5)
    assert lr.demotion_check(LOCAL, JOB) is False                      # no entry
    lr.enable(BARE, JOB, "local_check")
    lr.disable(BARE, JOB, "manual")
    assert lr.demotion_check(LOCAL, JOB) is False
    assert lr.entry(BARE, JOB)["reason"] == "manual"
    lr.enable(BARE, JOB, "local_check")
    ledger(home, ["accepted"] * 20, t0=time.time() + 50)
    lr.disable(BARE, JOB, lr.DEMOTE_REASON)
    assert lr.demotion_check(LOCAL, JOB) is False and not lr.is_enabled(BARE, JOB)
    assert lr.on_pass(BARE, [JOB]) == {JOB: "kept_off"}


def test_monitor_survives_a_broken_ledger(home):
    lr.enable(BARE, JOB, "local_check")
    (home / "text_job_local_attempts.jsonl").write_bytes(b"\xff\xfe not json\n{\"a\": 1}\n[1]\n")
    assert lr.demotion_check(LOCAL, JOB) is False


def test_llm_text_job_calls_the_monitor(home, monkeypatch):
    import asyncio
    from llm_router.tools import text as T
    from llm_router.types import LLMResponse
    calls = []
    monkeypatch.setattr(lr, "demotion_check", lambda m, j, **k: calls.append((m, j)))

    async def fake_route(*a, **k):
        return LLMResponse(content="nothing useful", model="ollama/x:1b", input_tokens=1, output_tokens=1,
                           cost_usd=0.0, latency_ms=1.0, provider="ollama")

    async def noop(*a, **k):
        return None

    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "1")
    monkeypatch.setattr(T, "route_and_call", fake_route)
    monkeypatch.setattr(T, "_announce_routing", noop)
    asyncio.run(T.llm_text_job(JOB, "diff --git a/f b/f\n", object()))
    assert calls == [("ollama/x:1b", JOB)]


# ------------------------------------------------------------------ 4. no HTTP, no proxy, no authority
def test_no_http_on_the_routing_path(ready, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network on the routing path")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    import http.client
    monkeypatch.setattr(http.client.HTTPConnection, "connect", boom)
    monkeypatch.setattr("llm_router.local_check.display.inventory", lambda: ([BARE], {BARE: DIGEST}))
    monkeypatch.setattr(er, "_route_time_ctx", lambda m: CTX)
    monkeypatch.setattr(er, "_route_time_think", lambda: False)
    assert er.prefer_passed(CHAIN, JOB, 10, NOW)[0] == LOCAL           # default digest/ctx/think readers, no HTTP
    ledger(ready, ["accepted"] * 20)
    assert lr.demotion_check(LOCAL, JOB) is False


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            out.add(base)
            out |= {f"{base}.{a.name}" for a in node.names}
    return out


def test_new_code_imports_no_proxy_or_authority_module():
    import llm_router
    root = Path(llm_router.__file__).parent
    for rel in ("local_check/routing.py", "eligibility_routing.py"):
        bad = [m for m in _imports(root / rel) if "proxy" in m or "authority" in m]
        assert not bad, (rel, bad)
    assert er.prefer_passed.__module__ == "llm_router.eligibility_routing"


def test_routing_modules_load_without_proxy_in_a_fresh_process():
    code = ("import sys\nfrom llm_router import eligibility_routing\nfrom llm_router.local_check import routing\n"
            "bad=[m for m in sys.modules if m.startswith('llm_router.proxy') or m.endswith('authority')]\n"
            "sys.exit(1 if bad else 0)\n")
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd="/")
    assert r.returncode == 0, r.stderr


def test_doctor_prints_a_line_for_a_demoted_lane(home, capsys):
    from llm_router.commands import doctor
    lr.enable(BARE, JOB, "local_check")
    ledger(home, ["accepted"] * 13 + ["rejected"] * 7, t0=time.time() + 5)
    assert lr.demotion_check(LOCAL, JOB) is True
    doctor._local_text_jobs_section()
    out = capsys.readouterr().out
    assert f"{BARE} — {JOB} routing demoted" in out and f"local-check enable {JOB}" in out
