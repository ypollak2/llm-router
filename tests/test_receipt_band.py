"""The receipt band's host side: receipts from the proxy ledger, signals, feed, and
the opt-in install / uninstall of the Claude Code mod (on a temp HOME only)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
import subprocess
import sys
from pathlib import Path

import pytest

from llm_router import receipt_band as rb, user_signal
from llm_router.proxy import ledger

REPO = Path(__file__).resolve().parents[1]
MOD = REPO / "src" / "llm_router" / "mods" / "llm-router-receipt"
SESSION = "sess-1"


def _rows(rows: list[dict]) -> None:
    p = ledger.ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _fwd(ts, sid=SESSION, model="claude-sonnet-5"):
    return {"ts": ts, "session_id": sid, "decision": "forwarded", "requested_model": model, "msg_id": f"f{ts}",
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 50000,
                      "cache_creation_input_tokens": 0}}


def _served(ts, sid=SESSION, msg="msg_lr1", model="ollama/qwen3-coder:30b"):
    return {"ts": ts, "session_id": sid, "decision": "served", "model": model, "msg_id": msg,
            "requested_model": "claude-sonnet-5", "task_type": "code", "backend_usage": {"output_tokens": 200}}


# ── receipt ──────────────────────────────────────────────────────────────────

def test_a_turn_served_off_claude_is_a_receipt():
    now = time.time() - 60
    _rows([_fwd(now - 100), _served(now + 1), _served(now + 2, msg="msg_lr2")])
    r = rb.last_routed_turn(now, SESSION, until=now + 10)
    assert r["routed"] is True and r["key"] == "msg_lr2"
    assert r["model"] == "ollama/qwen3-coder:30b"
    assert r["cost_usd"] == 0.0                       # served locally: Anthropic cost is zero
    # The ledger's own net-avoided estimate (one deferred call: 50k cached tokens
    # read + 200 output tokens on the requested model), not a made-up figure.
    assert r["saved_usd"] == pytest.approx(0.012, abs=1e-4)


def test_a_turn_not_served_off_claude_is_not_a_receipt():
    now = time.time() - 60
    _rows([_fwd(now - 100), _fwd(now + 1), _fwd(now + 2)])
    assert rb.last_routed_turn(now, SESSION, until=now + 10) == {"routed": False}


def test_other_sessions_and_earlier_turns_are_not_this_turns_receipt():
    now = time.time() - 60
    _rows([_served(now - 50, msg="earlier"), _served(now + 1, sid="someone-else")])
    assert rb.last_routed_turn(now, SESSION, until=now + 10) == {"routed": False}


def test_empty_ledger_is_not_routed():
    assert rb.last_routed_turn(0.0, SESSION) == {"routed": False}


def test_feed_has_time_model_why_outcome_and_no_prompt_text():
    now = time.time() - 60
    _rows([_fwd(now), _served(now + 1)])
    user_signal.record("msg_lr1", "redone", "terminal")
    rows = rb.feed(SESSION, 10)
    assert [set(r) for r in rows] == [{"ts", "model", "why", "outcome"}] * 2
    assert rows[0]["outcome"] == "served, redone" and rows[0]["why"] == "code"
    assert len(rb.feed(SESSION, 1)) == 1


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_signal_records_one_row_and_receipt_prints_json(capsys):
    assert rb.cmd_mod(["signal", "--key", "msg_lr1", "--signal", "kept", "--surface", "terminal"]) == 0
    rows = user_signal.read_rows()
    assert [(r["key"], r["signal"], r["surface"]) for r in rows] == [("msg_lr1", "kept", "terminal")]
    capsys.readouterr()
    assert rb.cmd_mod(["receipt", "--since", "0"]) == 0
    assert json.loads(capsys.readouterr().out) == {"routed": False}


def test_cli_signal_refuses_free_text(capsys):
    assert rb.cmd_mod(["signal", "--key", "write me a poem", "--signal", "kept", "--surface", "terminal"]) == 1
    assert not user_signal.ledger_path().exists()


def test_cli_dispatch_exists():
    out = subprocess.run([sys.executable, "-m", "llm_router.cli", "mod", "--help"],
                         capture_output=True, text=True, env={**os.environ}, timeout=60)
    assert out.returncode == 0 and "install" in out.stdout


# ── install / uninstall ──────────────────────────────────────────────────────

@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    """A temp Claude config dir: the real ~/.claude is never reachable from here."""
    home = tmp_path / "claude-home"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_DIR", str(home))
    assert rb._settings_path() == home / "settings.json"
    assert str(Path.home() / ".claude") not in str(rb._settings_path())
    return home


def _digest(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


ORIGINAL = '{\n    "model": "opus",   "env": {"FOO": "1"},\n  "hooks": {}\n}\n'


@pytest.mark.parametrize("original", [ORIGINAL, None, "{}\n", '{"env": {"CLAUDE_CODE_PLUGIN_DIRS": "/x/other"}}\n'])
def test_install_then_uninstall_is_byte_identical_five_times(claude_home, original):
    sp = claude_home / "settings.json"
    if original is not None:
        sp.write_text(original)
    before = _digest(sp)
    for _ in range(5):
        rb.install()
        data = json.loads(sp.read_text())
        assert str(rb.mod_dest()) in data["env"]["CLAUDE_CODE_PLUGIN_DIRS"].split(os.pathsep)
        assert (rb.mod_dest() / ".claude-plugin" / "plugin.json").exists()
        rb.uninstall()
        assert _digest(sp) == before, "settings must be byte-identical after install + uninstall"
        assert not rb.mod_dest().exists()


def test_install_is_idempotent_and_backs_up(claude_home):
    sp = claude_home / "settings.json"
    sp.write_text(ORIGINAL)
    rb.install()
    after_first = sp.read_bytes()
    backup = sp.with_name(sp.name + rb._SETTINGS_BACKUP_SUFFIX)
    assert backup.read_bytes() == ORIGINAL.encode()
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600
    again = rb.install()
    assert sp.read_bytes() == after_first
    assert any("already names" in a for a in again)
    assert backup.read_bytes() == ORIGINAL.encode(), "the backup is the PRE-install file, never overwritten"


def test_install_keeps_other_plugin_dirs_and_uninstall_restores_them(claude_home):
    sp = claude_home / "settings.json"
    sp.write_text('{"env": {"CLAUDE_CODE_PLUGIN_DIRS": "/x/other"}}\n')
    rb.install()
    dirs = json.loads(sp.read_text())["env"]["CLAUDE_CODE_PLUGIN_DIRS"].split(os.pathsep)
    assert dirs == ["/x/other", str(rb.mod_dest())]
    rb.uninstall()
    assert json.loads(sp.read_text())["env"]["CLAUDE_CODE_PLUGIN_DIRS"] == "/x/other"


def test_uninstall_keeps_changes_made_after_install(claude_home):
    sp = claude_home / "settings.json"
    sp.write_text(ORIGINAL)
    rb.install()
    data = json.loads(sp.read_text())
    data["theme"] = "dark"
    sp.write_text(json.dumps(data))
    rb.uninstall()
    after = json.loads(sp.read_text())
    assert after["theme"] == "dark" and "CLAUDE_CODE_PLUGIN_DIRS" not in after["env"]
    assert after["env"]["FOO"] == "1"
    assert not sp.with_name(sp.name + rb._SETTINGS_BACKUP_SUFFIX).exists()


def test_uninstall_without_install_changes_nothing(claude_home):
    sp = claude_home / "settings.json"
    sp.write_text(ORIGINAL)
    assert any("unchanged" in a for a in rb.uninstall())
    assert sp.read_text() == ORIGINAL


def test_install_refuses_a_settings_file_that_is_not_an_object(claude_home):
    sp = claude_home / "settings.json"
    sp.write_text("[1, 2]\n")
    with pytest.raises(ValueError):
        rb.install()
    assert sp.read_text() == "[1, 2]\n"


def test_install_never_reaches_the_real_home(claude_home, monkeypatch):
    real = Path(os.path.expanduser("~")) / ".claude" / "settings.json"
    before = _digest(real)
    rb.install()
    rb.uninstall()
    assert _digest(real) == before


# ── the shipped mod ──────────────────────────────────────────────────────────

def test_mod_files_are_present_and_wired():
    manifest = json.loads((MOD / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "llm-router-receipt" == MOD.name
    assert json.loads((MOD / "hooks" / "hooks.json").read_text()) == {"modules": ["./register.tsx"]}
    src = (MOD / "hooks" / "register.tsx").read_text()
    for needle in ("AbovePrompt", "hotkey=\"k\"", "hotkey=\"r\"", "prompt.submit", "turn.complete", "command.run"):
        assert needle in src, needle


def test_mod_copy_excludes_tests(claude_home):
    rb.install()
    assert not list(rb.mod_dest().rglob("*.test.ts"))
    rb.uninstall()


def test_mod_pure_logic_and_engine_tests_run():
    """The mod's pure logic under plain node, and its engine tests under
    ``claude plugin test`` where that is installed. In CI (``CI`` set) a missing
    node is a failure, not a skip: an empty set passes everything."""
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI to run the receipt mod's logic tests")
        pytest.skip("node not installed")
    r = subprocess.run([node, "--test", str(REPO / "tests" / "mods" / "receipt_logic.spec.mjs")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pass 13" in r.stdout, r.stdout  # 13 tests ran: not an empty pass


def test_settings_mode_and_symlink_are_kept(claude_home, tmp_path):
    real = tmp_path / "real-settings.json"
    real.write_text('{"env": {"TOKEN": "s3cret", "NAME": "Yali ü"}}\n')
    os.chmod(real, 0o600)
    sp = claude_home / "settings.json"
    sp.symlink_to(real)
    rb.install()
    assert sp.is_symlink(), "a symlinked settings file stays a symlink"
    assert stat.S_IMODE(os.stat(real).st_mode) == 0o600
    assert "Yali ü" in real.read_text(encoding="utf-8")
    rb.uninstall()
    assert sp.is_symlink() and stat.S_IMODE(os.stat(real).st_mode) == 0o600


def test_malformed_settings_is_refused_before_anything_is_copied(claude_home, capsys):
    sp = claude_home / "settings.json"
    sp.write_text("{not json")
    assert rb.cmd_mod(["install"]) == 1
    assert "not valid JSON" in capsys.readouterr().err
    assert not rb.mod_dest().exists() and sp.read_text() == "{not json"


def test_uninstall_without_record_never_leaves_an_empty_variable(claude_home):
    sp = claude_home / "settings.json"
    sp.write_text(json.dumps({"env": {"CLAUDE_CODE_PLUGIN_DIRS": str(rb.mod_dest())}}))
    rb.uninstall()
    assert "CLAUDE_CODE_PLUGIN_DIRS" not in json.loads(sp.read_text())["env"]
