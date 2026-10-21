"""LC-8c: bounded ledgers, failure-path demotion, and input-limit diagnostics."""
from __future__ import annotations

import asyncio
import calendar
import json
from unittest.mock import AsyncMock

import pytest

from llm_router import capped_log, eligibility_routing as er, paths, skip_causes
from llm_router import text_job_ledger as L
from llm_router.local_check import routing as lr
from llm_router.local_check import runtimes as rt
from llm_router.resolver import eligibility_store as store

LOCAL = "ollama/fake-a:1b"
OTHER_LOCAL = "ollama/fake-b:1b"
BARE = "fake-a:1b"
CLOUD = "gemini/fake-cloud"
JOB = "commit_message"
NOW = float(calendar.timegm((2026, 10, 20, 1, 0, 0)))
DIFF = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n-a\n+b\n"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router_home"))
    for name in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "LLM_ROUTER_IGNORE_ELIGIBILITY"):
        monkeypatch.delenv(name, raising=False)
    er.reset_cache()
    yield tmp_path / "router_home"
    er.reset_cache()


def write_row(filename):
    if filename == L.LEDGER_FILENAME:
        L.record_text_job_outcome(job=JOB, model=LOCAL, status=L.STATUS_ACCEPTED, attempts=1)
    else:
        L.record_local_attempts(
            job=JOB, served_model=LOCAL, chain_attempts=[LOCAL], accepted=True,
        )


@pytest.mark.parametrize("filename", [L.LEDGER_FILENAME, L.LOCAL_ATTEMPTS_FILENAME])
def test_ledger_cap_and_private_generations(home, monkeypatch, filename):
    monkeypatch.setattr(L.time, "time", lambda: NOW)
    monkeypatch.setattr(L, "_resolve_session_id", lambda: None)
    monkeypatch.setattr(L, "MAX_LEDGER_BYTES", 1024)
    path = paths.state_path(filename)
    backup = path.with_name(path.name + ".1")
    write_row(filename)
    row_bytes = path.stat().st_size
    for _ in range(100):
        write_row(filename)
        if path.exists():
            assert path.stat().st_size <= L.MAX_LEDGER_BYTES + row_bytes
    # Rotation moves the live file; ensure the next generation has an append.
    if not path.exists():
        write_row(filename)
    assert backup.exists()
    for generation in (path, backup):
        assert generation.stat().st_mode & 0o777 == 0o600
        assert generation.stat().st_size <= L.MAX_LEDGER_BYTES + row_bytes
    rows = capped_log.read_dicts(path)
    assert 1 < len(rows) < 101
    assert all(row["model"] == LOCAL and row["job"] == JOB for row in rows)


def test_recent_window_survives_rotation(home, monkeypatch):
    monkeypatch.setattr(L.time, "time", lambda: NOW)
    row = {"ts": NOW, "job": JOB, "model": LOCAL, "outcome": "ok"}
    row_bytes = len((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
    monkeypatch.setattr(L, "MAX_LEDGER_BYTES", 18 * row_bytes)
    for _ in range(19):
        L._append_local_attempt(JOB, LOCAL, "ok")
    path = paths.state_path(L.LOCAL_ATTEMPTS_FILENAME)
    assert path.with_name(path.name + ".1").exists()
    assert lr._recent_outcomes(LOCAL, JOB, None, path) == ["ok"] * 19
    L._append_local_attempt(JOB, LOCAL, "error")
    assert lr._recent_outcomes(LOCAL, JOB, None, path) == ["ok"] * 19 + ["error"]


def test_recent_tails_skip_partial_first_line(home, monkeypatch):
    path = paths.state_path(L.LOCAL_ATTEMPTS_FILENAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = json.dumps({"ts": NOW, "job": JOB, "model": LOCAL, "outcome": "ok"}) + "\n"
    path.write_text("broken prefix\n" + row, encoding="utf-8")
    monkeypatch.setattr(lr, "_LEDGER_TAIL_BYTES", len(row.encode("utf-8")) + 3)
    assert lr._recent_outcomes(LOCAL, JOB, None, path) == ["ok"]


@pytest.mark.parametrize("broken_cleanup", [False, True])
def test_failed_route_records_errors_and_demotes(home, monkeypatch, broken_cleanup):
    from llm_router.tools import text as T

    error = RuntimeError("All models failed")
    error.chain_attempts = [LOCAL, OTHER_LOCAL, CLOUD]

    async def fail(*args, **kwargs):
        raise error

    calls = []

    def demote(model, job):
        calls.append((model, job))
        if broken_cleanup:
            raise OSError("monitor unavailable")

    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "1")
    monkeypatch.setattr(T, "route_and_call", fail)
    monkeypatch.setattr(T, "_announce_routing", AsyncMock())
    monkeypatch.setattr(T, "_read_hook_route_directive", lambda: None)
    monkeypatch.setattr(lr, "demotion_check", demote)
    if broken_cleanup:
        def broken_reset(token):
            raise OSError("context cleanup unavailable")
        monkeypatch.setattr(er, "reset_text_job_context", broken_reset)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(T.llm_text_job(JOB, DIFF, object()))
    assert caught.value is error
    rows = capped_log.read_dicts(paths.state_path(L.LOCAL_ATTEMPTS_FILENAME))
    assert [(r["model"], r["outcome"]) for r in rows] == [
        (LOCAL, "error"), (OTHER_LOCAL, "error"),
    ]
    assert calls == [(LOCAL, JOB), (OTHER_LOCAL, JOB)]
    assert not paths.state_path(L.LEDGER_FILENAME).exists()


def test_oversized_input_reports_not_preferred(home):
    ctx = 8192
    limit = rt.max_prompt_chars(ctx)
    digest = "sha256:" + "a" * 16
    provenance = {
        "bank_version": "v1", "bank_sha256": "b" * 64,
        "model_digest": digest, "runtime": "ollama", "run_id": "r1",
        "date": "2026-10-20", "scope": {"max_prompt_chars": limit},
        "ctx": ctx, "think": False,
        "settings_hash": rt.settings_hash(
            temperature=rt.TEMPERATURE, ctx=ctx, max_tokens=rt.MAX_TOKENS, think=False,
        ),
    }
    store.record_local_check(
        BARE, {JOB: {"k": 60, "n": 70, "verdict": "PASS"}}, provenance,
        path=home / "local_eligibility.json",
    )
    lr.enable(BARE, JOB, "local_check")
    skip_causes.begin()
    chain = [CLOUD, LOCAL]
    assert er._prefer_passed(
        chain, JOB, limit + 1, NOW, {BARE: digest}, lambda model: ctx, False, None, None,
    ) == chain
    entries = skip_causes.entries_so_far()
    assert ("not_preferred", "input_over_limit") in [
        (entry["reason"], entry.get("detail")) for entry in entries
    ]
    entry = next(e for e in entries if e.get("detail") == "input_over_limit")
    assert entry == {
        "model": LOCAL, "reason": "not_preferred", "lane": JOB,
        "detail": "input_over_limit", "limit": limit, "input_len": limit + 1,
    }
