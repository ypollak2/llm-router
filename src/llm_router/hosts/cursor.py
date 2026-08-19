"""Cursor host adapter — writes ~/.cursor/mcp.json fragment for LLM Router.

Cursor's MCP config lives at ~/.cursor/mcp.json. The schema is:
    {"mcpServers": {"<server_name>": {"command": "...", "args": [...]}}}

We append (or update) the "llm_router" key without disturbing other servers.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CursorAdapter:
    name: str = "cursor"
    config_path: Path = Path.home() / ".cursor" / "mcp.json"

    def install(self, server_command: list[str]) -> Path:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        config = self._read_or_init()
        config.setdefault("mcpServers", {})
        config["mcpServers"]["llm_router"] = {
            "command": server_command[0],
            "args": server_command[1:],
        }
        self.config_path.write_text(json.dumps(config, indent=2))
        return self.config_path

    def uninstall(self) -> Path | None:
        if not self.config_path.exists():
            return None
        config = self._read_or_init()
        config.get("mcpServers", {}).pop("llm_router", None)
        self.config_path.write_text(json.dumps(config, indent=2))
        return self.config_path

    def is_installed(self) -> bool:
        if not self.config_path.exists():
            return False
        return "llm_router" in self._read_or_init().get("mcpServers", {})

    def _read_or_init(self) -> dict:
        if not self.config_path.exists():
            return {}
        try:
            return json.loads(self.config_path.read_text())
        except json.JSONDecodeError:
            return {}
