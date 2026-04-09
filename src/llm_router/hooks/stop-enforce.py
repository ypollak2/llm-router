#!/usr/bin/env python3
# llm-router-hook-version: 1
"""Stop hook — soft stop enforcement for direct text answers.

When Claude answers a Q&A prompt in plain text (no tool call), the
pending routing state survives to this hook. Logs the violation and
increments a per-session strike counter so the next auto-route turn
can inject a stronger compliance warning.

Never blocks (exit 0 always) — the response is already generated.
Only active in hard/smart enforce modes.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_ROUTER_DIR = Path.home() / ".llm-router"
_LOG_PATH = _ROUTER_DIR / "enforcement.log"
_QA_TASK_TYPES = frozenset({"query", "research", "generate", "analyze"})

_ENV_PATHS = [
    Path.home() / ".llm-router" / ".env",
    Path.home() / ".env",
]


def _load_dotenv() -> None:
    for env_path in _ENV_PATHS:
        if not env_path.exists():
            continue
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            pass


_load_dotenv()


def _pending_path(session_id: str) -> Path:
    return _ROUTER_DIR / f"pending_route_{session_id}.json"


def _strikes_path(session_id: str) -> Path:
    return _ROUTER_DIR / f"direct_answer_strikes_{session_id}.json"


def _read_pending(session_id: str) -> dict | None:
    p = _pending_path(session_id)
    try:
        data = json.loads(p.read_text())
        if time.time() - data.get("issued_at", 0) > 300:
            p.unlink(missing_ok=True)
            return None
        return data
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _increment_strikes(session_id: str, task_type: str, expected_tool: str) -> int:
    path = _strikes_path(session_id)
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data["count"] = data.get("count", 0) + 1
    data["last_task_type"] = task_type
    data["last_expected_tool"] = expected_tool
    data["last_at"] = time.time()
    try:
        _ROUTER_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except OSError:
        pass
    return data["count"]


def _log_direct_answer(session_id: str, expected_tool: str, strikes: int) -> None:
    try:
        _ROUTER_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(
                f"[{ts}] DIRECT_ANSWER session={session_id[:12]} "
                f"expected={expected_tool} strikes={strikes}\n"
            )
    except OSError:
        pass


def main() -> None:
    enforce = os.environ.get("LLM_ROUTER_ENFORCE", "hard").lower()
    if enforce in ("off", "shadow", "soft", "suggest"):
        sys.exit(0)  # Only track violations in hard/smart mode

    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    session_id = hook_input.get("session_id", "")
    if not session_id:
        sys.exit(0)

    pending = _read_pending(session_id)
    if pending is None:
        sys.exit(0)  # Routing was honored (or no directive issued)

    task_type = pending.get("task_type", "?")

    # In smart mode, code tasks are allowed to answer via file tools — not a violation
    if enforce == "smart" and task_type not in _QA_TASK_TYPES:
        sys.exit(0)

    expected_tool = pending.get("expected_tool", "llm_route")

    # Claude answered in plain text this turn — pending state survived
    strikes = _increment_strikes(session_id, task_type, expected_tool)
    _log_direct_answer(session_id, expected_tool, strikes)

    # Clear pending — don't double-penalise on the next turn's auto-route consume
    _pending_path(session_id).unlink(missing_ok=True)

    # Soft stop: never block, never output a decision
    sys.exit(0)


if __name__ == "__main__":
    main()
