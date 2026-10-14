"""Which agent hosts are installed on this machine.

A host is "present" when its binary is on PATH **or** its config directory
exists. Either alone is enough: a user who installed Codex but has never run
it has the binary and no ``~/.codex``; a user who runs Claude Code from an
IDE extension may have ``~/.claude`` and no ``claude`` on the shell PATH.

A config directory that holds nothing but what llm-router's own ``--host``
installer writes there (``_ROUTER_FILES``) is not evidence: that installer
creates the directory, so counting it would make every host it ever wired
look installed.

Pure and injectable: no network, no subprocesses. ``which``, ``home``,
``platform`` and ``environ`` are parameters so tests never touch the real
machine.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping

# host id -> (binary name, config dir relative to $HOME). "{platform}" entries
# are resolved per call by _config_dir.
_HOSTS: dict[str, tuple[str, str]] = {
    "claude-code": ("claude", ".claude"),
    "codex": ("codex", ".codex"),
    "gemini-cli": ("gemini", ".gemini"),
    "opencode": ("opencode", ".config/opencode"),
    # Copilot CLI keeps its own state in ~/.copilot.
    "copilot-cli": ("copilot", ".copilot"),
    "openclaw": ("openclaw", ".openclaw"),
    "trae": ("trae", "{platform}/Trae"),
    # Factory's CLI binary is `droid`; it owns ~/.factory.
    "factory": ("droid", ".factory"),
    "vscode": ("code", "{platform}/Code/User"),
    "cursor": ("cursor", ".cursor"),
    # The installer writes project-scoped .windsurf/mcp.json.
    "windsurf": ("windsurf", ".windsurf"),
    # Detected so `llm-router install` can report them with a reason; no
    # installer wires them yet (v16 P0.4; wiring is P2.12).
    "pi": ("pi", ".pi"),
    "kimi": ("kimi", ".kimi"),
}


#: Files llm-router's own installer writes in a host's config dir. A dir holding
#: only these (or nothing) does not count as the host being installed.
_ROUTER_FILES: dict[str, frozenset[str]] = {
    "opencode": frozenset({"config.json", "instructions.md"}),
    "copilot-cli": frozenset({"mcp.json", "instructions.md"}),
    "openclaw": frozenset({"mcp.json", "instructions.md"}),
    "trae": frozenset({"mcp.json"}),
    "cursor": frozenset({"mcp.json", "rules"}),
}


def _config_dir(rel: str, home: Path, platform: str, environ: Mapping[str, str]) -> Path:
    """``rel`` under the platform's app-config root: macOS Application Support,
    Windows ``%APPDATA%`` (as the installers use it), else ``~/.config``."""
    if not rel.startswith("{platform}/"):
        return home / rel
    tail = rel.split("/", 1)[1]
    if platform == "darwin":
        return home / "Library" / "Application Support" / tail
    if platform == "win32":
        return Path(environ.get("APPDATA") or home / "AppData" / "Roaming") / tail
    return home / ".config" / tail


def _is_host_dir(host: str, cfg_dir: Path) -> bool:
    if not cfg_dir.is_dir():
        return False
    ours = _ROUTER_FILES.get(host)
    if ours is None:
        return True
    try:
        return any(entry.name not in ours for entry in cfg_dir.iterdir())
    except OSError:
        return False


UNDETECTABLE: dict[str, str] = {
    "desktop": (
        "GUI app config path is platform specific; handled by claude_desktop_config_path"
    ),
    "copilot": "VS Code extension, no binary or dir of its own",
}


@dataclass(frozen=True)
class HostInfo:
    host: str
    binary: str | None
    config_dir: str | None

    @property
    def present(self) -> bool:
        return self.binary is not None or self.config_dir is not None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["present"] = self.present
        return d


def detect_hosts(
    *,
    home: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, HostInfo]:
    """Return every known host keyed by id, present or not."""
    home = home or Path.home()
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    out: dict[str, HostInfo] = {}
    for host, (binary, cfg) in _HOSTS.items():
        found = which(binary)
        cfg_dir = _config_dir(cfg, home, platform, environ)
        out[host] = HostInfo(
            host=host,
            binary=found,
            config_dir=str(cfg_dir) if _is_host_dir(host, cfg_dir) else None,
        )
    return out


def present_hosts(**kw) -> list[str]:
    """Ids of the hosts that are installed, in canonical order."""
    return [h for h, info in detect_hosts(**kw).items() if info.present]
