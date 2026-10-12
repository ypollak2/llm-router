"""Fixture state directories (``LLM_ROUTER_HOME``) in the shapes an upgrade meets."""
from __future__ import annotations

import json
import time
from pathlib import Path

#: User-owned vs generated (P2.12-c), derived from the writers in the code:
#:   user-owned: the user edits it and the router only reads it. ``profile.yaml``
#:   (auto_profile._profile_path: the router writes it only while it is still
#:   router-written, P0.15), ``routing.yaml`` (pins), ``.env`` (keys; install only
#:   appends the subscription flag when a Claude seat is found), ``notes.txt``
#:   (arbitrary user file in the state dir), ``config.yaml`` (safe_config.load_safe_config:
#:   read only), ``session_kind_overrides.json`` (session_kind: read only).
#:   generated: the router rewrites it at will (``GENERATED``); a user never edits it.
#: ``usage.db`` is history: the router appends rows, the upgrade must keep them
#: (row count checked in test_p212_upgrade_from_15_4_0), so it is in neither list.
USER_OWNED = ("profile.yaml", "routing.yaml", ".env", "notes.txt", "config.yaml", "session_kind_overrides.json")
GENERATED = (
    "profile.generated.yaml", "profile_cache.json", "profile_hash.json",
    "discovery.json", "ollama_last_ok.txt",
    "grandfathered_models.json", "grandfathered_models.init",
    "seats.json", "usage.json", "install-manifest.json", ".claude.json",
)
#: directories the installer owns wholesale inside the state dir
GENERATED_DIRS = ("hooks",)
#: directories the installer may CREATE files in; a file already there (the user's
#: ``claude-home/settings.json``) is user-edited and protected by sha
GENERATED_NEW_ONLY_DIRS = ("claude-home", "claude-desktop")


def user_owned_files(home: Path) -> list[Path]:
    return [home / n for n in USER_OWNED if (home / n).exists()]

_PROFILE_BODY = (
    "routing:\n  model_priority:\n    - tier: free_local\n      models: ['ollama/mine:latest']\n"
    "quotas:\n  claude_subscription:\n    remaining_percent: 80%\n"
)


def write_discovery(home: Path, models: list[str]) -> None:
    (home / "discovery.json").write_text(json.dumps({
        "cached_at": time.time(),
        "models": {f"ollama/{m}": {"model_id": f"ollama/{m}", "provider": "ollama"} for m in models},
    }))


def make_home(root: Path, shape: str, *, models: tuple[str, ...] = ("old-a:latest", "old-b:latest")) -> Path:
    """Create ``root/<shape>`` and return it.

    shapes: ``fresh`` (nothing), ``upgrade`` (discovery.json + usage.db + legacy
    profile.yaml with no recorded hash), ``edited`` (router-written then edited),
    ``crlf``, ``empty``, ``nonyaml`` (user profile.yaml variants on an upgrade).
    """
    home = root / shape
    home.mkdir(parents=True)
    if shape == "fresh":
        return home
    write_discovery(home, list(models))
    (home / "usage.db").write_bytes(b"")
    (home / "routing.yaml").write_text("routing:\n  code:\n    model: ollama/pinned:latest\n")
    (home / ".env").write_text("# my keys\nOLLAMA_BUDGET_MODELS=\"\"\n")
    (home / "notes.txt").write_bytes(b"mine\r\n")
    (home / "config.yaml").write_text("log_level: info\n")
    (home / "session_kind_overrides.json").write_text("{}\n")
    body = {
        "upgrade": _PROFILE_BODY,
        "edited": _PROFILE_BODY + "# my own comment\n",
        "crlf": _PROFILE_BODY.replace("\n", "\r\n"),
        "empty": "",
        "nonyaml": "{{ this is: not [yaml\n\t- x: : :\n",
    }[shape]
    (home / "profile.yaml").write_bytes(body.encode())
    return home
