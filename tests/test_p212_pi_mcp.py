"""P2.12 (P212-6, D-315): Pi gets an MCP config only.

The installer adds ``mcpServers.llm_router`` to ``~/.pi/agent/mcp.json`` (the file and shape
Pi 0.99.1 documents in ``docs/mcp.md``), keeps every other key and server and the file mode,
refuses a malformed file without overwriting it, and runs ``pi mcp list --json`` as a smoke
when a ``pi`` binary is on PATH (``skipped`` otherwise, never a failure). Uninstall removes
only the ``llm_router`` entry.

Same tmp-HOME harness as test_p212_receipt.py. The smoke tests put a FAKE ``pi`` on PATH that
answers like ``pi mcp list --json`` (servers[].name/state/tools, errors[]; exit 1 while any
server is down), so no test runs the developer's real Pi or a real MCP server.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from llm_router.commands import install
from tests.test_p212_receipt import _add_host, _point_at

REAL_PI_BINARY = install._pi_binary  # _point_at replaces it; the smoke tests put it back
ENTRY = {"command": "llm-router", "args": []}

FAKE_PI = r'''#!{python}
import json, os, sys
from pathlib import Path
here = Path(__file__).resolve().parent
mode = (here / "mode.txt").read_text().strip() if (here / "mode.txt").exists() else "ok"
agent = os.environ.get("PI_CODING_AGENT_DIR", "")
with open(here / "calls.jsonl", "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:], "agent_dir": agent, "cwd": os.getcwd()}}) + "\n")
if mode == "garbage":
    print("pi: something went wrong")
    sys.exit(1)
odd = {{"servers-int": '{{"servers": 5}}', "errors-int": '{{"servers": [], "errors": 5}}',
       "errors-str": '{{"servers": [], "errors": "boom"}}', "not-object": '[1, 2]'}}
if mode in odd:
    print(odd[mode])
    sys.exit(1)
if mode == "binary":
    sys.stdout.buffer.write(b"\xff\xfe{{not utf-8\x80")
    sys.exit(1)
if mode == "huge":
    print(json.dumps({{"servers": [{{"name": "llm_router", "state": "failed", "tools": [], "error": "x" * 100000}}]}}))
    sys.exit(1)
cfg = json.loads((Path(agent) / "mcp.json").read_text())
servers, down = [], False
for name, entry in cfg.get("mcpServers", {{}}).items():
    ok = (name == "llm_router" and entry.get("command") == "llm-router" and mode != "fail") or \
         (name != "llm_router" and entry.get("command") == "true")
    down = down or not ok
    servers.append({{"name": name, "state": "connected" if ok else "failed",
                     "tools": ["llm_route", "llm_edit"] if ok else [],
                     "error": None if ok else "spawn " + entry.get("command", "?") + " ENOENT"}})
print(json.dumps({{"servers": servers, "errors": []}}))
sys.exit(1 if down else 0)
'''


@pytest.fixture
def home(tmp_path, monkeypatch):
    return _point_at(tmp_path / "home", monkeypatch)


def _mcp(home: Path) -> Path:
    return home / ".pi" / "agent" / "mcp.json"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _fake_pi(tmp_path: Path, monkeypatch, mode: str = "ok") -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    pi = bindir / "pi"
    pi.write_text(FAKE_PI.format(python=sys.executable))
    pi.chmod(0o755)
    (bindir / "mode.txt").write_text(mode)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(install, "_pi_binary", REAL_PI_BINARY)
    return bindir


def _calls(bindir: Path) -> list[dict]:
    f = bindir / "calls.jsonl"
    return [json.loads(ln) for ln in f.read_text().splitlines()] if f.exists() else []


USER_FILE = ('{\n  "autoEnableCodemode": false,\n  "mcpServers": {\n'
             '    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},\n'
             '    "docs": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}}\n'
             '  }\n}\n')


def _seed(home: Path, text: str = USER_FILE, mode: int = 0o640) -> Path:
    path = _mcp(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


# ── the writer ────────────────────────────────────────────────────────────────


def test_merges_into_an_existing_mcp_json_and_keeps_the_mode(home):
    path = _seed(home)
    before = json.loads(USER_FILE)

    actions = install._install_pi_files()

    after = json.loads(path.read_text())
    assert after["mcpServers"]["llm_router"] == ENTRY  # no "lifecycle" key: Pi ignores it
    assert list(after["mcpServers"]) == ["filesystem", "docs", "llm_router"]
    for name, entry in before["mcpServers"].items():
        assert after["mcpServers"][name] == entry, name
    assert after["autoEnableCodemode"] is False
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert sorted(p.name for p in path.parent.iterdir()) == ["mcp.json"]  # no INSTRUCTIONS.md, no AGENTS.md
    assert actions == [f"✓ Added llm_router MCP server to {path}"]


def test_fresh_home_gets_a_file_with_only_our_server(home):
    install._install_pi_files()
    assert json.loads(_mcp(home).read_text()) == {"mcpServers": {"llm_router": ENTRY}}


def test_second_install_changes_nothing(home):
    path = _seed(home)
    install._install_pi_files()
    sha = _sha(path)

    actions = install._install_pi_files()

    assert _sha(path) == sha
    assert actions == [f"  llm_router already in {path} (left as it is)"]


def test_an_existing_llm_router_entry_is_left_as_it_is(home):
    text = json.dumps({"mcpServers": {"llm_router": {"command": "/opt/bin/llm-router", "args": [],
                                                     "env": {"LLM_ROUTER_HOME": "/x"}}}})
    path = _seed(home, text)
    install._install_pi_files()
    assert path.read_text() == text


def test_an_older_router_entry_is_reported_and_kept(home):
    text = json.dumps({"mcpServers": {"llm-router": {"command": "uvx", "args": ["claude-code-llm-router"]}}})
    path = _seed(home, text)

    actions = install._install_pi_files()

    after = json.loads(path.read_text())
    assert after["mcpServers"]["llm-router"] == {"command": "uvx", "args": ["claude-code-llm-router"]}
    assert after["mcpServers"]["llm_router"] == ENTRY
    assert any("pi mcp remove llm-router" in a for a in actions), actions


@pytest.mark.parametrize("text", ["{not json", "[]", '{"mcpServers": []}', '{"mcpServers": null}'])
def test_a_malformed_mcp_json_is_refused_not_overwritten(home, text):
    path = _seed(home, text)

    actions = install._install_pi_files()

    assert path.read_text() == text
    assert actions[0].startswith(f"SKIP {path} ") and "left unchanged" in actions[0], actions
    assert len(actions) == 1


def test_run_full_reports_a_malformed_file_as_not_wired_with_the_reason(home, capsys):
    _add_host(home, "pi")
    path = _seed(home, "{not json")

    receipt = install.run_full(interactive=False).receipt

    row = receipt["hosts"]["pi"]
    assert row["status"] == "not_wired" and str(path) in row["reason"] and "not valid JSON" in row["reason"], row
    assert path.read_text() == "{not json"
    assert f"pi: detected, not wired: {row['reason']}" in capsys.readouterr().out


def test_host_pi_wires_and_exits_0(home, capsys):
    install._run_install(["--host", "pi"])  # a SystemExit here fails the test
    assert json.loads(_mcp(home).read_text())["mcpServers"]["llm_router"] == ENTRY
    out = capsys.readouterr().out
    assert "pi mcp list: skipped: pi binary not on PATH" in out and "Run `pi mcp list` to verify" in out


def test_host_pi_runs_the_smoke_with_pi_on_path(home, tmp_path, monkeypatch, capsys):
    bindir = _fake_pi(tmp_path, monkeypatch)
    install._run_install(["--host", "pi"])
    assert "pi mcp list: passed: llm_router connected, 2 tools" in capsys.readouterr().out
    assert [c["argv"] for c in _calls(bindir)] == [["mcp", "list", "--json"]]


def test_host_pi_exits_1_when_the_smoke_fails(home, tmp_path, monkeypatch, capsys):
    _fake_pi(tmp_path, monkeypatch, mode="fail")
    with pytest.raises(SystemExit) as e:
        install._run_install(["--host", "pi"])
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "Pi not wired: pi mcp list: llm_router failed: spawn llm-router ENOENT" in out
    assert "to verify" not in out


def test_host_pi_on_a_malformed_file_exits_1_without_a_verify_hint(home, capsys):
    path = _seed(home, "{not json")
    with pytest.raises(SystemExit) as e:
        install._run_install(["--host", "pi"])
    assert e.value.code == 1
    assert path.read_text() == "{not json"
    out = capsys.readouterr().out
    assert f"SKIP {path} is not valid JSON" in out and "to verify" not in out


def test_host_all_does_not_create_pi_config_without_pi(home, capsys):
    install._run_install(["--host", "all"])
    assert not (home / ".pi").exists()
    assert "pi: not detected" in capsys.readouterr().out


@pytest.mark.parametrize("dangling", [False, True], ids=["symlink", "dangling-symlink"])
def test_a_symlinked_mcp_json_is_refused_and_left_alone(home, tmp_path, dangling):
    target = tmp_path / "dotfiles" / "pi-mcp.json"
    target.parent.mkdir()
    if not dangling:
        target.write_text(USER_FILE)
    link = _mcp(home)
    link.parent.mkdir(parents=True)
    link.symlink_to(target)

    actions = install._install_pi_files()

    assert link.is_symlink() and os.readlink(link) == str(target)
    assert (target.read_text() == USER_FILE) if not dangling else not target.exists()
    assert actions[0].startswith(f"SKIP {link} is a symlink (to {target}); left unchanged"), actions


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes")
def test_a_read_only_mcp_json_is_refused_and_left_byte_identical(home):
    path = _seed(home, mode=0o444)
    actions = install._install_pi_files()
    assert path.read_text() == USER_FILE and stat.S_IMODE(path.stat().st_mode) == 0o444
    assert actions == [f"SKIP {path} is read-only; left unchanged. Make it writable, "
                       "then rerun `llm-router install --host pi`"]


def test_a_new_pi_config_is_private(home):
    install._install_pi_files()
    assert stat.S_IMODE(_mcp(home).stat().st_mode) == 0o600
    assert stat.S_IMODE((home / ".pi").stat().st_mode) == 0o700
    assert stat.S_IMODE((home / ".pi" / "agent").stat().st_mode) == 0o700
    assert sorted(p.name for p in (home / ".pi" / "agent").iterdir()) == ["mcp.json"]  # no temp left


def test_non_ascii_values_are_written_as_is(home):
    path = _seed(home, json.dumps({"mcpServers": {"fs": {"command": "npx", "args": ["/Users/josé/פרויקט"]}}},
                                  ensure_ascii=False))
    install._install_pi_files()
    assert "/Users/josé/פרויקט" in path.read_text(encoding="utf-8")


def test_quickstart_pi_writer_is_the_install_writer(home):
    from llm_router import cli

    cli._install_pi_files()

    agent = home / ".pi" / "agent"
    assert json.loads((agent / "mcp.json").read_text()) == {"mcpServers": {"llm_router": ENTRY}}
    assert not (agent / "INSTRUCTIONS.md").exists()


def test_the_mcp_install_plan_names_the_pi_file(home):
    _add_host(home, "pi")
    plan = install._install_plan()
    assert plan["pi"] == {"hook_files": [], "registry": str(_mcp(home))}
    assert not _mcp(home).exists()  # the plan reads only


# ── receipt row + smoke ───────────────────────────────────────────────────────


def test_run_full_wires_pi_and_the_smoke_passes_with_pi_on_path(home, tmp_path, monkeypatch, capsys):
    _add_host(home, "pi")
    bindir = _fake_pi(tmp_path, monkeypatch)

    receipt = install.run_full(interactive=False).receipt

    assert receipt["hosts"]["pi"] == {
        "status": "wired", "reason": None, "via": "files", "hook_version": None,
        "smoke": {"status": "passed", "detail": "llm_router connected, 2 tools"},
    }
    assert install.receipt_failures(receipt) == []
    agent = home / ".pi" / "agent"
    assert _calls(bindir) == [{"argv": ["mcp", "list", "--json"], "agent_dir": str(agent), "cwd": str(agent)}]
    assert "pi mcp list: passed: llm_router connected, 2 tools" in capsys.readouterr().out


def test_no_pi_binary_is_a_skipped_smoke_never_a_failure(home, tmp_path, monkeypatch):
    _add_host(home, "pi")
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(install, "_pi_binary", REAL_PI_BINARY)

    assert install.cmd_install(["--auto"]) == 0

    row = install.read_receipt()["hosts"]["pi"]
    assert row["status"] == "wired" and row["smoke"] == {"status": "skipped", "detail": "pi binary not on PATH"}


def test_smoke_status_is_outside_the_digest(tmp_path, monkeypatch):
    """P212-5 compares install paths by digest: a pi binary on one machine and not on its
    twin must not change it (the smoke rides in ``smoke``, like ``via``)."""
    a = _point_at(tmp_path / "a", monkeypatch)
    _add_host(a, "pi")
    skipped = install.run_full(interactive=False).receipt

    b = _point_at(tmp_path / "b", monkeypatch)
    _add_host(b, "pi")
    _fake_pi(tmp_path, monkeypatch)
    passed = install.run_full(interactive=False).receipt

    assert skipped["hosts"]["pi"]["smoke"]["status"] == "skipped"
    assert passed["hosts"]["pi"]["smoke"]["status"] == "passed"
    assert skipped["digest"] == passed["digest"]


def test_a_failing_smoke_is_an_error_row_and_auto_exits_1(home, tmp_path, monkeypatch):
    _add_host(home, "pi")
    _fake_pi(tmp_path, monkeypatch, mode="fail")

    assert install.cmd_install(["--auto"]) == 1

    receipt = install.read_receipt()
    row = receipt["hosts"]["pi"]
    assert row["status"] == "error" and row["smoke"]["status"] == "failed", row
    assert row["reason"] == "pi mcp list: llm_router failed: spawn llm-router ENOENT"
    assert install.receipt_failures(receipt) == [f"pi: error: {row['reason']}"]


def test_another_server_down_does_not_fail_our_smoke(home, tmp_path, monkeypatch):
    _seed(home, json.dumps({"mcpServers": {"broken": {"command": "/nonexistent", "args": []}}}))
    install._install_pi_files()
    _fake_pi(tmp_path, monkeypatch)

    smoke = install.pi_smoke()

    assert smoke == {"status": "passed",
                     "detail": "llm_router connected, 2 tools; `pi mcp list` exited 1 because of another server"}


def test_output_that_is_not_json_fails_the_smoke(home, tmp_path, monkeypatch):
    install._install_pi_files()
    _fake_pi(tmp_path, monkeypatch, mode="garbage")

    smoke = install.pi_smoke()

    assert smoke["status"] == "failed" and "exited 1 without JSON" in smoke["detail"], smoke


@pytest.mark.parametrize("mode, detail", [
    ("servers-int", "`pi mcp list --json` output has no servers list"),
    ("not-object", "`pi mcp list --json` output has no servers list"),
    ("errors-int", "llm_router not listed by `pi mcp list`"),
    ("errors-str", "llm_router not listed by `pi mcp list`"),
    ("binary", None),
    ("huge", None),
])
def test_odd_smoke_output_is_a_failed_smoke_and_the_receipt_is_still_written(home, tmp_path, monkeypatch,
                                                                              mode, detail):
    _add_host(home, "pi")
    _fake_pi(tmp_path, monkeypatch, mode=mode)

    assert install.cmd_install(["--auto"]) == 1

    receipt = install.read_receipt()
    assert receipt is not None and receipt["hosts"]["claude-code"]["status"] == "wired"
    row = receipt["hosts"]["pi"]
    assert row["status"] == "error" and row["smoke"]["status"] == "failed", row
    if detail is not None:
        assert row["smoke"]["detail"] == detail
    assert len(row["reason"]) <= len("pi mcp list: ") + install.PI_REASON_MAX + 40, len(row["reason"])


def test_a_crashing_smoke_is_an_error_row_not_an_aborted_install(home, monkeypatch):
    _add_host(home, "pi")
    monkeypatch.setattr(install, "pi_smoke", lambda *a, **k: 1 / 0)

    assert install.cmd_install(["--auto"]) == 1

    row = install.read_receipt()["hosts"]["pi"]
    assert row["status"] == "error" and row["reason"] == "pi mcp list: smoke crashed: ZeroDivisionError: division by zero"


def test_a_raising_pi_row_is_an_error_row(home, monkeypatch):
    _add_host(home, "pi")
    monkeypatch.setattr(install, "pi_row", lambda: {}["boom"])
    assert install.cmd_install(["--auto"]) == 1
    assert install.read_receipt()["hosts"]["pi"]["status"] == "error"


def test_a_relative_path_entry_still_finds_pi(home, tmp_path, monkeypatch):
    bindir = _fake_pi(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir.name}{os.pathsep}/usr/bin:/bin")
    install._install_pi_files()
    assert install.pi_smoke()["status"] == "passed"


def test_the_summary_does_not_claim_pi_auto_routes(home, capsys):
    _add_host(home, "pi")
    install.run_full(interactive=False)
    out = capsys.readouterr().out
    assert "Every Claude Code session will now auto-route tasks." in out
    assert "Pi has no hooks, so prompts are not auto-routed" in out
    assert "and Pi session" not in out


def test_smoke_reports_llm_router_missing(home, tmp_path, monkeypatch):
    _seed(home, json.dumps({"mcpServers": {"other": {"command": "true"}}}))
    _fake_pi(tmp_path, monkeypatch)
    assert install.pi_smoke() == {"status": "failed", "detail": "llm_router not listed by `pi mcp list`"}


# ── uninstall ─────────────────────────────────────────────────────────────────


def test_manifest_uninstall_removes_only_our_entry(home):
    from llm_router import install_manifest

    path = _seed(home)
    install._install_pi_files()

    install_manifest.apply_uninstall()

    assert json.loads(path.read_text()) == json.loads(USER_FILE)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_legacy_uninstall_removes_only_our_entry(home):
    path = _seed(home)
    install._install_pi_files()

    actions = install.uninstall_host_integrations()

    assert json.loads(path.read_text()) == json.loads(USER_FILE)
    assert f"✓ Removed llm_router MCP server from {path}" in actions
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
