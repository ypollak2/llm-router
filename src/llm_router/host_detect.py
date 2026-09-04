"""Which agent hosts are installed on this machine.

A host is "present" when its binary is on PATH **or** its config directory
exists. Either alone is enough: a user who installed Codex but has never run
it has the binary and no ``~/.codex``; a user who runs Claude Code from an
IDE extension may have ``~/.claude`` and no ``claude`` on the shell PATH.

Pure and injectable: no network, no subprocesses. ``which`` and ``home`` are
parameters so tests never touch the real machine.
"""
from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

# host id -> (binary name, config dir relative to $HOME)
_HOSTS: dict[str, tuple[str, str]] = {
    "claude-code": ("claude", ".claude"),
    "codex": ("codex", ".codex"),
    "gemini-cli": ("gemini", ".gemini"),
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
) -> dict[str, HostInfo]:
    """Return every known host keyed by id, present or not."""
    home = home or Path.home()
    out: dict[str, HostInfo] = {}
    for host, (binary, cfg) in _HOSTS.items():
        found = which(binary)
        cfg_dir = home / cfg
        out[host] = HostInfo(
            host=host,
            binary=found,
            config_dir=str(cfg_dir) if cfg_dir.is_dir() else None,
        )
    return out


def present_hosts(**kw) -> list[str]:
    """Ids of the hosts that are installed, in canonical order."""
    return [h for h, info in detect_hosts(**kw).items() if info.present]
