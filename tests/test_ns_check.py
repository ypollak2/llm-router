"""ns-check (North Star PR D): ledger-only reader of the A1 per-turn record."""

from __future__ import annotations

import json

import pytest

from llm_router import cli
from llm_router.commands import ns_check
from llm_router.proxy.cls_decide import TURN_FIELD_DEFAULTS

SID = "11111111-2222-3333-4444-555555555555"


def _row(session=SID, **kw):
    row = {"session_id": session, **TURN_FIELD_DEFAULTS}
    row.update(kw)
    return row


def _sup(idx, source, reason, decided, did, tier=None, sha="s"):
    return _row(supported=True, unsupported_reason=None, cls_turn_idx=idx, cls_turn_sha=f"{sha}{idx}",
                cls_decision_id=did, cls_turn_source=source, cls_turn_reason=reason,
                cls_turn_tier=tier, cls_turn_decided=decided)


def _script():
    """4 human turns: allow, veto, failopen timeout, started_elsewhere; plus a side call,
    a continuation (inherit) and an override row."""
    return [
        _sup(1, "llm", "allow", True, "d1", "haiku"),
        _sup(1, "inherit", "allow", True, "d1", "haiku"),
        _sup(2, "llm", "veto_tier", True, "d2", "opus"),
        _sup(3, "timeout", "failopen:timeout", False, "d3"),
        _sup(4, "none", "started_elsewhere", False, "d4"),
        _sup(5, "none", "prefix_claude", False, "d5"),
        _row(supported=False, unsupported_reason="no_tools", cls_turn_source="none",
             cls_turn_reason="unsupported", cls_turn_decided=False),
        _sup(1, "llm", "allow", True, "dX", "haiku", sha="other"),
        {"session_id": "someone-else", **TURN_FIELD_DEFAULTS, "supported": True},
    ]


def _write(tmp_path, rows, tail=""):
    p = tmp_path / "proxy_calls.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows) + tail)
    return p


def _run(capsys, *args):
    code = ns_check.cmd_ns_check(list(args))
    return code, capsys.readouterr()


def test_scripted_session_exits_zero_and_reports_every_part(tmp_path, capsys):
    p = _write(tmp_path, _script())
    code, out = _run(capsys, "--session", SID, "--ledger", str(p))
    assert code == 0, out.err
    o = out.out
    assert f"snapshot offset: {p.stat().st_size} bytes" in o
    # non-override supported rows: d1 llm, d1 inherit, d2, d3, d4, dX = 6; decided: d1, d1 inherit, d2, dX = 4
    assert "decided % of supported non-override rows: 66.7% (4/6)" in o
    assert "failopen turns go local, not decided" in o
    assert "turns (distinct cls_turn_idx+sha, supported rows): 6" in o
    assert "failopen:timeout" in o and "no_tools" in o
    assert "started_elsewhere" in o and "prefix_claude" in o
    for src in ("llm", "inherit", "timeout", "none"):
        assert src in o
    assert "cls_turn_sha: recorded on" in o
    assert "someone-else" not in o


def test_verdict_distribution_flags_a_constant_classifier(tmp_path, capsys):
    rows = [_sup(i, "llm", "allow", True, f"d{i}", "haiku") for i in range(1, 5)]
    p = _write(tmp_path, rows)
    code, out = _run(capsys, "--session", SID, "--ledger", str(p))
    assert code == 0
    assert "n=4" in out.out and "every decided turn has the same verdict (allow, n=4)" in out.out


def test_a_deciding_classifier_is_not_flagged(tmp_path, capsys):
    p = _write(tmp_path, _script())
    _, out = _run(capsys, "--session", SID, "--ledger", str(p))
    assert "same verdict" not in out.out
    assert "veto_tier" in out.out and "opus" in out.out


def test_too_few_decided_turns_is_said_not_judged(tmp_path, capsys):
    p = _write(tmp_path, [_sup(1, "llm", "allow", True, "d1", "haiku")])
    _, out = _run(capsys, "--session", SID, "--ledger", str(p))
    assert "too few to tell" in out.out


def test_unknown_session_exits_nonzero(tmp_path, capsys):
    p = _write(tmp_path, _script())
    code, out = _run(capsys, "--session", "nope", "--ledger", str(p))
    assert code == 2 and "no ledger rows for session nope" in out.err and out.out == ""


def test_unreadable_ledger_exits_nonzero(tmp_path, capsys):
    code, out = _run(capsys, "--session", SID, "--ledger", str(tmp_path / "missing.jsonl"))
    assert code == 2 and "cannot read the ledger" in out.err


def test_pre_a1_rows_are_not_a_pass(tmp_path, capsys):
    p = _write(tmp_path, [{"session_id": SID, "decision": "served"}])
    code, out = _run(capsys, "--session", SID, "--ledger", str(p))
    assert code == 3 and "not a pass" in out.out and "n/a" not in out.out.lower().replace("not a pass", "")


def test_only_override_rows_is_undefined_not_zero_percent(tmp_path, capsys):
    p = _write(tmp_path, [_sup(1, "none", "prefix_local", False, "d1")])
    code, out = _run(capsys, "--session", SID, "--ledger", str(p))
    assert code == 3 and "undefined" in out.out and "0.0%" not in out.out


def test_torn_tail_and_garbage_lines_are_counted_not_fatal(tmp_path, capsys):
    p = _write(tmp_path, _script(), tail='{"session_id": "x", "supp')
    code, out = _run(capsys, "--session", SID, "--ledger", str(p))
    assert code == 0 and "unparseable lines skipped: 1" in out.out


def test_snapshot_ignores_rows_appended_after_the_offset(tmp_path):
    p = _write(tmp_path, _script())
    size = p.stat().st_size
    offset, rows, _ = ns_check._read_snapshot(p)
    assert offset == size
    with p.open("a") as fh:
        fh.write(json.dumps(_sup(9, "llm", "allow", True, "late", "haiku")) + "\n")
    assert len(rows) == len(_script())


def test_reader_keys_are_the_a1_keys_and_cls_turn_sha_is_one():
    assert set(ns_check.A1_KEYS) == set(TURN_FIELD_DEFAULTS)
    assert "cls_turn_sha" in TURN_FIELD_DEFAULTS


def test_says_so_when_cls_turn_sha_is_not_an_a1_key(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ns_check, "_a1_key_names", lambda: tuple(k for k in TURN_FIELD_DEFAULTS if k != "cls_turn_sha"))
    p = _write(tmp_path, _script())
    _, out = _run(capsys, "--session", SID, "--ledger", str(p))
    assert "cls_turn_sha: not an A1 key in this build" in out.out


def test_session_is_required():
    with pytest.raises(SystemExit) as e:
        ns_check.cmd_ns_check([])
    assert e.value.code == 2


def test_wired_into_the_cli():
    assert "ns-check" in cli._OWN_HELP_ANYWHERE and "ns-check" in cli._KNOWN_SUBCOMMANDS
    assert "llm-router ns-check" in cli.__doc__


def test_python_dash_m_is_never_a_silent_zero(tmp_path):
    """`python -m llm_router.commands.ns_check` once printed nothing and exited 0 (no __main__)."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    src = str(Path(ns_check.__file__).resolve().parents[2])
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "PYTHONPATH": src, "LLM_ROUTER_HOME": str(home), "HOME": str(home)}
    proc = subprocess.run([sys.executable, "-m", "llm_router.commands.ns_check", "--session", SID],
                          env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert (proc.stdout + proc.stderr).strip()
