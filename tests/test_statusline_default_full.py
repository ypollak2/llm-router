"""The default status line is the full layout, byte-for-byte as before PR #273.

Owner decision (reverses #273's fast-by-default): everyone gets the full status
line by default; the fast line is the opt-in debug mode ``LLM_ROUTER_STATUSLINE=fast``.

The reference is the script as it was just before #273, kept verbatim in
``tests/fixtures/statusline-command.pre273.sh`` (``git show
5575906^:src/llm_router/hooks/statusline-command.sh``). Both scripts run in the
same temp folder against the same fixture state, and stdout must be identical.
Mutation guard: make the fast line the default again and every state here fails.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.test_statusline_savings import _seed_savings_log, _seed_usage_db

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "src" / "llm_router" / "hooks" / "statusline-command.sh"
TICK = REPO / "src" / "llm_router" / "statusline_tick.py"
SEGMENTS = REPO / "src" / "llm_router" / "statusline_segments.py"
REFERENCE = REPO / "tests" / "fixtures" / "statusline-command.pre273.sh"
#: sha256 of the reference as `git show 5575906^:...` printed it. A reference
#: that drifts would compare the default against the wrong thing.
REFERENCE_SHA256 = "27626856d2dd0c41369acac4cbdcc66a9c453bf44e6e2703efaec181ee9983cb"


def test_reference_is_the_pre_273_script():
    assert hashlib.sha256(REFERENCE.read_bytes()).hexdigest() == REFERENCE_SHA256


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _usage(home: Path, **fields) -> None:
    (home / ".llm-router" / "usage.json").write_text(json.dumps(fields))


def _state_empty(home: Path) -> dict:
    return {}


def _state_fresh_quota(home: Path) -> dict:
    reset = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    _usage(home, session_pct=12.4, weekly_pct=41.0, sonnet_pct=3.0,
           session_resets_at=reset, updated_at=time.time())
    return {}


def _state_stale_quota(home: Path) -> dict:
    _usage(home, session_pct=88.0, weekly_pct=55.0, sonnet_pct=9.0, updated_at=time.time() - 7200)
    return {}


def _state_fallback_quota(home: Path) -> dict:
    _usage(home, session_pct=50, weekly_pct=50, sonnet_pct=50, is_fallback=True, updated_at=time.time())
    return {}


def _state_session_json(home: Path) -> dict:
    transcript = home / "t.jsonl"
    transcript.write_text(json.dumps({"message": {"usage": {
        "input_tokens": 1200, "cache_read_input_tokens": 50000,
        "cache_creation_input_tokens": 3000, "output_tokens": 400}}}) + "\n")
    return {"__stdin__": {"cwd": str(home / "my-project"), "transcript_path": str(transcript),
                          "model": {"id": "claude-opus-4-1[1m]"}}}


def _state_money_and_routes(home: Path) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    _seed_usage_db(home, [{"timestamp": now, "model": "gemini-2.5-flash", "provider": "gemini",
                           "input_tokens": 1000, "output_tokens": 500, "cost_usd": 0.001,
                           "success": 1, "baseline_model": "claude-opus", "saved_usd": 0.42}])
    _seed_savings_log(home, [{"timestamp": datetime.now(timezone.utc).isoformat(),
                              "model": "ollama/qwen", "saved_usd": 0.1, "input_tokens": 10,
                              "output_tokens": 10}])
    (home / ".llm-router" / "last_route_s.json").write_text(json.dumps(
        {"task_type": "code", "tool": "llm_code", "saved_at": time.time() - 30}))
    return {"PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")])}


def _state_proxy_down_hard_color(home: Path) -> dict:
    (home / ".llm-router" / "proxy_default.json").write_text(json.dumps(
        {"port": _closed_port(), "host": "127.0.0.1"}))
    return {"LLM_ROUTER_ENFORCE": "hard", "NO_COLOR": ""}


def _state_router_env_without_switch(home: Path) -> dict:
    """A router .env that does not name the switch leaves the default alone."""
    (home / ".llm-router" / ".env").write_text("LLM_ROUTER_ENFORCE=soft\nOLLAMA_URL=x\n")
    return {}


def _state_explicit_full(home: Path) -> dict:
    """LLM_ROUTER_STATUSLINE=full (what #273 told people to set) still means full."""
    _usage(home, session_pct=5.0, weekly_pct=7.0, sonnet_pct=1.0, updated_at=time.time())
    return {"LLM_ROUTER_STATUSLINE": "full"}


STATES = {
    "empty home": _state_empty,
    "fresh quota with reset": _state_fresh_quota,
    "stale quota": _state_stale_quota,
    "fallback quota": _state_fallback_quota,
    "session json cwd/ctx/1m": _state_session_json,
    "money + last route": _state_money_and_routes,
    "proxy down, hard, colour": _state_proxy_down_hard_color,
    "router .env without the switch": _state_router_env_without_switch,
    "explicit full": _state_explicit_full,
}


def _run(script: Path, home: Path, extra: dict, stdin: dict) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TERM": "dumb",
        "NO_COLOR": "1", "LLM_ROUTER_ENFORCE": "smart",
        "OLLAMA_URL": f"http://127.0.0.1:{_closed_port()}",
    }
    env.update(extra)
    if env.get("NO_COLOR") == "":
        del env["NO_COLOR"]
    return subprocess.run(["bash", str(script)], input=json.dumps(stdin), env=env,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("name", list(STATES))
def test_default_output_matches_the_pre_273_script(tmp_path, name):
    home = tmp_path / "home"
    (home / ".llm-router").mkdir(parents=True)
    (home / "my-project").mkdir()
    extra = STATES[name](home)
    stdin = extra.pop("__stdin__", {})
    # Same folder for both, so every $0-relative probe resolves identically; the
    # tick sits beside them as install() lays it out, so a script that ran the
    # fast line by default would really print it here (not fall back for want
    # of the tick and pass by accident).
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    old, new = hooks / "old.sh", hooks / "new.sh"
    shutil.copy2(REFERENCE, old)
    shutil.copy2(SCRIPT, new)
    shutil.copy2(TICK, hooks / "llm_router_statusline_tick.py")
    # P0.9-c: the segments the script reads from its cache are computed by this file.
    shutil.copy2(SEGMENTS, hooks / "llm_router_statusline_segments.py")
    a = _run(old, home, extra, stdin)
    b = _run(new, home, extra, stdin)
    assert a.returncode == b.returncode == 0, (a.stderr, b.stderr)
    assert a.stdout.strip(), f"{name}: the reference printed nothing, the comparison is empty"
    assert b.stdout == a.stdout, f"{name}:\n pre-273: {a.stdout!r}\n now:     {b.stdout!r}"
    assert not b.stdout.startswith("llm-router · "), "the default is not the fast line"
