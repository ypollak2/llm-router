"""P2.2-A: the routing authority state (authority.py) and `llm-router mode`.

Every test runs on a scratch LLM_ROUTER_HOME; settings.json is a tmp file whose
bytes must not change, and nothing is written outside the scratch state dir.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from llm_router import authority
from llm_router.commands.mode import cmd_mode


@pytest.fixture(autouse=True)
def _scratch(monkeypatch, tmp_path):
    home = tmp_path / "state"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.delenv("LLM_ROUTER_AUTHORITY", raising=False)
    monkeypatch.setattr(authority, "_file_cache", None)
    monkeypatch.setattr(authority, "_reported", set())
    return home


def _failopen_codes(home) -> list[str]:
    path = home / "fail_open.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["c"] for line in path.read_text().splitlines() if line.strip()]


def test_default_is_route_with_no_file_and_no_env():
    assert authority.mode() == "route"
    assert authority.describe() == ("route", "default")


def test_file_beats_default_and_env_beats_file(monkeypatch):
    authority.set_mode("advise")
    assert authority.describe() == ("advise", "file")
    monkeypatch.setenv("LLM_ROUTER_AUTHORITY", "route")
    assert authority.describe() == ("route", "env")
    monkeypatch.setenv("LLM_ROUTER_AUTHORITY", "ADVISE")
    assert authority.describe() == ("advise", "env")


def test_empty_env_counts_as_unset():
    authority.set_mode("advise")
    os.environ["LLM_ROUTER_AUTHORITY"] = "  "
    assert authority.describe() == ("advise", "file")


def test_mode_file_is_reread_after_it_changes_with_no_restart(_scratch):
    authority.set_mode("advise")
    assert authority.mode() == "advise"
    # Another process rewrites the file: no set_mode() call here, so the cache must notice.
    (_scratch / "authority").write_text("route\n")
    os.utime(_scratch / "authority", ns=(1, 2_000_000_000))
    assert authority.mode() == "route"


@pytest.mark.parametrize("where", ["env", "file"])
def test_invalid_value_is_route_and_recorded_once(monkeypatch, _scratch, where):
    if where == "env":
        monkeypatch.setenv("LLM_ROUTER_AUTHORITY", "turbo")
    else:
        _scratch.mkdir(parents=True)
        (_scratch / "authority").write_text("turbo\n")
    assert authority.mode() == "route"
    assert authority.mode() == "route"
    assert _failopen_codes(_scratch) == ["CHZ-FO-AUTHORITY-MODE"]


def test_invalid_env_does_not_fall_through_to_an_advise_file(monkeypatch):
    authority.set_mode("advise")
    monkeypatch.setenv("LLM_ROUTER_AUTHORITY", "turbo")
    assert authority.mode() == "route"


def test_set_mode_is_atomic_private_and_creates_the_state_dir(_scratch):
    assert not _scratch.exists()
    assert authority.set_mode(" Advise ") == "advise"
    path = _scratch / "authority"
    assert path.read_text() == "advise\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in _scratch.iterdir()] == ["authority"], "no temp file may be left behind"


def test_set_mode_rejects_anything_else_and_writes_nothing(_scratch):
    for bad in ("", "on", "route advise", None):
        with pytest.raises(ValueError):
            authority.set_mode(bad)
    assert not (_scratch / "authority").exists()


def test_unreadable_state_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(authority, "describe", boom)
    assert authority.mode() == "route"


# ── the command ──────────────────────────────────────────────────────────────

def test_cli_no_argument_prints_mode_and_source(capsys):
    assert cmd_mode([]) == 0
    assert capsys.readouterr().out.strip() == "mode: route (default)"
    assert cmd_mode(["advise"]) == 0
    capsys.readouterr()
    assert cmd_mode([]) == 0
    assert capsys.readouterr().out.strip() == "mode: advise (file)"


def test_cli_route_and_advise_write_the_file(capsys, _scratch):
    assert cmd_mode(["advise"]) == 0
    assert "forwards the model you asked for" in capsys.readouterr().out
    assert (_scratch / "authority").read_text() == "advise\n"
    assert cmd_mode(["route"]) == 0
    assert "(the default)" in capsys.readouterr().out
    assert (_scratch / "authority").read_text() == "route\n"


def test_cli_unknown_value_exits_2_and_writes_nothing(capsys, _scratch):
    assert cmd_mode(["turbo"]) == 2
    assert cmd_mode(["route", "advise"]) == 2
    assert "usage: llm-router mode" in capsys.readouterr().err
    assert not (_scratch / "authority").exists()


def test_cli_says_when_the_environment_overrides_what_it_wrote(monkeypatch, capsys):
    monkeypatch.setenv("LLM_ROUTER_AUTHORITY", "route")
    assert cmd_mode(["advise"]) == 0
    assert "wins over the file" in capsys.readouterr().out


def test_mode_never_touches_settings_json_or_anything_outside_the_state_dir(tmp_path, _scratch, monkeypatch):
    claude = tmp_path / "claude"
    claude.mkdir()
    settings = claude / "settings.json"
    settings.write_bytes(b'{"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"}}\n')
    before = settings.read_bytes()
    import llm_router.install_hooks as ih

    monkeypatch.setattr(ih, "_SETTINGS_PATH", settings)
    cmd_mode(["advise"])
    cmd_mode(["route"])
    assert settings.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["claude", "state"]


def test_main_dispatches_the_mode_command(monkeypatch, capsys, _scratch):
    from llm_router import cli

    monkeypatch.setattr("sys.argv", ["llm-router", "mode", "advise"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert (_scratch / "authority").read_text() == "advise\n"
