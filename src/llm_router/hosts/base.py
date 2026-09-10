"""Host adapter protocol — every supported CLI implements install + uninstall."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

# Keys that must never be copied into a host config, even though they match the
# LLM_ROUTER_ prefix. Editor config files get synced between machines, opened in
# screenshares and committed by accident; whatever else a host config is, it is
# not a secret store. Provider credentials are read from the environment or the
# .env file at request time and stay there.
_NEVER_PROPAGATE = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def routing_env() -> dict[str, str]:
    """The ``LLM_ROUTER_*`` settings a host must pass through to the server.

    Config is portable across hosts; environment is not, and nothing was carrying
    it. The tuned model selection lives in Claude Code's `settings.json` `env`
    block, which Cursor, OpenCode, Windsurf and Codex never read — so spawned from
    any of them the server fell back to a built-in default that is not installed
    here, and said so on the way past:

        ensemble: local classify via ollama/qwen2.5:7b failed:
            OllamaException - {"error":"model 'qwen2.5:7b' not found"}

    Tool serving is unaffected, so this degraded rather than broke, which is how it
    went unnoticed: the classifier silently stopped being the tuned one on every
    host except the one whose settings file happened to hold the values.
    """
    out: dict[str, str] = {}
    for key, value in os.environ.items():
        if not key.startswith("LLM_ROUTER_"):
            continue
        if any(marker in key.upper() for marker in _NEVER_PROPAGATE):
            continue
        out[key] = value
    return out


class HostAdapter(Protocol):
    """Per-host config writer.

    install() returns the absolute path of the config file written; the CLI
    surfaces this so users can verify the change. uninstall() removes LLM Router's
    entry but leaves other MCP servers in the host's config untouched.

    ``env`` is written into the host's MCP stdio ``env`` field when non-empty, so
    the server starts with the same routing configuration regardless of which
    editor launched it. Omitted entirely when empty — an empty ``env: {}`` is noise
    in a file humans read and edit.
    """

    name: str

    def install(self, server_command: list[str], env: dict[str, str] | None = None) -> Path:
        ...

    def uninstall(self) -> Path | None:
        ...

    def is_installed(self) -> bool:
        ...
