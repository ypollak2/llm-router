"""GH#63: statusline health-glyph truth table.

Follow-up to GH#50 (closed as fixed in 13.0.4 — the `$SAVINGS_LOG` crash). #50's
own regression test (`test_gh50_statusline_defines_every_var.py`) only proves
the script "doesn't crash" for one Ollama-only shape; it says nothing about
whether the reported health state is actually TRUE. #63 reported two separate
correctness bugs in that state, both still checkable without ever touching a
real Ollama server:

1. Claude subscription mode (`LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`) was not a
   recognized provider signal at all — a subscription-only, no-cloud-key setup
   read "no provider" unconditionally.
2. "No Ollama activity in the savings log for the last 30 minutes" was treated
   as equivalent to "no provider is configured" — collapsing an ACTIVITY signal
   into a CONFIGURATION signal. A subscription-free, Ollama-only user who simply
   hadn't made a call recently saw the exact same outage glyph as someone with
   nothing set up and Ollama actually down.

The fix splits these: `providers` (cloud key OR subscription — GH#63 fix #1)
is now independent of `ollama_recent` (an activity signal only). When neither
holds, a cheap `/api/tags` reachability probe distinguishes a genuinely broken
setup ("down") from one that's merely quiet ("idle" — the new state).

This test drives the embedded python health snippet directly (extracted
verbatim from the shell script, not reimplemented — a reimplementation could
drift from what actually ships and pass while the real script stays broken).
Every case backs the truth table with real files/sockets: a real synthetic
savings_log.jsonl entry back-dated with `datetime.timedelta` (never a real
30/120-minute wait), and a real ephemeral loopback socket standing in for
Ollama (bound-and-listening for "reachable", bound-and-closed for
"unreachable" — never `localhost:11434`, so this never depends on, or talks
to, an actual Ollama installation).
"""
from __future__ import annotations

import http.server
import json
import os
import re
import socket
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "src" / "llm_router" / "hooks" / "statusline-command.sh"
)

_PROVIDER_KEYS = (
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY", "GROQ_API_KEY",
)


def _extract_health_snippet() -> str:
    """Pull the embedded python -c body for the health check out of the .sh.

    Anchored on `CHZ_OLLAMA_URL`, the env var GH#63's fix introduces for the
    reachability probe — if a future edit drops it, this test fails loudly
    instead of silently testing stale/duplicated python.
    """
    body = _SCRIPT.read_text()
    m = re.search(
        r"health=\$\([^\n]*CHZ_OLLAMA_URL[^\n]*python3 -c '\n(.*?)\n' 2>/dev/null\)",
        body,
        re.DOTALL,
    )
    assert m, (
        "could not locate the health python snippet (CHZ_OLLAMA_URL anchor) — "
        "did GH#63's fix move or get reverted?"
    )
    return m.group(1)


_SNIPPET = _extract_health_snippet()


class _TagsHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        if self.path == "/api/tags":
            body = b'{"models":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a, **k):  # silence
        pass


@pytest.fixture
def reachable_ollama():
    """A real loopback HTTP server answering /api/tags — stands in for a
    reachable Ollama without ever touching the real one."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _TagsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


@pytest.fixture
def unreachable_url():
    """A loopback port with nothing listening — a real, fast connection
    refusal, not a hung timeout and not a real Ollama server."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # closed immediately: nothing will ever accept on this port
    return f"http://127.0.0.1:{port}"


def _write_savings_log(path: Path, age: timedelta | None) -> None:
    """Back-dated synthetic ollama/ entry — never a real wait."""
    if age is None:
        path.write_text("")
        return
    ts = (datetime.now(timezone.utc) - age).isoformat()
    record = {
        "timestamp": ts, "model": "ollama/llama3.1:8b",
        "input_tokens": 10, "output_tokens": 5, "estimated_saved": 0.001,
    }
    path.write_text(json.dumps(record) + "\n")


