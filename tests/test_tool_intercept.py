"""Answer a tool call locally so its output never reaches Claude.

80.5% of tool-output tokens on this machine over 5 days were images; 14.3% were
shell output. Neither can be reduced after the call — a PostToolUse hook cannot
replace a tool result (verified live: `updatedOutput` ignored,
`additionalContext` appends). Preventing the call is the only mechanism, and a
PreToolUse `deny` carrying the answer in `permissionDecisionReason` was verified
in-session: the file was never loaded and the substitute text arrived.

The rule every test here enforces: any uncertainty returns None and the call
proceeds to Claude. The expensive right answer beats the cheap wrong one.
"""
from __future__ import annotations

import json

import pytest

from llm_router.hooks import tool_intercept as ti


@pytest.fixture(autouse=True)
def _off_by_default(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_IMAGE_INTERCEPT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_BASH_INTERCEPT", raising=False)


# ── gating ──────────────────────────────────────────────────────────────────

def test_both_intercepts_are_off_by_default():
    """A description is not a substitute for looking, and running a command in
    the hook makes the hook the executor. Both are decisions the user makes."""
    assert ti.image_intercept_enabled() is False
    assert ti.bash_intercept_enabled() is False


@pytest.mark.parametrize("val", ["1", "on", "true", "yes", "ON"])
def test_the_flags_accept_the_usual_spellings(monkeypatch, val):
    monkeypatch.setenv("LLM_ROUTER_IMAGE_INTERCEPT", val)
    monkeypatch.setenv("LLM_ROUTER_BASH_INTERCEPT", val)
    assert ti.image_intercept_enabled() and ti.bash_intercept_enabled()


def test_nothing_is_intercepted_while_off():
    assert ti.try_intercept_read(
        {"tool_name": "Read", "tool_input": {"file_path": "a.png"}}) is None
    assert ti.try_intercept_bash(
        {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}) is None


# ── images ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,expected", [
    ("a.png", True), ("a.PNG", True), ("a.jpg", True), ("a.jpeg", True),
    ("a.webp", True), ("a.gif", True),
    ("a.svg", False),   # text, not an image to a vision model
    ("a.pdf", False), ("a.py", False), ("a", False), ("", False),
])
def test_only_real_raster_images_qualify(path, expected):
    assert ti.is_image(path) is expected


def test_a_non_read_tool_is_never_intercepted_as_an_image(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_IMAGE_INTERCEPT", "1")
    assert ti.try_intercept_read(
        {"tool_name": "Write", "tool_input": {"file_path": "a.png"}}) is None


def test_no_proven_vision_model_means_claude_reads_it(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_IMAGE_INTERCEPT", "1")
    monkeypatch.setattr("llm_router.vision_registry.best_vision_model",
                        lambda **k: None)
    assert ti.try_intercept_read(
        {"tool_name": "Read", "tool_input": {"file_path": "a.png"}}) is None


def test_an_oversized_image_falls_through(tmp_path):
    big = tmp_path / "big.png"
    big.write_bytes(b"\x00" * (ti.MAX_IMAGE_BYTES + 1))
    assert ti.describe_image(str(big), "any:model") is None


def test_a_missing_file_falls_through():
    assert ti.describe_image("/nonexistent/x.png", "any:model") is None


def test_a_two_word_reply_is_treated_as_failure(monkeypatch, tmp_path):
    """A terse reply is a failure wearing a success's clothes: it would be
    substituted for the image and look like an answer."""
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"message": {"content": "a form"}}).encode()

    monkeypatch.setattr(ti.urllib.request, "urlopen", lambda *a, **k: _R())
    assert ti.describe_image(str(img), "m") is None


