"""Host adapter protocol — every supported CLI implements install + uninstall."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class HostAdapter(Protocol):
    """Per-host config writer.

    install() returns the absolute path of the config file written; the CLI
    surfaces this so users can verify the change. uninstall() removes LLM Router's
    entry but leaves other MCP servers in the host's config untouched.
    """

    name: str

    def install(self, server_command: list[str]) -> Path:
        ...

    def uninstall(self) -> Path | None:
        ...

    def is_installed(self) -> bool:
        ...