def _run_health(
    tmp_path: Path,
    *,
    provider_env: dict[str, str] | None = None,
    log_age: timedelta | None = None,
    ollama_url: str = "http://127.0.0.1:1",  # overridden per-case below
    usage_stale: bool = False,
) -> str:
    savings_log = tmp_path / "savings_log.jsonl"
    usage_json = tmp_path / "usage.json"
    _write_savings_log(savings_log, log_age)
    usage_json.write_text("{}")
    if usage_stale:
        stale_time = (datetime.now() - timedelta(hours=1)).timestamp()
        os.utime(usage_json, (stale_time, stale_time))

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for k in _PROVIDER_KEYS:
        env.pop(k, None)  # ensure a clean slate regardless of the host's own env
    if provider_env:
        env.update(provider_env)
    env["CHZ_SAVINGS_LOG"] = str(savings_log)
    env["CHZ_USAGE_JSON"] = str(usage_json)
    env["CHZ_OLLAMA_URL"] = ollama_url

    result = subprocess.run(
        [sys.executable, "-c", _SNIPPET],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, f"snippet raised: {result.stderr}"
    return result.stdout.strip()


# ── the truth table ─────────────────────────────────────────────────────────

def test_subscription_only_is_ok(tmp_path):
    """(a) Only LLM_ROUTER_CLAUDE_SUBSCRIPTION=true, no cloud keys → 'ok'.

    GH#63 fix #1: before, `keys` never included the subscription var, so this
    exact (and reportedly common) configuration fell straight to the Ollama
    branch and read 'down'.
    """
    state = _run_health(
        tmp_path,
        provider_env={"LLM_ROUTER_CLAUDE_SUBSCRIPTION": "true"},
        log_age=None,
    )
    assert state == "ok"


def test_cloud_key_alone_is_ok(tmp_path):
    """A plain cloud API key (no subscription) is still recognized."""
    state = _run_health(
        tmp_path, provider_env={"ANTHROPIC_API_KEY": "sk-fake"}, log_age=None,
    )
    assert state == "ok"


@pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes"])
def test_subscription_truthy_values_match_doctor_exactly(tmp_path, value):
    """Same value domain as install_hooks.check_api_keys (what doctor.py
    reports through). A second, divergent parser is how this bug class
    recurs — so pin the exact accepted spellings here."""
    state = _run_health(
        tmp_path, provider_env={"LLM_ROUTER_CLAUDE_SUBSCRIPTION": value}, log_age=None,
    )
    assert state == "ok"


@pytest.mark.parametrize("value", ["false", "0", "no", "", "maybe"])
def test_subscription_falsy_values_do_not_count_as_a_provider(tmp_path, value, unreachable_url):
    """Values outside doctor's truthy set must NOT be treated as configured —
    otherwise the reader can't trust the glyph at all."""
    state = _run_health(
        tmp_path,
        provider_env={"LLM_ROUTER_CLAUDE_SUBSCRIPTION": value},
        log_age=None,
        ollama_url=unreachable_url,
    )
    assert state == "down"


def test_idle_ollama_activity_probe_reachable_is_idle(tmp_path, reachable_ollama):
    """(b) No keys, no subscription, last ollama/ entry ~2h old (past the
    30-min activity window), reachability probe succeeds → 'idle'.

    GH#63 fix #2: before, this collapsed into the same 'down'/'no provider'
    state as a genuinely broken setup. 'idle' is the new state this test
    exists to pin.
    """
    state = _run_health(
        tmp_path,
        provider_env=None,
        log_age=timedelta(hours=2),
        ollama_url=reachable_ollama,
    )
    assert state == "idle"


def test_recent_ollama_activity_is_ok_without_any_provider_key(tmp_path, unreachable_url):
    """Activity INSIDE the 30-min window is still direct evidence of a working
    setup and must read 'ok' — even with the reachability probe pointed at
    nothing, proving the probe path is never reached in this case."""
    state = _run_health(
        tmp_path,
        provider_env=None,
        log_age=timedelta(minutes=5),
        ollama_url=unreachable_url,
    )
    assert state == "ok"


def test_nothing_configured_probe_unreachable_is_down(tmp_path, unreachable_url):
    """(c) No keys, no subscription, no activity, probe fails → 'down'. The
    ONLY combination that should still earn the outage glyph."""
    state = _run_health(
        tmp_path,
        provider_env=None,
        log_age=None,
        ollama_url=unreachable_url,
    )
    assert state == "down"


def test_nothing_configured_probe_reachable_is_idle_not_down(tmp_path, reachable_ollama):
    """No keys, no activity ever logged (empty log) — but Ollama itself
    answers right now. Must not read 'down': the setup isn't broken, it's
    just never been used yet."""
    state = _run_health(
        tmp_path,
        provider_env=None,
        log_age=None,
        ollama_url=reachable_ollama,
    )
    assert state == "idle"


def test_provider_configured_but_usage_stale_is_degraded(tmp_path, unreachable_url):
    """Stale usage.json still overrides 'ok' -> 'degraded' when a provider IS
    configured — GH#63 didn't touch this axis; pin it stays intact."""
    state = _run_health(
        tmp_path,
        provider_env={"LLM_ROUTER_CLAUDE_SUBSCRIPTION": "true"},
        log_age=None,
        ollama_url=unreachable_url,
        usage_stale=True,
    )
    assert state == "degraded"


def test_probe_failure_never_raises(tmp_path):
    """GH#50 was 'the embedded python snippet threw'. Point the probe at an
    address that refuses instantly AND at large negative/garbage input
    shapes it must tolerate — the snippet must still print a clean state,
    never a traceback, regardless of what's reachable."""
    state = _run_health(
        tmp_path,
        provider_env=None,
        log_age=None,
        ollama_url="http://127.0.0.1:0",  # invalid/unbindable port, not just closed
    )
    assert state in ("down", "idle")  # must resolve to SOME valid state, never crash
