"""W7 text-job-bind: the hook is registered by the installer, with its own timeout, and the plugin
bundle carries the same registration.

A hook file that is not registered never runs, and the router's own ``check_and_update_hooks``
only copies files: registration is the installer's job. So this is the deploy step (the owner
runs ``llm-router install --force`` once), and this file pins what that step must produce.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import llm_router.install_hooks as ih
import llm_router.install_manifest as im
from llm_router.text_job_bind import limits

REPO = Path(__file__).resolve().parents[1]
NAME = "llm_router-text-job-bind.py"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    claude = tmp_path / ".claude"
    claude.mkdir()
    monkeypatch.setattr(ih, "_CLAUDE_DIR", claude)
    monkeypatch.setattr(ih, "_HOOKS_DST", claude / "hooks")
    monkeypatch.setattr(ih, "_RULES_DST", claude / "rules")
    monkeypatch.setattr(ih, "_SETTINGS_PATH", claude / "settings.json")
    monkeypatch.setattr(ih, "_CLAUDE_JSON_PATH", tmp_path / ".claude.json")
    desktop = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(ih, "claude_desktop_config_path", lambda: desktop)
    monkeypatch.setattr(im, "_manifest_path", lambda: tmp_path / ".llm-router" / "install-manifest.json")
    (tmp_path / ".llm-router").mkdir()
    import shutil as _sh
    real_which = _sh.which
    monkeypatch.setattr(ih.shutil, "which", lambda n: None if n == "claude" else real_which(n))
    return tmp_path


def _entries(settings: dict) -> list[tuple[str, dict]]:
    return [(e.get("matcher", ""), h) for e in settings["hooks"]["PreToolUse"] for h in e["hooks"]
            if NAME in h.get("command", "")]


def test_the_hook_is_in_the_install_manifest_for_bash_only():
    (d,) = [d for d in ih._HOOK_DEFS if d[0] == "text-job-bind.py"]
    assert d == ("text-job-bind.py", NAME, "PreToolUse", "Bash")
    assert (ih._HOOKS_SRC / "text-job-bind.py").is_file()
    assert "text-job-bind.py" not in {d[0] for d in ih._CLAW_CODE_HOOK_DEFS}  # Claude Code only


def test_install_registers_it_with_the_bash_matcher_and_its_own_timeout(sandbox):
    ih.install(force=True)
    settings = json.loads((sandbox / ".claude" / "settings.json").read_text())
    ((matcher, hook),) = _entries(settings)
    assert matcher == "Bash" and hook["type"] == "command"
    assert hook["timeout"] == limits.HOOK_TIMEOUT_SEC == 130
    assert hook["if"] == "Bash(*LLM_TEXT*)", "the hook must spawn only for a placeholder command"
    others = [h for e in settings["hooks"]["PreToolUse"] for h in e["hooks"] if NAME not in h["command"]]
    assert others and not any("if" in h for h in others), "no other router hook gets an `if`"
    script = sandbox / ".claude" / "hooks" / NAME
    assert script.is_file() and os.access(script, os.X_OK)
    assert script.read_bytes() == (ih._HOOKS_SRC / "text-job-bind.py").read_bytes()
    assert hook["command"].endswith(str(script)) or str(script) in hook["command"]
    # the other PreToolUse hooks are untouched
    names = {h["command"].rsplit("/", 1)[-1] for e in settings["hooks"]["PreToolUse"] for h in e["hooks"]}
    assert {"llm_router-enforce-route.py", "llm_router-agent-route.py", NAME} <= names


def test_install_twice_registers_it_once_and_uninstall_removes_it(sandbox):
    ih.install(force=True)
    ih.install(force=True)
    settings = json.loads((sandbox / ".claude" / "settings.json").read_text())
    assert len(_entries(settings)) == 1
    ih.uninstall()
    after = json.loads((sandbox / ".claude" / "settings.json").read_text()) if (sandbox / ".claude" / "settings.json").exists() else {}
    assert not _entries(after) if after.get("hooks", {}).get("PreToolUse") else True
    assert not (sandbox / ".claude" / "hooks" / NAME).exists()


def test_the_registered_command_runs_the_installed_script_with_the_routers_interpreter(sandbox):
    ih.install(force=True)
    settings = json.loads((sandbox / ".claude" / "settings.json").read_text())
    ((_m, hook),) = _entries(settings)
    assert hook["command"].startswith(sys.executable) or "python" in hook["command"].split()[0]


def test_the_hook_waits_less_than_the_host_does():
    """The host kills a hook at its timeout and that kill is silent: the Bash call then runs. The hook must
    always have answered, even if only with a deny, before that."""
    assert limits.BUDGET_SEC + limits.MARGIN_SEC == limits.HOOK_TIMEOUT_SEC
    assert limits.MARGIN_SEC >= 20 and limits.BUDGET_SEC >= 60
    assert ih._HOOK_TIMEOUTS[NAME] == limits.HOOK_TIMEOUT_SEC
    from llm_router.text_job_bind import engine
    import inspect
    assert inspect.signature(engine.decide).parameters["budget_s"].default == limits.BUDGET_SEC


def test_the_plugin_bundle_registers_the_same_hook_for_bash():
    for rel, root_var in (("hooks/hooks.json", "${CLAUDE_PLUGIN_ROOT}"), (".codex-plugin/hooks.json", "${CODEX_PLUGIN_ROOT}")):
        hooks = json.loads((REPO / rel).read_text())["hooks"]["PreToolUse"]
        found = [(g.get("matcher"), h) for g in hooks for h in g["hooks"] if h["command"].endswith("/text-job-bind.py")]
        assert len(found) == 1, rel
        matcher, h = found[0]
        assert matcher == "Bash" and h["timeout"] == limits.HOOK_TIMEOUT_SEC and h["command"].startswith(root_var)
        # The `if` field is Claude Code's: the Claude plugin file carries it, the Codex file does not.
        assert h.get("if") == ("Bash(*LLM_TEXT*)" if root_var == "${CLAUDE_PLUGIN_ROOT}" else None), rel
    assert (REPO / "hooks" / "text-job-bind.py").read_bytes() == (REPO / "src/llm_router/hooks/text-job-bind.py").read_bytes()


def test_no_other_router_hook_that_sees_bash_rewrites_its_input(tmp_path):
    """Claude Code does not merge `updatedInput` from several hooks and does not say which one wins
    (hooks guide: avoid more than one hook rewriting the same tool's input), so this must stay the only
    router hook that returns it for Bash. Behavioural, not a scan of source text: the one other hook that
    sees Bash is run on Bash payloads and must never print `updatedInput`."""
    import subprocess

    sees_bash = [d[0] for d in ih._HOOK_DEFS if d[2] == "PreToolUse" and d[3] in ("", "Bash")]
    assert sees_bash == ["enforce-route.py", "text-job-bind.py"], sees_bash
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_ROUTER_", "CLAUDE"))}
    env.update(HOME=str(home), LLM_ROUTER_HOME=str(home / ".llm-router"))
    script = REPO / "src" / "llm_router" / "hooks" / "enforce-route.py"
    for cmd in ("ls -la", "git status", "git commit -m 'by hand'", 'git commit -m "${LLM_TEXT:?}"', "cat README.md"):
        payload = json.dumps({"session_id": "w7", "cwd": str(tmp_path), "hook_event_name": "PreToolUse",
                              "tool_name": "Bash", "tool_input": {"command": cmd}})
        r = subprocess.run([sys.executable, str(script)], input=payload, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[-500:]
        assert "updatedInput" not in r.stdout, (cmd, r.stdout[:300])


# ---- the `if` rule: the hook spawns only for a placeholder command ----------------------------------------

CMD = "/usr/bin/python3 /home/u/.claude/hooks/llm_router-text-job-bind.py"
RULE = ih._HOOK_IF[NAME]


def test_the_if_rule_names_the_placeholder_the_hook_acts_on():
    from llm_router.text_job_bind.shellscan import TOKEN_NAME

    assert RULE == f"Bash(*{TOKEN_NAME}*)"
    assert set(ih._HOOK_IF) == {NAME}, "only the text-job hook is filtered"


def test_a_new_registration_carries_the_rule_and_a_repeat_changes_nothing():
    settings: dict = {}
    assert ih._register_hook(settings, "PreToolUse", "Bash", CMD, 130, RULE) == "added"
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry == {"matcher": "Bash", "hooks": [{"type": "command", "command": CMD, "timeout": 130, "if": RULE}]}
    assert ih._hook_is_registered(settings, "PreToolUse", "Bash", CMD, 130, RULE)
    assert ih._register_hook(settings, "PreToolUse", "Bash", CMD, 130, RULE) == "existing"
    assert len(settings["hooks"]["PreToolUse"]) == 1


def test_an_older_registration_without_the_rule_is_found_stale_and_updated_in_place():
    settings = {"hooks": {"PreToolUse": [
        {"matcher": "", "hooks": [{"type": "command", "command": "/x/enforce.py"}]},
        {"matcher": "Bash", "hooks": [{"type": "command", "command": CMD, "timeout": 130}]},
    ]}}
    assert not ih._hook_is_registered(settings, "PreToolUse", "Bash", CMD, 130, RULE), "install() must not skip it"
    assert ih._hook_is_registered(settings, "PreToolUse", "Bash", CMD, 130), "without a rule asked for it is current"
    assert ih._existing_hook_if(settings, "PreToolUse", "Bash", CMD) is None
    assert ih._register_hook(settings, "PreToolUse", "Bash", CMD, 130, RULE) == "updated"
    assert [h["command"] for e in settings["hooks"]["PreToolUse"] for h in e["hooks"]] == ["/x/enforce.py", CMD]
    assert ih._existing_hook_if(settings, "PreToolUse", "Bash", CMD) == RULE
    assert ih._hook_is_registered(settings, "PreToolUse", "Bash", CMD, 130, RULE)
    assert "if" not in settings["hooks"]["PreToolUse"][0]["hooks"][0]


def test_a_changed_rule_is_updated_and_no_rule_asked_for_leaves_the_existing_one():
    settings = {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": CMD, "timeout": 130, "if": "Bash(git *)"}]}]}}
    assert ih._register_hook(settings, "PreToolUse", "Bash", CMD, 130, None) == "existing"
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["if"] == "Bash(git *)"
    assert ih._register_hook(settings, "PreToolUse", "Bash", CMD, 130, RULE) == "updated"
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["if"] == RULE


def test_duplicates_collapse_to_one_entry_that_has_the_rule():
    h = {"type": "command", "command": CMD, "timeout": 130}
    settings = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [dict(h)]}, {"matcher": "Bash", "hooks": [dict(h)]}]}}
    assert ih._register_hook(settings, "PreToolUse", "Bash", CMD, 130, RULE) == "updated"
    assert [x for e in settings["hooks"]["PreToolUse"] for x in e["hooks"]] == [{**h, "if": RULE}]


def test_install_without_force_adds_the_rule_to_an_existing_registration(sandbox):
    ih.install(force=True)
    sp = sandbox / ".claude" / "settings.json"
    settings = json.loads(sp.read_text())
    ((_m, hook),) = _entries(settings)
    del hook["if"]  # what a registration made before this change looks like
    sp.write_text(json.dumps(settings))
    actions = ih.install()  # not forced: the same file content must NOT make install() skip the registration
    after = json.loads(sp.read_text())
    ((_m2, hook2),) = _entries(after)
    assert hook2["if"] == RULE and hook2["timeout"] == 130
    assert any("text-job-bind" in a and "if none" in a and RULE in a for a in actions), actions
    ih.install()
    assert len(_entries(json.loads(sp.read_text()))) == 1
