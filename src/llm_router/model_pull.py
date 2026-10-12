"""Pull a model through Ollama ``/api/pull``, only after explicit approval.

The HTTP client is injectable (``stream``) so tests never touch a network. Nothing the
provider said is kept: an error is stored as a fixed text.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlparse

DEFAULT_BASE = "http://localhost:11434"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}

Stream = Callable[[str, dict], Iterable[dict]]
Progress = Callable[[str, int, int], None]


@dataclass
class PullResult:
    status: str            # "success" | "error" | "not_approved"
    detail: str = ""
    last_completed: int = 0
    last_total: int = 0


def default_stream(url: str, body: dict) -> Iterable[dict]:
    """POST ``body`` and yield decoded NDJSON objects (loopback Ollama only)."""
    import urllib.request

    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3600) as resp:  # noqa: S310 - loopback checked by caller
        for line in resp:
            line = line.strip()
            if line:
                yield json.loads(line)


def _int(v: object) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def pull_model(model: str, *, approved: bool, stream: Stream | None = None,
               base_url: str = DEFAULT_BASE, on_progress: Progress | None = None) -> PullResult:
    """Pull ``model``. Without ``approved is True`` nothing is sent."""
    if approved is not True:
        return PullResult("not_approved", "pull needs explicit approval")
    if (urlparse(base_url).hostname or "") not in _LOOPBACK:
        return PullResult("error", "pull failed")
    send = stream or default_stream
    done = PullResult("error", "incomplete")
    try:
        for obj in send(f"{base_url.rstrip('/')}/api/pull", {"model": model, "stream": True}):
            if not isinstance(obj, dict):
                continue
            if obj.get("error"):
                return PullResult("error", "pull failed", done.last_completed, done.last_total)
            done.last_completed = _int(obj.get("completed")) or done.last_completed
            done.last_total = _int(obj.get("total")) or done.last_total
            status = str(obj.get("status", ""))
            if on_progress:
                on_progress(status, _int(obj.get("completed")), _int(obj.get("total")))
            if status == "success":
                done.status, done.detail = "success", ""
    except Exception:  # noqa: BLE001 - provider text may carry secrets
        return PullResult("error", "pull failed", done.last_completed, done.last_total)
    return done
