"""The Codex UserPromptSubmit payload, captured from a real run, agrees with events.py."""
from __future__ import annotations

import json
import pathlib

from llm_router.hosts import events

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "codex_user_prompt_submit.json"


def test_verified_keys_exist_in_the_real_payload():
    payload = json.loads(FIXTURE.read_text())
    spec = events.HOSTS["codex"]
    assert payload[spec.prompt_key] == "Reply with the single word: pong"
    assert payload[spec.session_key].startswith("01a06e3d")
    assert payload["hook_event_name"] == "UserPromptSubmit"


def test_tool_keys_remain_honestly_unverified():
    assert set(events.unverified_fields("codex")) == {"tool_name_key", "tool_input_key"}
    assert events.routing_ready("codex") is False, "no PreToolUse payload captured yet"


def test_auto_route_can_tell_a_codex_session_from_the_model_field():
    """auto-route.py keys platform detection off `model`; the real payload carries it."""
    import importlib.util
    path = pathlib.Path(events.__file__).resolve().parents[1] / "hooks" / "auto-route.py"
    spec = importlib.util.spec_from_file_location("llm_router_auto_route_hook", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._is_codex_session(json.loads(FIXTURE.read_text())) is True