def test_an_unreachable_model_falls_through(monkeypatch, tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    monkeypatch.setattr(ti.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert ti.describe_image(str(img), "m") is None


# ── the substitute message ──────────────────────────────────────────────────

def test_the_message_says_it_is_not_an_error():
    """The substitute arrives wrapped as an ERROR. Text that does not say
    'this is your result, continue' invites a retry, and a retry loop costs
    more than the interception saves."""
    msg = ti.substitute_message("/x/a.png", "m:1", "a long description " * 5)
    assert "not an error" in msg.lower()
    assert "continue" in msg.lower()


def test_the_message_attributes_the_description():
    """Without this the model may report the description as its own observation
    of an image it never saw."""
    msg = ti.substitute_message("/x/a.png", "m:1", "desc " * 20)
    assert "m:1" in msg
    assert "do not present it as your own" in msg.lower()


def test_the_message_carries_the_escape_hatch():
    """The model is the only party that can judge the description insufficient,
    and it cannot ask for the real image unless it is told how."""
    msg = ti.substitute_message("/x/a.png", "m:1", "desc " * 20)
    assert "LLM_ROUTER_IMAGE_INTERCEPT=off" in msg


# ── bash ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "git status --porcelain", "git log --oneline -20", "ls -la src",
    "cat pyproject.toml", "wc -l x.py", "find . -name '*.py'", "git diff --stat",
])
def test_readonly_allowlisted_commands_qualify(command):
    assert ti._interceptable(command) is True


@pytest.mark.parametrize("command", [
    "git commit -m x",       # mutates
    "git push",              # mutates
    "rm -rf build",          # not allowlisted
    "pytest -q",             # not allowlisted: output shape matters to the model
    "curl http://x",         # network
])
def test_mutating_or_unlisted_commands_do_not_qualify(command):
    assert ti._interceptable(command) is False


@pytest.mark.parametrize("command", [
    "git status && rm -rf build",
    "git status; rm -rf build",
    "ls | rm",
    "cat x > y",
    "git status `rm -rf /`",
    "ls $(rm -rf /)",
])
def test_a_compound_command_is_refused_outright(command):
    """`git status && rm -rf build` starts with an allowlisted verb. Deciding
    which half is safe is exactly the judgement that must not live in a cost
    optimisation — so the whole thing is handed back."""
    assert ti._interceptable(command) is False


def test_a_failing_command_is_handed_back(monkeypatch, tmp_path):
    """A non-zero exit is information the model needs, and a denial reason is a
    poor place to convey a failure."""
    monkeypatch.setenv("LLM_ROUTER_BASH_INTERCEPT", "1")
    out = ti.try_intercept_bash({
        "tool_name": "Bash", "cwd": str(tmp_path),
        "tool_input": {"command": "cat definitely_missing_file.txt"}})
    assert out is None


def test_short_output_is_not_worth_intercepting(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_BASH_INTERCEPT", "1")
    (tmp_path / "small.txt").write_text("one\ntwo\n")
    assert ti.try_intercept_bash({
        "tool_name": "Bash", "cwd": str(tmp_path),
        "tool_input": {"command": "cat small.txt"}}) is None


def test_an_intercepted_command_reports_itself(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_BASH_INTERCEPT", "1")
    (tmp_path / "big.txt").write_text("\n".join(f"line {i}" for i in range(60)))
    out = ti.try_intercept_bash({
        "tool_name": "Bash", "cwd": str(tmp_path),
        "tool_input": {"command": "cat big.txt"}})
    if out is not None:            # compression may decline; that is valid
        assert "not an error" in out.lower()
        assert "LLM_ROUTER_BASH_INTERCEPT=off" in out


# ── the deny payload ────────────────────────────────────────────────────────

def test_the_deny_payload_is_the_verified_shape():
    """PreToolUse deny + permissionDecisionReason is the ONLY mechanism that
    delivers substitute content — PostToolUse updatedOutput is ignored."""
    payload = ti.deny_payload("hello")
    out = payload["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == "hello"


def test_try_intercept_dispatches_by_tool(monkeypatch):
    monkeypatch.setattr(ti, "try_intercept_read", lambda h: "IMG" if h.get("tool_name") == "Read" else None)
    monkeypatch.setattr(ti, "try_intercept_bash", lambda h: "BASH" if h.get("tool_name") == "Bash" else None)
    assert ti.try_intercept({"tool_name": "Read"}) == "IMG"
    assert ti.try_intercept({"tool_name": "Bash"}) == "BASH"
    assert ti.try_intercept({"tool_name": "Edit"}) is None
