"""Regression: CHZ-SEC-01/02/09 + ST-006 — one shared secret scrubber.

Three drifted scrubbers (secret_scrubber / session_store / error_sanitization)
meant `transcript_*.jsonl` persisted full prompts AND responses unscrubbed at
0644, `pending_route_*.json` stored the raw prompt, and session_store missed
`password:`. The fix makes `secret_scrubber.scrub_text` the single superset;
every content store delegates to it. These tests drive a secret battery through
the real writers and assert 0 unredacted occurrences + 0600 perms.
"""

from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path

import pytest

from llm_router.secret_scrubber import scrub_text
from llm_router import session_store

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"

# A battery of realistic-shaped secrets (values are fake but pattern-matching).
BATTERY = {
    "anthropic": "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA1234",
    "openai": "sk-proj-BBBBBBBBBBBBBBBBBBBBBBBB5678",
    "google": "AIzaCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
    "aws": "AKIA1234567890ABCDEF",
    "github": "ghp_DDDDDDDDDDDDDDDDDDDDDDDDDDDDDD1234",
    "bearer": "Bearer abcdef123456.ghijkl789012",
    "password": "password: hunter2secretpw",
    "env_key": "MY_SERVICE_API_KEY=zzzzzzzzzzzzzzzz",
}


def _load_hook():
    spec = importlib.util.spec_from_file_location("llm_router_auto_route_scrub", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load_hook()


@pytest.mark.parametrize("name,secret", list(BATTERY.items()))
def test_canonical_scrubber_redacts_every_battery_secret(name, secret) -> None:
    out = scrub_text(f"here is a value {secret} in context")
    # The raw secret token must not survive.
    core = secret.split("=")[-1].split(":")[-1].strip().split()[-1]
    assert core not in out, f"{name}: secret survived canonical scrub_text: {out!r}"
    assert "[REDACTED" in out


def test_session_store_scrubber_catches_password_drift() -> None:
    """The specific drift the audit found: session_store missed `password:`."""
    out = session_store._scrub_secrets("login password: hunter2secretpw done")
    assert "hunter2secretpw" not in out, "session_store still misses password: (drift)"


def test_transcript_writer_scrubs_and_is_0600(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(hook, "_ROUTER_DIR", tmp_path)
    prompt = "deploy with " + BATTERY["anthropic"]
    draft = "sure, also " + BATTERY["aws"] + " and " + BATTERY["github"]
    hook._append_transcript_shard("sess-scrub", prompt, draft)

    path = tmp_path / "transcript_sess-scrub.jsonl"
    assert path.exists()
    body = path.read_text()

    for name, secret in [("anthropic", BATTERY["anthropic"]),
                         ("aws", BATTERY["aws"]), ("github", BATTERY["github"])]:
        core = secret.split("=")[-1]
        assert core not in body, f"transcript leaked {name} secret unredacted"

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"transcript must be 0600, got {oct(mode)}"


def test_pending_prompt_would_be_scrubbed() -> None:
    """ST-006: the pending-route prompt goes through the same scrubber."""
    out = hook._scrub_secrets_text("run deploy with " + BATTERY["openai"])
    assert BATTERY["openai"].split("-")[-1] not in out
    assert "[REDACTED" in out
