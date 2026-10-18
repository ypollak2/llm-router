"""P3.4-B3 (D-218): the status-bar hook prints ledger figures only, with n and window.

Evidence: the P3.4 plan (section 1, row 2) found the hook owning a UTC day, a Monday-UTC week, a
UTC month, a private Opus baseline that priced to $0 in a subprocess (rv-597) and its own
free-provider list. The figures themselves are checked against hand oracles in
``test_p34a_six_surfaces``; this file pins the structure that keeps them from drifting back.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAIR = (ROOT / "hooks" / "status-bar.py", ROOT / "src" / "llm_router" / "hooks" / "status-bar.py")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
DELETED = ("_time_bucket_starts", "_savings_for_period", "_read_savings", "_read_session_calls",
           "_FREE_PROVIDERS", "HOST_INPUT_PER_M", "HOST_OUTPUT_PER_M", "HOST_PRICE_KNOWN",
           "HOST_BASELINE_TIER", "HOST_BASELINE_LABEL", "_CACHE_PROVIDER", "_efficiency", "_net")


def _load(name: str = "sb_b3"):
    spec = importlib.util.spec_from_file_location(name, PAIR[1])
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


@pytest.fixture()
def hook(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(state))
    mod = _load()
    monkeypatch.setattr(mod, "STATUS_MODE", "full")
    monkeypatch.setattr(mod, "_read_claude_credits", lambda: (None, None, None, True))
    monkeypatch.setattr(mod, "_read_gemini_credits", lambda: (None, None, None))
    monkeypatch.setattr(mod, "_read_provider_health", lambda: None)
    return mod


def test_the_pair_is_byte_identical_and_the_version_was_bumped():
    texts = [p.read_text(encoding="utf-8") for p in PAIR]
    assert texts[0] == texts[1], "hooks/status-bar.py and src/llm_router/hooks/status-bar.py differ"
    m = re.search(r"^# llm_router-hook-version:\s*(\d+)", texts[0], re.M)
    assert m and int(m.group(1)) >= 8, "B3 changes the printed line: the hook version must be 8 or more"


def test_the_hook_holds_no_private_ledger_sql_and_none_of_the_deleted_readers():
    for path in PAIR:
        text = path.read_text(encoding="utf-8")
        assert text.strip(), path
        tree = ast.parse(text)
        strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        assert not [s for s in strings if re.search(r"\bFROM\s+(usage|routing_decisions)\b", s, re.I)], path
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert "sqlite3" not in imported and "datetime" not in imported, (path, imported)
        defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        assigned = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Name)}
        assert not (set(DELETED) & (defined | assigned)), (path, set(DELETED) & (defined | assigned))
        assert "ledger_view" in text and "window_summary" in text


def test_the_lint_would_catch_a_private_read_in_the_hook():
    """Anti-vacuity: the structural check above finds what it is looking for."""
    bad = "def f():\n    return 'SELECT 1 FROM usage WHERE 1'\n"
    tree = ast.parse(bad)
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert [s for s in strings if re.search(r"\bFROM\s+(usage|routing_decisions)\b", s, re.I)]


def _spy(monkeypatch):
    from llm_router import ledger_view as lv

    seen: list[str] = []
    real = lv.window_summary

    def spy(since="24h", *a, **k):
        seen.append(since)
        return real(since, *a, **k)

    monkeypatch.setattr(lv, "window_summary", spy)
    return seen


def test_the_refresher_asks_the_ledger_for_today_7d_month_and_the_session(hook, monkeypatch, tmp_path):
    (tmp_path / "state" / "proxy_calls.jsonl").write_text("")
    seen = _spy(monkeypatch)
    hook._format_status()
    assert sorted(seen) == ["7d", "month", "session", "today"], seen
    seen.clear()
    monkeypatch.setattr(hook, "STATUS_MODE", "compact")
    hook._format_status()
    assert sorted(seen) == ["7d", "today"], seen            # compact prints D and W only


def test_a_home_with_no_ledger_prints_na_never_zero(hook):
    for mode in ("compact", "full"):
        hook.STATUS_MODE = mode
        line = ANSI.sub("", hook._format_status())
        assert "D:n/a" in line and "W:n/a" in line, line
        assert "$0" not in line and "sub:" not in line, line
    assert "M:n/a" in line                                  # full mode, last iteration


def test_an_empty_window_with_a_ledger_is_a_counted_zero_with_its_n(hook, tmp_path):
    (tmp_path / "state" / "proxy_calls.jsonl").write_text("")
    # a proxy ledger exists but there is no usage.db: money is unknown (n/a), not "$0.00"
    line = ANSI.sub("", hook._format_status())
    assert "D:n/a" in line and "$0.00" not in line, line


_STANDALONE = """
import importlib.util, sqlite3, sys
sys.modules["llm_router"] = None            # the package is not importable on this host
def boom(*a, **k):
    raise AssertionError("a standalone copy scanned sqlite")
