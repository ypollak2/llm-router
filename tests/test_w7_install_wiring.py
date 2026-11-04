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
from types import SimpleNamespace

import pytest

import llm_router.install_hooks as ih
from llm_router import eligibility_routing as er
import llm_router.install_manifest as im
from llm_router.text_job_bind import limits

REPO = Path(__file__).resolve().parents[1]
NAME = "llm_router-text-job-bind.py"


@pytest.fixture(autouse=True)
def lane(monkeypatch):
    """The router's lane check, stubbed: passed commit_message and pr_description lanes exist unless a test says
    otherwise (``available`` / ``pr_available``). The off switch is cleared from the environment."""
    monkeypatch.delenv("LLM_ROUTER_LOCAL_TEXT_JOBS", raising=False)
    state = SimpleNamespace(available=True, pr_available=True)
    monkeypatch.setattr(
        er, "passed_lane_available",
        lambda job, input_len, now, **kw: state.pr_available if job == "pr_description" else state.available,
    )
    return state


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


def _bundle_handlers(rel: str) -> list[tuple[str, str | None, dict]]:
    """Every (event, matcher, handler) of a generated hooks.json."""
    doc = json.loads((REPO / rel).read_text())["hooks"]
    return [(ev, g.get("matcher"), h) for ev, groups in doc.items() for g in groups for h in g["hooks"]]


def test_the_claude_plugin_bundle_registers_the_hook_for_bash_with_the_if_rule():
    handlers = _bundle_handlers("hooks/hooks.json")
    assert len(handlers) > 5, "the bundle must list the router's hooks, or the checks below prove nothing"
    found = [(ev, m, h) for ev, m, h in handlers if h["command"].endswith("/text-job-bind.py")]
    assert len(found) == 1
    ((event, matcher, h),) = found
    assert (event, matcher) == ("PreToolUse", "Bash")
    assert h["timeout"] == limits.HOOK_TIMEOUT_SEC == 130
    assert h["if"] == "Bash(*LLM_TEXT*)" and h["command"].startswith("${CLAUDE_PLUGIN_ROOT}")
    assert (REPO / "hooks" / "text-job-bind.py").read_bytes() == (REPO / "src/llm_router/hooks/text-job-bind.py").read_bytes()


def test_the_codex_plugin_bundle_does_not_ship_the_claude_only_hook():
    """The placeholder convention and the PreToolUse `updatedInput` rewrite are Claude Code's: on Codex the
    hook would never rewrite anything. The Codex file is generated from the same table, so this is the
    per-hook `_CLAUDE_ONLY_HOOKS` notion at work, not a hand edit."""
    handlers = _bundle_handlers(".codex-plugin/hooks.json")
    claude_handlers = _bundle_handlers("hooks/hooks.json")
    assert len(handlers) > 5, "an empty Codex bundle would pass every absence check"
    assert not [h for _e, _m, h in handlers if "text-job-bind" in h["command"] or "text-job-bind" in json.dumps(h)]
    assert not [h for _e, _m, h in handlers if "if" in h], "the `if` field is Claude Code's"
    assert any(h["command"].endswith("/enforce-route.py") for _e, _m, h in handlers), "other hooks stay"
    assert ih._CLAUDE_ONLY_HOOKS == {NAME}
    # the Claude bundle has everything the Codex bundle has, plus the hook (and its own plugin-only ones)
    codex_cmds = {h["command"].rsplit("/", 1)[-1] for _e, _m, h in handlers}
    claude_cmds = {h["command"].rsplit("/", 1)[-1] for _e, _m, h in claude_handlers}
    assert "text-job-bind.py" in claude_cmds - codex_cmds


