"""P0.14-c (PLAN v16 R8 A.1, owner decision D-R8-4 = explicit only):
``llm-router doctor --fix-routing``.

2026-10-08: ``~/.claude/settings.json`` lost ``env.ANTHROPIC_BASE_URL`` in an
unrecorded rewrite (16:55-17:17Z) while the proxy-default sentinel still said
enabled; the proxy ledger went silent until a hand restore at ~19:58Z.

The MUST: in a sandboxed HOME the repair writes in exactly the one state where
every condition holds and refuses in the other six (7/7 states); the written
state is a one-key diff with a backup file; a second run is a zero-diff no-op;
no hook and no plain install reaches the fix path; ``routing_opt_out``
suppresses the repair and the SessionStart nag; each SessionStart appends one
``observe`` row to ``settings_writes.jsonl``.

Sandbox: HOME, LLM_ROUTER_HOME and LLM_ROUTER_CLAUDE_DIR all point inside
tmp_path, and real loopback sockets on ephemeral ports stand in for the shim
and the main proxy (never 8787/8797).
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import socket
import sys
from pathlib import Path

import pytest

from llm_router import proxy_default as pd
from llm_router.commands import proxy_default as cmd
from llm_router.commands.doctor import cmd_doctor

ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = ROOT / "src" / "llm_router" / "hooks" / "session-start.py"


# ── sandbox ────────────────────────────────────────────────────────────────

@pytest.fixture()
def box(monkeypatch, tmp_path):
    home, proj = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / ".llm-router"))
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_DIR", str(home / ".claude"))
    for var in ("ANTHROPIC_BASE_URL", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(proj)
    (home / ".llm-router").mkdir()
    socks: list[socket.socket] = []
    yield _Box(home, proj, socks)
    for s in socks:
        s.close()


class _Box:
    def __init__(self, home: Path, proj: Path, socks: list):
        self.home, self.proj, self._socks = home, proj, socks
        self.settings = home / ".claude" / "settings.json"
        self.writes = home / ".llm-router" / "settings_writes.jsonl"

    def listening(self) -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(16)
        self._socks.append(s)
        return s.getsockname()[1]

    @staticmethod
    def dead() -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def sentinel(self, port: int, upstream: int | None, **extra) -> None:
        data = {"enabled": True, "port": port, "upstream_port": upstream,
                "shim_label": pd.SHIM_LABEL if upstream else None, "label": pd.LABEL, **extra}
        pd.sentinel_path().write_text(json.dumps(data))

    def write_settings(self, env: dict) -> bytes:
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps(
            {"model": "opus", "hooks": {"SessionStart": [{"hooks": []}]}, "env": env}, indent=2))
        return self.settings.read_bytes()

    def write_rows(self) -> list[dict]:
        if not self.writes.exists():
            return []
        return [json.loads(line) for line in self.writes.read_text().splitlines() if line.strip()]

    def backups(self) -> list[Path]:
        found = list(self.settings.parent.glob("settings.json*.bak"))
        found += list((self.home / ".llm-router" / "backups").glob("settings.json*.bak"))
        return sorted(found)


def _doctor(capsys, *args: str) -> tuple[int, str]:
    rc = cmd_doctor(["--fix-routing", *args])
    return rc, capsys.readouterr().out


def _load_hook():
    spec = importlib.util.spec_from_file_location("session_start_fix_routing", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


# ── the seven states (R8 A.1 task 6) ─────────────────────────────────────────

def test_state1_all_conditions_hold_writes_one_key_with_backup_and_a_write_row(box, capsys):
    shim, main = box.listening(), box.listening()
    box.sentinel(shim, main)
    before = box.write_settings({"FOO": "bar"})  # no ENABLE_TOOL_SEARCH: repair must not add it

    rc, out = _doctor(capsys, "--yes")

    assert rc == 0, out
    after = json.loads(box.settings.read_text())
    original = json.loads(before)
    assert after["env"] == {"FOO": "bar", "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{shim}"}
    assert {k: v for k, v in after.items() if k != "env"} == {k: v for k, v in original.items() if k != "env"}
    assert f'+  env.ANTHROPIC_BASE_URL: "http://127.0.0.1:{shim}"' in out
    [backup] = box.backups()
    assert f"backup: {backup}" in out and backup.read_bytes() == before
    [row] = box.write_rows()
    assert row["kind"] == "write" and row["keys_changed"] == ["env.ANTHROPIC_BASE_URL"]
    assert row["backup"] == str(backup) and row["env_sha256"] == cmd.env_block_sha256(after["env"])

    # a second run is a zero-diff no-op: same bytes, no new backup, no new row
    written = box.settings.read_bytes()
    rc2, out2 = _doctor(capsys, "--yes")
    assert rc2 == 0 and "fix-routing noop" in out2, out2
    assert box.settings.read_bytes() == written
    assert box.backups() == [backup] and len(box.write_rows()) == 1


def test_state2_key_present_and_live_is_a_noop(box, capsys):
    shim, main = box.listening(), box.listening()
    box.sentinel(shim, main)
    before = box.write_settings({"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{shim}"})
    rc, out = _doctor(capsys, "--yes")
    assert rc == 0 and "fix-routing noop" in out, out
    assert box.settings.read_bytes() == before and box.backups() == [] and box.write_rows() == []


def test_state3_key_present_but_its_port_is_dead_refuses_and_explains(box, capsys):
    shim, main = box.dead(), box.dead()
    box.sentinel(shim, main)
    before = box.write_settings({"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{shim}"})
    rc, out = _doctor(capsys, "--yes")
    assert rc == 1 and "fix-routing refused" in out and "nothing answers" in out, out
    assert box.settings.read_bytes() == before and box.backups() == []


def test_state4_key_pointing_at_another_host_is_never_overwritten(box, capsys):
    shim, main = box.listening(), box.listening()
    box.sentinel(shim, main)
    before = box.write_settings({"ANTHROPIC_BASE_URL": "https://u:pw@corp.example.com:8443/v1"})
    rc, out = _doctor(capsys, "--yes")
    assert rc == 1 and "corp.example.com:8443" in out and "not ours to change" in out, out
    assert "pw@" not in out and "/v1" not in out
    assert box.settings.read_bytes() == before and box.backups() == []


@pytest.mark.parametrize("name", ["settings.local.json", "settings.json"])
def test_state5_project_override_refuses_and_names_the_file(box, capsys, name):
    shim, main = box.listening(), box.listening()
    box.sentinel(shim, main)
    before = box.write_settings({"FOO": "bar"})
    override = box.proj / ".claude" / name
    override.parent.mkdir()
    override.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://proxy.other:9000"}}))
    rc, out = _doctor(capsys, "--yes")
    assert rc == 1 and "project override" in out and str(override) in out, out
    assert box.settings.read_bytes() == before and box.backups() == []


def test_state6_routing_opt_out_suppresses_the_repair_and_the_session_start_nag(box, capsys):
    shim, main = box.listening(), box.listening()
    box.sentinel(shim, main)
    before = box.write_settings({"FOO": "bar"})
    # control: the same fixture without the flag is nagged, and told about the repair
    nag = _load_hook()._check_proxy_default_health()
    assert "routing is OFF" in nag and "llm-router doctor --fix-routing --decline" in nag

    box.sentinel(shim, main, routing_opt_out=True)
    rc, out = _doctor(capsys, "--yes")
    assert rc == 1 and "routing_opt_out" in out, out
    assert box.settings.read_bytes() == before and box.backups() == []
    assert _load_hook()._check_proxy_default_health() == ""


def test_shim_down_refuses(box, capsys):
    shim, main = box.dead(), box.listening()
    box.sentinel(shim, main)
    before = box.write_settings({"FOO": "bar"})
    rc, out = _doctor(capsys, "--yes")
    assert rc == 1 and f"nothing answers on 127.0.0.1:{shim}" in out, out
    assert box.settings.read_bytes() == before and box.backups() == []


def test_state7_shim_up_but_main_proxy_down_refuses(box, capsys):
    shim, main = box.listening(), box.dead()
    box.sentinel(shim, main)
    before = box.write_settings({"FOO": "bar"})
    rc, out = _doctor(capsys, "--yes")
    assert rc == 1 and f"main proxy on :{main}" in out, out
    assert box.settings.read_bytes() == before and box.backups() == []


# ── the other refusals and the confirmation path ─────────────────────────────

def test_no_sentinel_refuses_and_uninstall_leaves_nothing_to_repair(box, capsys):
    box.listening()
    before = box.write_settings({"FOO": "bar"})
    rc, out = _doctor(capsys, "--yes")
    assert rc == 1 and "not installed" in out
    # install-then-uninstall: uninstall removes the sentinel, so the repair refuses
    shim, main = box.listening(), box.listening()
    box.sentinel(shim, main)
    cmd.uninstall_proxy_default(home=box.home, system="Darwin",
                                runner=lambda c, **k: __import__("subprocess").CompletedProcess(c, 0, "", ""))
    assert pd.read_sentinel() is None
    rc, out = _doctor(capsys, "--yes")
    assert rc == 1 and "not installed" in out
    assert box.settings.read_bytes() == before


def test_sentinel_not_enabled_refuses(box, capsys):
    shim, main = box.listening(), box.listening()
    box.sentinel(shim, main, enabled=False)
    before = box.write_settings({})
    rc, out = _doctor(capsys, "--yes")
    assert rc == 1 and "not enabled" in out and box.settings.read_bytes() == before


def test_environment_override_to_another_host_refuses(box, capsys, monkeypatch):
    shim, main = box.listening(), box.listening()
    box.sentinel(shim, main)
    before = box.write_settings({})
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.net")
    rc, out = _doctor(capsys, "--yes")
    assert rc == 1 and "the environment" in out and "gateway.example.net" in out
    assert box.settings.read_bytes() == before


def test_environment_still_pointing_at_this_proxy_is_not_an_override(box, capsys, monkeypatch):
    """The incident shape: a session started before the removal keeps the old value in
    its environment while settings.json has lost it. That is the case to repair."""
    shim, main = box.listening(), box.listening()
    box.sentinel(shim, main)
    box.write_settings({})
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{shim}")
    rc, out = _doctor(capsys, "--yes")
    assert rc == 0 and "fix-routing written" in out, out


def test_cwd_at_home_does_not_mistake_the_user_file_for_a_project_override(box, capsys, monkeypatch):
    shim, main = box.listening(), box.listening()
    box.sentinel(shim, main)
    box.write_settings({"ANTHROPIC_BASE_URL": ""})  # present but empty == missing
    monkeypatch.chdir(box.home)
    rc, out = _doctor(capsys, "--yes")
    assert rc == 0 and "fix-routing written" in out, out
    assert '-  env.ANTHROPIC_BASE_URL: ""' in out


def test_unparseable_settings_is_never_touched(box, capsys):
    shim, main = box.listening(), box.listening()
    box.sentinel(shim, main)
    box.settings.parent.mkdir(parents=True)
    box.settings.write_text("{ not json")
    rc, out = _doctor(capsys, "--yes")
    assert rc == 1 and "does not parse" in out
    assert box.settings.read_text() == "{ not json"


def test_non_interactive_without_yes_shows_the_diff_and_writes_nothing(box, capsys):
    shim, main = box.listening(), box.listening()
    box.sentinel(shim, main)
    before = box.write_settings({"FOO": "bar"})
    rc, out = _doctor(capsys)  # pytest's stdin is not a tty
    assert rc == 1 and "--yes" in out and "+  env.ANTHROPIC_BASE_URL" in out
    assert box.settings.read_bytes() == before and box.backups() == []


def test_interactive_confirm_sees_the_diff_before_any_write(box):
    shim, main = box.listening(), box.listening()
    box.sentinel(shim, main)
    before = box.write_settings({"FOO": "bar"})
    seen = []

    def no(diff):
        seen.append((list(diff), box.settings.read_bytes()))
        return False

    assert cmd.fix_routing(confirm=no)["status"] == "cancelled"
    assert box.settings.read_bytes() == before
    assert seen and seen[0][1] == before and any("+  env.ANTHROPIC_BASE_URL" in d for d in seen[0][0])
    assert cmd.fix_routing(confirm=lambda diff: True)["status"] == "written"


def test_decline_sets_the_flag_keeps_the_sentinel_and_reinstall_opts_back_in(box, capsys):
    shim, main = box.listening(), box.listening()
    box.sentinel(shim, main, tiers="conversation")
    box.write_settings({})
    rc, out = _doctor(capsys, "--decline")
    assert rc == 0 and "declined" in out
    s = pd.read_sentinel()
    assert s["routing_opt_out"] is True and s["port"] == shim and s["tiers"] == "conversation"
    assert _doctor(capsys, "--yes")[0] == 1
    # a fresh `install --proxy-default` rewrites the sentinel without the flag
    pd.write_sentinel(port=shim, steps="off", tiers="conversation", label=pd.LABEL, system="Darwin",
                      upstream_port=main, shim_label=pd.SHIM_LABEL)
    assert "routing_opt_out" not in pd.read_sentinel()
    assert _doctor(capsys, "--yes")[0] == 0


def test_decline_without_a_sentinel_refuses(box, capsys):
    rc, out = _doctor(capsys, "--decline")
    assert rc == 1 and "nothing to decline" in out and not pd.sentinel_path().exists()


def test_no_write_when_the_backup_cannot_be_taken(box, capsys, monkeypatch):
    import llm_router.install_hooks as ih

    shim, main = box.listening(), box.listening()
    box.sentinel(shim, main)
    before = box.write_settings({"FOO": "bar"})
    monkeypatch.setattr(ih, "_backup_before_overwrite", lambda dst: None)
    rc, out = _doctor(capsys, "--yes")
    assert rc == 1 and "could not back up" in out
    assert box.settings.read_bytes() == before and box.write_rows() == []


def test_fix_routing_skips_the_full_doctor_scan(box, capsys, monkeypatch):
    import llm_router.commands.doctor as doc

    monkeypatch.setattr(doc, "_run_doctor", lambda **k: (_ for _ in ()).throw(AssertionError("full scan ran")))
    rc, out = _doctor(capsys, "--yes")
    assert rc == 1 and "not installed" in out


def test_the_real_cli_entry_point_writes_then_noops_in_a_sandboxed_home(box):
    """End to end through `llm-router` (cli.main), a subprocess with HOME = the sandbox."""
    import subprocess

    shim, main = box.listening(), box.listening()
    box.sentinel(shim, main)
    box.write_settings({"FOO": "bar"})
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_BASE_URL"}
    argv = [sys.executable, "-c", "import sys; from llm_router.cli import main; sys.argv=['llm-router']+sys.argv[1:]; main()",
            "doctor", "--fix-routing", "--yes"]
    first = subprocess.run(argv, env=env, cwd=box.proj, capture_output=True, text=True, timeout=120)
    assert first.returncode == 0 and "fix-routing written" in first.stdout, first.stdout + first.stderr
    second = subprocess.run(argv, env=env, cwd=box.proj, capture_output=True, text=True, timeout=120)
    assert second.returncode == 0 and "fix-routing noop" in second.stdout, second.stdout + second.stderr
    assert json.loads(box.settings.read_text())["env"]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{shim}"


# ── explicit only: no hook, no plain install ─────────────────────────────────

def test_no_hook_imports_or_names_the_fix_path():
    hook_files = sorted((ROOT / "hooks").glob("*")) + sorted((ROOT / "src" / "llm_router" / "hooks").glob("*"))
    hook_files = [p for p in hook_files if p.is_file() and p.suffix in (".py", ".sh")]
    assert len(hook_files) >= 20
    for path in hook_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in ("fix_routing", "_wire_settings_env", "commands.proxy_default",
                       "commands import proxy_default", "set_routing_opt_out"):
            assert needle not in text, f"{path.name} names {needle}"


def test_install_command_never_reaches_the_fix_path():
    for rel in ("commands/install.py", "install_hooks.py", "commands/proxy_default.py"):
        tree = ast.parse((ROOT / "src" / "llm_router" / rel).read_text())
        for fn in tree.body:
            if not isinstance(fn, ast.FunctionDef) or fn.name in ("cmd_fix_routing", "fix_routing"):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    f = node.func
                    name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
                    assert name not in ("fix_routing", "cmd_fix_routing"), f"{rel}:{fn.name}"


# ── SessionStart observe rows ────────────────────────────────────────────────

def _run_session_start(mod, monkeypatch, session_id: str) -> None:
    monkeypatch.setattr(mod, "_refresh_claude_usage_nonblocking", lambda: "\n✅ Usage: cached")
    monkeypatch.setattr(mod, "_check_proxy_default_health", lambda: "")
    monkeypatch.setattr(mod, "_spawn_background_session_work", lambda *a, **k: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": session_id, "cwd": os.getcwd()})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    mod.main()


def test_each_session_start_appends_one_observe_row(box, monkeypatch):
    box.sentinel(box.listening(), None)
    env = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787", "FOO": "bar"}
    box.write_settings(env)
    mod = _load_hook()
    _run_session_start(mod, monkeypatch, "sess-a")
    box.write_settings({"FOO": "bar"})  # the key disappears between two sessions
    _run_session_start(mod, monkeypatch, "sess-b")

    rows = box.write_rows()
    assert [r["kind"] for r in rows] == ["observe", "observe"]
    assert [r["session_id"] for r in rows] == ["sess-a", "sess-b"]
    assert [r["base_url_present"] for r in rows] == [True, False]
    assert rows[0]["env_sha256"] == cmd.env_block_sha256(env)
    assert rows[1]["env_sha256"] == cmd.env_block_sha256({"FOO": "bar"})
    assert all(isinstance(r["mtime"], float) and r["ts"].endswith("Z") for r in rows)
    assert "127.0.0.1" not in box.writes.read_text()  # presence only, never the value


def test_no_observe_row_when_proxy_default_is_not_installed(box, monkeypatch):
    box.write_settings({"FOO": "bar"})
    _run_session_start(_load_hook(), monkeypatch, "sess-x")
    assert not box.writes.exists()


def test_unreadable_settings_is_recorded_as_unknown_not_absent(box, monkeypatch):
    box.sentinel(box.listening(), None)
    box.settings.parent.mkdir(parents=True)
    box.settings.write_text("{ broken")
    _run_session_start(_load_hook(), monkeypatch, "sess-u")
    [row] = box.write_rows()
    assert row["base_url_present"] is None and row["settings_exists"] is True


# ── one rule: the doctor and the hook read the same thing ────────────────────

@pytest.mark.parametrize("value", [
    None, "", "http://127.0.0.1:8787", "http://localhost:8787/", "127.0.0.1:8797",
    "http://[::1]:8787", "http://127.0.0.1:9999", "https://api.anthropic.com",
    "http://127.0.0.1", "http://127.0.0.1:notaport", "http://10.0.0.1:8787",
])
def test_local_port_rule_matches_the_hook(value):
    hook = _load_hook()
    assert cmd.routes_to_local_port(value, [8787, 8797]) == hook._routes_to_local_port(value, [8787, 8797])


@pytest.mark.parametrize("env", [None, {}, {"A": "1"}, {"b": 2, "a": [1, {"z": None}]}, "not-a-dict"])
def test_env_hash_matches_the_hook(env):
    assert cmd.env_block_sha256(env) == _load_hook()._env_block_sha256(env)


@pytest.mark.parametrize("files", [
    {"settings.local.json": "https://a.example:1"},
    {"settings.json": "https://b.example:2"},
    {"settings.local.json": "https://a.example:1", "settings.json": "https://b.example:2"},
    {"settings.local.json": "  ", "settings.json": "https://b.example:2"},
    {},
])
def test_project_override_rule_matches_the_hook(box, files):
    for name, url in files.items():
        p = box.proj / ".claude" / name
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": url}}))
    box.write_settings({})  # user file sets nothing, so the hook can only find a project value
    assert cmd._project_override(str(box.proj)) == _load_hook()._effective_base_url(str(box.proj))
