"""P0.4-a: no README-advertised host fails install; every detected host is
wired or reported with a reason.

Bug (da31df7): the README "Works With" table advertised ``--host pi`` and
``--host kimi``; both printed ``Unknown host(s)`` and exited 0. The plain
``llm-router install`` auto-detected gemini-cli and said nothing about it.
"""
from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest

from llm_router import host_detect
from llm_router.commands import install

_README = Path(__file__).resolve().parents[1] / "README.md"
_PLANNED = "planned (v16 P2.12)"


def _readme_host_rows() -> list[tuple[str, str]]:
    """(host id, raw row) for every row of the README "Works With" table."""
    text = _README.read_text(encoding="utf-8")
    section = text.split("## Works With", 1)[1].split("\n---", 1)[0]
    rows = []
    for line in section.splitlines():
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| Host"):
            continue
        m = re.search(r"--host\s+([a-z0-9_-]+)", line)
        # A row with no --host is the default install: Claude Code.
        rows.append((m.group(1) if m else "claude-code", line))
    return rows


def _run(args: list[str]) -> tuple[str, int]:
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            install._run_install(args)
        except SystemExit as e:  # noqa: PT012 -- the exit code is the assertion
            code = e.code if isinstance(e.code, int) else 1
    return buf.getvalue(), code


def test_readme_table_parses_every_row() -> None:
    rows = _readme_host_rows()
    # An empty parse would pass every check below; pin the size.
    assert len(rows) == 15, [h for h, _ in rows]


def test_every_readme_row_is_installable_or_labelled_planned() -> None:
    bad = []
    for host, row in _readme_host_rows():
        resolved = install._HOST_ALIASES.get(host, host)
        if resolved == "claude-code" or resolved in install._HOST_SNIPPETS:
            continue
        if _PLANNED in row and resolved in install._UNSUPPORTED_HOSTS:
            continue
        bad.append(host)
    assert bad == []


def test_every_advertised_host_installs_without_unknown(isolated_install_env) -> None:
    checked = 0
    for host, row in _readme_host_rows():
        if host == "claude-code" or _PLANNED in row:
            continue
        out, code = _run(["--host", host])
        assert "Unknown host" not in out, host
        assert code == 0, host
        checked += 1
    assert checked == 12  # 15 rows minus claude-code, pi, kimi


@pytest.mark.parametrize("host", ["pi", "kimi"])
def test_planned_host_reports_reason_and_exits_2(host, isolated_install_env) -> None:
    out, code = _run(["--host", host])
    assert code == 2
    assert "Unknown host" not in out
    line = out.strip().splitlines()[-1]
    assert line.startswith("unsupported: ")
    assert line.endswith("; planned in v16 P2.12")
    reason = line[len("unsupported: "):-len("; planned in v16 P2.12")]
    assert reason.strip(), "the reason must not be empty"


def _stub_claude_install(monkeypatch, calls: list[str]) -> None:
    monkeypatch.setattr("llm_router.install_hooks.install",
                        lambda force=False: calls.append("claude-code") or ["claude ok"])
    monkeypatch.setattr("llm_router.install_hooks.install_claw_code", lambda: [])
    monkeypatch.setattr("llm_router.install_hooks.claw_code_settings_path", lambda: None)
    monkeypatch.setattr("llm_router.install_hooks.check_api_keys", lambda: [])
    monkeypatch.setattr("llm_router.seats.refresh_seats",
                        lambda **kw: (_ for _ in ()).throw(OSError("no seats in tests")))


def test_plain_install_wires_detected_hosts_and_reports_the_rest(isolated_install_env, monkeypatch) -> None:
    home = isolated_install_env / "home"
    calls: list[str] = []
    _stub_claude_install(monkeypatch, calls)
    fake = {
        "claude-code": host_detect.HostInfo("claude-code", "/bin/claude", None),
        "codex": host_detect.HostInfo("codex", "/bin/codex", None),
        "gemini-cli": host_detect.HostInfo("gemini-cli", "/bin/gemini", None),
        "unknown": host_detect.HostInfo("unknown", "/bin/unknown", None),
    }
    monkeypatch.setattr(host_detect, "detect_hosts", lambda **kw: fake)

    out, code = _run([])

    assert code == 0
    assert calls == ["claude-code"]                                  # wired
    assert (home / ".codex" / "config.toml").exists()                # wired
    assert "llm_router" in (home / ".gemini" / "settings.json").read_text()  # wired
    reported = [ln.strip() for ln in out.splitlines() if "detected, not wired" in ln]
    assert len(reported) == 1
    assert reported[0].startswith("unknown: detected, not wired: ")
    assert reported[0].split("not wired: ", 1)[1].strip()


def test_plain_install_reports_pi_and_kimi_with_their_reason(isolated_install_env, monkeypatch) -> None:
    home = isolated_install_env / "home"
    (home / ".pi").mkdir()
    (home / ".kimi").mkdir()
    calls: list[str] = []
    _stub_claude_install(monkeypatch, calls)
    real = host_detect.detect_hosts
    monkeypatch.setattr(host_detect, "detect_hosts",
                        lambda **kw: real(home=home, which=lambda b: None))

    out, _ = _run([])

    for host in ("pi", "kimi"):
        lines = [ln.strip() for ln in out.splitlines() if ln.strip().startswith(f"{host}: detected")]
        assert lines == [f"{host}: detected, not wired: {install._UNSUPPORTED_HOSTS[host]}"]
    assert sum("detected, not wired" in ln for ln in out.splitlines()) == 2


def test_plain_install_silent_for_absent_hosts(isolated_install_env, monkeypatch) -> None:
    calls: list[str] = []
    _stub_claude_install(monkeypatch, calls)
    absent = {k: host_detect.HostInfo(k, None, None)
              for k in ("claude-code", "codex", "gemini-cli", "pi", "kimi")}
    monkeypatch.setattr(host_detect, "detect_hosts", lambda **kw: absent)
    out, _ = _run([])
    assert "detected" not in out
    assert not (isolated_install_env / "home" / ".gemini" / "settings.json").exists()


def test_npm_postinstall_points_at_the_plugin() -> None:
    js = (Path(__file__).resolve().parents[1] / "npm" / "install.js").read_text()
    assert "next step ->  llm-router install" in js
    assert "/plugin install llm-router@ypollak2" in js