def test_the_bundle_generator_leaves_a_claude_only_hook_out_for_any_other_host():
    import importlib.util

    spec = importlib.util.spec_from_file_location("w7_build_plugin_bundle", REPO / "scripts" / "build_plugin_bundle.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def commands(root_var: str) -> list[str]:
        doc = mod._hooks_json(root_var)["hooks"]
        return [h["command"] for groups in doc.values() for g in groups for h in g["hooks"]]

    claude, codex = commands("${CLAUDE_PLUGIN_ROOT}"), commands("${CODEX_PLUGIN_ROOT}")
    assert codex and claude
    assert any(c.endswith("/text-job-bind.py") for c in claude)
    assert not any(c.endswith("/text-job-bind.py") for c in codex)
    assert commands("${SOME_OTHER_ROOT}") and not any(c.endswith("/text-job-bind.py") for c in commands("${SOME_OTHER_ROOT}"))


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


# ---- the installed rules and the registration agree (F13) -------------------------------------------------

PLACEHOLDER = '"${LLM_TEXT:?}"'


def _rules(sandbox) -> str:
    return (sandbox / ".claude" / "rules" / "llm_router.md").read_text(encoding="utf-8")


def test_a_fresh_install_ends_with_the_section_and_the_registration_together(sandbox):
    ih.install(force=True)
    assert PLACEHOLDER in _rules(sandbox) and ih.text_job_bind_registered()
    assert ih.text_job_bind_rules_gap() is None
    ih.install()  # a second, unforced install rewrites nothing that matters and stays consistent
    assert PLACEHOLDER in _rules(sandbox) and ih.text_job_bind_rules_gap() is None
    assert ih.check_and_update_rules() is None


def test_an_install_that_registers_no_hooks_installs_rules_without_the_section(sandbox):
    """`via_plugin`: the plugin carries the hooks, settings.json gets none of them, so nothing here
    would replace the placeholder."""
    ih.install(force=True, via_plugin=True)
    assert not ih.text_job_bind_registered()
    assert PLACEHOLDER not in _rules(sandbox)
    assert ih.text_job_bind_rules_gap() is None


def test_uninstalling_the_hook_then_refreshing_the_rules_removes_the_section(sandbox):
    ih.install(force=True)
    sp = sandbox / ".claude" / "settings.json"
    settings = json.loads(sp.read_text())
    for entry in settings["hooks"]["PreToolUse"]:
        entry["hooks"] = [h for h in entry["hooks"] if NAME not in h["command"]]
    sp.write_text(json.dumps(settings))
    gap = ih.text_job_bind_rules_gap()
    assert gap and gap.level == "danger"
    assert "removed" in ih.check_and_update_rules()
    assert PLACEHOLDER not in _rules(sandbox) and ih.text_job_bind_rules_gap() is None


def test_gap_rules_teach_the_placeholder_but_the_hook_is_not_registered_is_the_dangerous_one(sandbox):
    ih.install(force=True)
    sp = sandbox / ".claude" / "settings.json"
    sp.write_text(json.dumps({"hooks": {}}))
    gap = ih.text_job_bind_rules_gap()
    assert gap.level == "danger"
    assert "is not registered" in gap.message and "LLM_TEXT: parameter not set" in gap.message
    assert "llm-router install" in gap.fix and "restart the MCP server" in gap.fix


def test_gap_rules_teach_the_placeholder_but_the_hook_file_is_missing(sandbox):
    ih.install(force=True)
    (sandbox / ".claude" / "hooks" / NAME).unlink()
    gap = ih.text_job_bind_rules_gap()
    assert gap.level == "danger" and "registered but its file" in gap.message and NAME in gap.message


PLAIN = "<!-- llm_router-rules-version: 12 -->\n# rules\n"


def test_gap_hook_registered_and_a_lane_exists_but_the_rules_are_stale_is_informational(sandbox):
    ih.install(force=True)
    (sandbox / ".claude" / "rules" / "llm_router.md").write_text(PLAIN)
    gap = ih.text_job_bind_rules_gap()
    assert gap.level == "info" and "does not teach the placeholder yet" in gap.message
    assert "passed lane" in gap.message and "refresh the rules" in gap.fix


def test_gap_hook_registered_rules_plain_and_no_lane_says_to_run_local_check(sandbox, lane):
    lane.available = False
    ih.install(force=True)
    assert PLACEHOLDER not in _rules(sandbox), "a plain install with no lane teaches nothing it cannot deliver"
    assert ih.text_job_bind_registered()
    gap = ih.text_job_bind_rules_gap()
    assert gap.level == "info"
    assert "hook registered, rules plain: no passed lane for commit_message yet" in gap.message
    assert "llm-router local-check" in gap.fix


def test_gap_rules_teach_the_placeholder_but_the_lane_is_gone_is_informational_and_heals_on_refresh(sandbox, lane):
    ih.install(force=True)
    assert PLACEHOLDER in _rules(sandbox) and ih.text_job_bind_rules_gap() is None
    lane.available = False
    gap = ih.text_job_bind_rules_gap()
    assert gap.level == "info" and "no_lane" in gap.message and "no passed lane for commit_message" in gap.message
    assert "removed" in ih.check_and_update_rules()
    gap = ih.text_job_bind_rules_gap()
    assert gap.level == "info" and "rules plain" in gap.message


def test_gap_is_the_danger_when_unregistered_whatever_the_lane_says(sandbox, lane):
    ih.install(force=True)
    (sandbox / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}))
    for available in (True, False):
        lane.available = available
        assert ih.text_job_bind_rules_gap().level == "danger"
    (sandbox / ".claude" / "rules" / "llm_router.md").write_text(PLAIN)
    assert ih.text_job_bind_rules_gap() is None, "unregistered and plain agree: nothing to report"


