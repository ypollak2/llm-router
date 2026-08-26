"""P0-6 — README headline install + package identity integrity.

Pins the three packaging defects the audit flagged:
1. installed wheels reported a stale hardcoded version (`10.1.2`);
2. `llm_router install --host claude-code` errored "Unknown host";
3. generated MCP configs invoked the deprecated `claude-code-llm_router`
   package instead of the canonical `llm_router` stdio entry point.
"""
from __future__ import annotations

import contextlib
import importlib.metadata
import io

import llm_router
from llm_router.commands import install


# ── 1. Version reporting matches the installed distribution ─────────────────


def test_version_matches_installed_dist() -> None:
    assert llm_router.__version__ == importlib.metadata.version("llm-routing")


def test_version_is_not_the_stale_hardcoded_fallback() -> None:
    assert llm_router.__version__ != "10.1.2"


# ── 2. Every documented `--host` resolves (no "Unknown host") ───────────────


def _run_host(args: list[str]) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        install._run_install(args)
    return buf.getvalue()


def test_claude_desktop_alias_resolves_to_desktop_snippet() -> None:
    out = _run_host(["--host", "claude-desktop"])
    assert "Unknown host" not in out
    assert "Claude Desktop" in out


def test_claude_code_host_routes_to_default_install_not_unknown(monkeypatch) -> None:
    # `--host claude-code` is the default install target — it must route to
    # the full installer, never error as an unknown host. Stub the heavy
    # install so the test stays hermetic; assert the snippet printer is NOT
    # reached (i.e. it fell through to the default path).
    install_host_calls: list[str] = []
    monkeypatch.setattr(install, "_install_host", lambda h: install_host_calls.append(h))
    # The default branch imports from install_hooks; stub the work out.
    import llm_router.install_hooks as ih
    monkeypatch.setattr(ih, "install", lambda *a, **k: [], raising=False)
    out = _run_host(["--host", "claude-code", "--check"])
    assert "Unknown host" not in out
    assert install_host_calls == []  # did NOT go down the snippet path


def test_all_snippet_hosts_resolve() -> None:
    for host in install._HOST_SNIPPETS:
        out = _run_host(["--host", host])
        assert "Unknown host" not in out, host


# ── 3. Generated configs use the canonical `llm_router` entry, not the dead pkg ──


def test_host_snippets_never_reference_deprecated_package() -> None:
    for host, snippet in install._HOST_SNIPPETS.items():
        assert "claude-code-llm_router" not in snippet, f"{host} snippet references dead package"


def test_host_snippets_invoke_canonical_command() -> None:
    # Any snippet that shows an MCP server command should invoke `llm_router`
    # (the stdio entry), never `uvx <pkg>`.
    for host, snippet in install._HOST_SNIPPETS.items():
        if "command" in snippet:
            assert ("command: llm-router" in snippet) or ('"command": "llm-router"' in snippet), host
            assert "uvx" not in snippet, f"{host} still uses uvx"
