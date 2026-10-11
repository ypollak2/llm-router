"""Fixture state directories (``LLM_ROUTER_HOME``) in the shapes an upgrade meets."""
from __future__ import annotations

import json
import time
from pathlib import Path

#: Files the user owns. The router must never change a byte of these (T9f).
USER_OWNED = ("profile.yaml", "routing.yaml", ".env", "notes.txt")

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
    body = {
        "upgrade": _PROFILE_BODY,
        "edited": _PROFILE_BODY + "# my own comment\n",
        "crlf": _PROFILE_BODY.replace("\n", "\r\n"),
        "empty": "",
        "nonyaml": "{{ this is: not [yaml\n\t- x: : :\n",
    }[shape]
    (home / "profile.yaml").write_bytes(body.encode())
    return home