sqlite3.connect = boom
spec = importlib.util.spec_from_file_location("sb", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m._read_claude_credits = lambda: (None, None, None, True)
print(m._format_status())
"""


def test_a_copy_without_llm_router_prints_na_and_scans_nothing(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "usage.db").write_bytes(b"")                   # a file is there; the hook must not open it
    env = {"HOME": str(tmp_path), "LLM_ROUTER_HOME": str(state), "NO_COLOR": "1",
           "PATH": os.environ.get("PATH", ""), "LLM_ROUTER_STATUS_MODE": "full"}
    r = subprocess.run([sys.executable, "-c", _STANDALONE, str(PAIR[0])], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    line = ANSI.sub("", r.stdout)
    assert "\U0001f4b0 n/a" in line, line
    assert "D:" not in line and "$" not in line and "sub:" not in line, line


def test_the_sync_path_still_reads_only_the_cache(hook, monkeypatch, tmp_path, capsys):
    """Unchanged contract: main() prints the cached line and never calls the ledger."""
    line = "\U0001f4ca cached"
    hook._write_cache(line)
    monkeypatch.setattr(hook, "_ledger_window", lambda *a, **k: pytest.fail("ledger on the sync path"))
    monkeypatch.setattr(hook, "_lv", lambda: pytest.fail("ledger import on the sync path"))
    monkeypatch.setattr(hook, "_spawn_refresh", lambda: None)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("{}"))
    hook.main()
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["systemMessage"] == line


def _break_ledger_import(monkeypatch, exc: BaseException):
    """Make ``from llm_router import ledger_view`` raise ``exc`` at import time (F92)."""
    import importlib.abc

    class _Boom(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname == "llm_router.ledger_view":
                raise exc
            return None

    import llm_router

    monkeypatch.delitem(sys.modules, "llm_router.ledger_view", raising=False)
    monkeypatch.delattr(llm_router, "ledger_view", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_Boom(), *sys.meta_path])


def test_a_non_import_error_in_the_ledger_import_degrades_to_na_and_is_counted(hook, monkeypatch, tmp_path):
    """F92: ``_lv`` caught only ImportError, so a RuntimeError inside the package left the refresher
    raising and the bar showing the last good line. Now it is counted (class only) and the line
    is n/a."""
    from llm_router import failopen

    failopen.clear()
    _break_ledger_import(monkeypatch, RuntimeError("secret-detail-text"))
    assert hook._lv() is None
    failopen.reset_cache()
    assert failopen.snapshot().by_code == {"CHZ-FO-STATUS-BAR-LEDGER-IMPORT": 1}
    stored = failopen.store_path().read_text(encoding="utf-8")
    assert "RuntimeError" in stored and "secret-detail-text" not in stored      # class only
    for mode in ("compact", "full"):
        hook.STATUS_MODE = mode
        line = ANSI.sub("", hook._format_status())
        assert "\U0001f4b0 n/a" in line or "D:n/a" in line, line
        assert "$0" not in line, line


def test_the_refresher_replaces_a_stale_line_when_the_ledger_import_breaks(hook, monkeypatch, tmp_path):
    stale = "\U0001f4ca STALE LINE D:$9.99·today n=3"
    hook._write_cache(stale)
    _break_ledger_import(monkeypatch, RuntimeError("boom"))
    hook._refresh_cache()
    line, _age = hook._read_cache()
    assert line is not None and "STALE LINE" not in line, line
    assert "n/a" in ANSI.sub("", line) and "$9.99" not in line, line


def test_an_import_error_is_still_the_silent_standalone_case(hook, monkeypatch):
    """The standalone copy (ImportError) is not a failure: nothing is counted."""
    from llm_router import failopen

    failopen.clear()
    _break_ledger_import(monkeypatch, ImportError("no package"))
    assert hook._lv() is None
    failopen.reset_cache()
    assert failopen.snapshot().by_code == {}
