"""The ``--ignore-rules`` probe must survive CLICOLOR_FORCE=1 (coloured help from the CLI)."""

from __future__ import annotations

import stat

from llm_router import codex_agent

_PLAIN = "Usage: codex exec\n      --ignore-rules   skip rules\n"
_COLOUR = "Usage: codex exec\n      \x1b[1m--ignore\x1b[0m\x1b[1m-rules\x1b[0m   skip rules\n"


def _stub(tmp_path):
    p = tmp_path / "codex"
    p.write_text(
        "#!/bin/sh\n"
        'if [ "${CLICOLOR_FORCE:-}" = "1" ] && [ -z "${NO_COLOR:-}" ]; then\n'
        f"  printf '%s' '{_COLOUR}'\n"
        "else\n"
        f"  printf '%s' '{_PLAIN}'\nfi\n"
    )
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


def test_probe_with_clicolor_force(tmp_path, monkeypatch):
    monkeypatch.setenv("CLICOLOR_FORCE", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert codex_agent._probe_ignore_rules(_stub(tmp_path)) is True


def test_probe_strips_ansi_even_if_cli_ignores_env(tmp_path, monkeypatch):
    p = tmp_path / "codex"
    p.write_text(f"#!/bin/sh\nprintf '%s' '{_COLOUR}'\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    assert codex_agent._probe_ignore_rules(str(p)) is True