def test_no_gap_without_a_rules_file(sandbox):
    assert ih.text_job_bind_rules_gap() is None
    ih.install(force=True)
    (sandbox / ".claude" / "rules" / "llm_router.md").unlink()
    assert ih.text_job_bind_rules_gap() is None


def _verify_home(sandbox, monkeypatch):
    """`llm-router verify` looks for hook files under Path.home()/.claude; put that where the sandbox is."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: sandbox))


def test_verify_fails_and_says_what_to_do_when_the_rules_teach_a_hook_that_is_not_registered(sandbox, monkeypatch):
    from llm_router.commands.verify import check_hooks

    _verify_home(sandbox, monkeypatch)
    ih.install(force=True)
    ok, messages = check_hooks()
    assert ok is True and not [m for m in messages if "placeholder" in m], messages
    (sandbox / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}))
    ok, messages = check_hooks()
    (line,) = [m for m in messages if "placeholder" in m]
    assert ok is False and line.startswith("❌")
    assert "is not registered" in line and "LLM_TEXT: parameter not set" in line and "llm-router install" in line


@pytest.mark.parametrize("has_lane,fragment", [(True, "does not teach the placeholder yet"), (False, "no passed lane for commit_message yet")])
def test_verify_reports_a_registered_hook_with_plain_rules_without_failing(sandbox, monkeypatch, lane, has_lane, fragment):
    from llm_router.commands.verify import check_hooks

    _verify_home(sandbox, monkeypatch)
    ih.install(force=True)
    (sandbox / ".claude" / "rules" / "llm_router.md").write_text(PLAIN)
    lane.available = has_lane
    ok, messages = check_hooks()
    (line,) = [m for m in messages if fragment in m]
    assert ok is True and line.startswith("ℹ")
    assert ("local-check" in line) is (not has_lane)


def test_doctor_reports_every_mismatch_and_only_the_dangerous_one_is_an_issue(sandbox, lane):
    from llm_router.commands.doctor import _text_job_bind_rules_line

    ih.install(force=True)
    issues: list[str] = []
    assert _text_job_bind_rules_line(issues) is None and issues == []

    # danger: the rules teach the placeholder, the hook is not registered
    (sandbox / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}))
    line = _text_job_bind_rules_line(issues)
    assert "teaches the commit/PR placeholder" in line and "is not registered" in line and "fix:" in line
    assert len(issues) == 1 and "text-job-bind" in issues[0]

    ih.check_and_update_rules()  # the refresh heals it (section removed)
    issues.clear()
    assert _text_job_bind_rules_line(issues) is None and issues == []

    # info: registered, a lane exists, the rules are plain (not refreshed yet)
    ih.install(force=True)
    (sandbox / ".claude" / "rules" / "llm_router.md").write_text(PLAIN)
    line = _text_job_bind_rules_line(issues)
    assert "does not teach the placeholder yet" in line and issues == [], "informational: not an issue"

    # info: registered, no lane, rules plain
    lane.available = False
    line = _text_job_bind_rules_line(issues)
    assert "hook registered, rules plain: no passed lane for commit_message yet" in line
    assert "llm-router local-check" in line and issues == []

    # info: rules teach the placeholder but the lane is gone
    lane.available = True
    ih.check_and_update_rules()
    lane.available = False
    line = _text_job_bind_rules_line(issues)
    assert "no_lane" in line and issues == []


# ---- per part and the off switch (R8) -------------------------------------------------------------------------

PR_LINE = "gh pr create"


def test_a_plain_install_with_only_a_commit_lane_teaches_commits_and_says_prs_are_written_normally(sandbox, lane):
    lane.pr_available = False
    ih.install(force=True)
    body = _rules(sandbox)
    assert PLACEHOLDER in body and PR_LINE not in body and "Pull request bodies have no placeholder here" in body
    gap = ih.text_job_bind_rules_gap()
    assert gap.level == "info" and "rules teach commit messages only" in gap.message
    assert "pr_description" in gap.message and "local-check" in gap.fix


def test_an_install_with_the_off_switch_set_teaches_nothing(sandbox, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "0")
    ih.install(force=True)
    assert PLACEHOLDER not in _rules(sandbox) and ih.text_job_bind_registered()
    gap = ih.text_job_bind_rules_gap()
    assert gap.level == "info" and "LLM_ROUTER_LOCAL_TEXT_JOBS turns local text jobs off here" in gap.message
    assert "unset LLM_ROUTER_LOCAL_TEXT_JOBS" in gap.fix


# state after install (lanes at install time) -> state now -> (level, message fragment, fix fragment), None = consistent
CASES = {
    "full and consistent": ({}, {}, None),
    "commit only, consistent": ({"pr_available": False}, {}, ("info", "rules teach commit messages only", "local-check")),
    "pr lane gone": ({}, {"pr_available": False}, ("info", "teaches the PR placeholder but no passed lane for pr_description", "refresh the rules")),
    "pr lane appeared": ({"pr_available": False}, {"pr_available": True}, ("info", "teaches the commit placeholder only", "refresh the rules")),
    "commit lane gone": ({}, {"available": False}, ("info", "teaches the placeholder but no passed lane for commit_message", "local-check")),
    "off switch set later": ({}, {"switch": "0"}, ("info", "teaches the placeholder but LLM_ROUTER_LOCAL_TEXT_JOBS turns local text jobs off", "unset LLM_ROUTER_LOCAL_TEXT_JOBS")),
    "no commit lane yet": ({"available": False, "pr_available": False}, {}, ("info", "hook registered, rules plain: no passed lane for commit_message yet", "local-check")),
    "off switch, plain rules": ({"available": False}, {"switch": "off"}, ("info", "hook registered, rules plain: LLM_ROUTER_LOCAL_TEXT_JOBS turns local text jobs off", "unset")),
}


def _arrange(sandbox, lane, monkeypatch, at_install: dict, now: dict) -> None:
    lane.available, lane.pr_available = at_install.get("available", True), at_install.get("pr_available", True)
    ih.install(force=True)
    for k, v in now.items():
        if k == "switch":
            monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", v)
        else:
            setattr(lane, k, v)


@pytest.mark.parametrize("name", list(CASES))
def test_every_part_state_is_reported_by_the_gap_doctor_and_verify_and_none_of_it_is_an_issue(sandbox, lane, monkeypatch, name):
    from llm_router.commands.doctor import _text_job_bind_rules_line
    from llm_router.commands.verify import check_hooks

    at_install, now, expected = CASES[name]
    _verify_home(sandbox, monkeypatch)
    _arrange(sandbox, lane, monkeypatch, at_install, now)
    gap = ih.text_job_bind_rules_gap()
    issues: list[str] = []
    line = _text_job_bind_rules_line(issues)
    ok, messages = check_hooks()
    if expected is None:
        assert gap is None and line is None and issues == []
        assert ok is True and not [m for m in messages if m.startswith(("ℹ", "❌")) and "placeholder" in m]
        return
    level, message, fix = expected
    assert gap.level == level and message in gap.message and fix in gap.fix, gap
    assert line is not None and message in line and issues == [], "information is not an issue"
    (v,) = [m for m in messages if message in m]
    assert v.startswith("ℹ") and ok is True


def test_the_danger_stays_for_either_part_when_the_hook_is_not_registered_or_its_file_is_missing(sandbox, lane):
    ih.install(force=True)
    (sandbox / ".claude" / "hooks" / NAME).unlink()
    assert ih.text_job_bind_rules_gap().level == "danger"
    ih.install(force=True)
    (sandbox / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}))
    for pr_lane in (True, False):
        lane.pr_available = pr_lane
        assert ih.text_job_bind_rules_gap().level == "danger"
    # a commit-only text is dangerous too when nothing registers the hook
    lane.pr_available = False
    (sandbox / ".claude" / "rules" / "llm_router.md").write_text(
        ih._render_text_job_bind_blocks((ih._RULES_SRC / "llm_router.md").read_text(encoding="utf-8"), commit=True, pr=False))
    assert PLACEHOLDER in _rules(sandbox) and ih.text_job_bind_rules_gap().level == "danger"
