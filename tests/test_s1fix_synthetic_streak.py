"""S1FIX: the scheduled daily run carries the synth-live manifest, and a manifest session's subagent_* rows
(no transcript) count as turns in release_clock. Every test runs under a tmp home with a fake runner."""
from __future__ import annotations

import json
import plistlib
import subprocess
from datetime import date, datetime, timezone

import pytest

from llm_router import daily_offload as do
from llm_router import daily_schedule as ds
from tests._o3_fixture import proxy_row

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc).timestamp()
DAY = date(2026, 10, 19)
ENV = "LLM_ROUTER_SYNTH_MANIFEST"
QUALITY = [{"ts": NOW - 25 * 86400.0, "door": "proxy", "served_class": "haiku", "task_group": "qa",
            "kind": "audit", "n": 30, "acceptable": 28, "source": "fixture"}]


def _kind_of(sid, stamp):
    return {"org": "organic", "syn": "research", "non": "research"}.get(str(sid)[:3])


def _rows(prefix: str, n_sessions: int = 3, per: int = 12) -> list[dict]:
    """Each session: 1 subagent_first + (per-1) subagent_turn rows, then continuations (the driver's shape)."""
    base = do.day_bounds(DAY)[0] + 3600
    out, i = [], 0
    for s in range(n_sessions):
        for k in range(per + 2 * per):   # per turns, each followed by two tool-result continuations
            i += 1
            step = ("subagent_first" if k == 0 else "subagent_turn") if k < per else "continuation"
            out.append(proxy_row(i, sid=f"{prefix}-{s}", ts=base + i, tier="haiku", msg_id=f"m{prefix}{i}",
                                 step=step, **{"tier_task_type": "query"}))
    return out


def _inputs(rows, thread_of=None):
    return do.Inputs(rows, [], [], [], QUALITY, _kind_of, thread_of=thread_of) if thread_of else \
        do.Inputs(rows, [], [], [], QUALITY, _kind_of)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(h / ".llm-router"))
    monkeypatch.delenv("LLM_ROUTER_SCHEDULED", raising=False)
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setattr(do, "_now", lambda: NOW)
    return h


def _manifest(tmp_path, ids):
    p = tmp_path / "manifest.jsonl"
    p.write_text("".join(json.dumps({"schema": "synth-live-manifest/1", "session_id": s}) + "\n" for s in ids))
    return p


def _row(rows, ids, thread_of=None):
    return do.compute_day(DAY, _inputs(rows, thread_of), now=NOW, manifest_ids=ids, code_rev="t")


def _run(home, tmp_path, manifest, rows):
    do.run_daily(date=str(DAY), backfill=0, force=False, partial=False, manifest=manifest,
                 inputs=_inputs(rows), now=NOW)
    return json.loads(do.day_file(DAY.isoformat(), do.daily_dir()).read_text())


# ---- (1) env var supplies the manifest; the flag wins
def test_env_supplies_manifest_when_flag_absent(home, tmp_path, monkeypatch):
    ids = {f"syn-{i}" for i in range(3)}
    monkeypatch.setenv(ENV, str(_manifest(tmp_path, ids)))
    row = _run(home, tmp_path, None, _rows("syn"))
    assert row["synthetic_manifest_ids"] == 3 and row["synthetic_manifest_source"] == "env"
    assert "(env)" in row["populations"]["release_clock"]["note"]


def test_flag_wins_over_env(home, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(_manifest(tmp_path, {"syn-0"})))
    flag = _manifest(tmp_path, {"syn-0", "syn-1"})
    monkeypatch.setenv(ENV, str(tmp_path / "env-manifest.jsonl"))
    (tmp_path / "env-manifest.jsonl").write_text(json.dumps({"schema": "synth-live-manifest/1", "session_id": "syn-0"}) + "\n")
    row = _run(home, tmp_path, str(flag), _rows("syn"))
    assert row["synthetic_manifest_ids"] == 2 and row["synthetic_manifest_source"] == "flag"


def test_unreadable_env_manifest_is_loud(home, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ENV, str(tmp_path / "nope.jsonl"))
    rc = do.run_daily(date=str(DAY), backfill=0, force=False, partial=False, manifest=None,
                      inputs=_inputs(_rows("syn")), now=NOW)
    assert rc == 2 and ENV in capsys.readouterr().err


# ---- (2) scheduler carries the variable only when set at install time
class _Runner:
    def __call__(self, cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _plist_env(home):
    p = home / "Library" / "LaunchAgents" / "com.llm_router.kpi-daily.plist"
    return plistlib.loads(p.read_bytes())["EnvironmentVariables"]


def test_installer_writes_env_when_set(home, tmp_path, monkeypatch):
    m = tmp_path / "m.jsonl"
    monkeypatch.setenv(ENV, str(m))
    out = ds.install(system="Darwin", home=home, python="/opt/py", runner=_Runner(), now=NOW)
    assert out["status"] == "installed" and _plist_env(home)[ENV] == str(m)


def test_installer_omits_env_when_unset(home):
    out = ds.install(system="Darwin", home=home, python="/opt/py", runner=_Runner(), now=NOW)
    assert out["status"] == "installed" and ENV not in _plist_env(home)
    assert ds.scheduled_env() == {"LLM_ROUTER_SCHEDULED": "1"}


def test_empty_manifest_is_refused(home, tmp_path, monkeypatch, capsys):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    for flag, env in ((str(empty), None), (None, str(empty))):
        if env:
            monkeypatch.setenv(ENV, env)
        rc = do.run_daily(date=str(DAY), backfill=0, force=False, partial=False, manifest=flag,
                          inputs=_inputs(_rows("syn")), now=NOW)
        assert rc == 2 and "0 session ids" in capsys.readouterr().err


# ---- (3) manifest session, subagent_* rows, no transcript -> turns in release_clock, organic untouched
def test_manifest_subagent_rows_are_release_clock_turns(home):
    rows = _rows("syn") + _rows("org")
    ids = {f"syn-{i}" for i in range(3)}
    with_m = _row(rows, ids)["populations"]
    without = _row(rows, set())["populations"]
    assert with_m["synthetic"]["turn"]["n"] == 3 * 12
    assert with_m["release_clock"]["turn"]["n"] == with_m["organic"]["turn"]["n"] + 36
    assert with_m["organic"]["turn"] == without["organic"]["turn"]
    assert _row(rows, ids)["synthetic_turn_rule"]


# ---- (4) negative control: same rows, session not in the manifest
def test_non_manifest_session_still_zero_turns(home):
    rows = _rows("non")
    p = _row(rows, {"syn-other"})["populations"]
    assert p["release_clock"]["turn"]["n"] == 0 and p["other"]["turn"]["n"] == 0
    assert p["release_clock"]["action"]["n"] == 0   # research not in manifest is "other"


# ---- (5) a transcript role wins over the manifest rule
def test_transcript_role_wins(home):
    rows = _rows("syn")
    p = _row(rows, {f"syn-{i}" for i in range(3)}, thread_of=lambda sid, mid: "subagent")["populations"]
    assert p["synthetic"]["turn"]["n"] == 0
