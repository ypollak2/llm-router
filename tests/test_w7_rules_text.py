"""W7 text-job-bind: what the shipped rules teach is what the hook supports, and it reaches installs.

The rules file is loaded into every session, sub-agents included, and is the only thing that
makes a model write the placeholder. If it taught a shape the hook refuses, or if an upgrade did
not carry it, the feature would be dead on arrival. So each example line in the section is run
through the real planner, each "not supported" claim is checked against it, and the refresh the
MCP server does at start-up is exercised.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import llm_router.install_hooks as ih
from llm_router.text_job_bind import PLACEHOLDER
from llm_router.text_job_bind.plan import JOB_COMMIT, JOB_PR, Unsupported, analyse

RULES = Path(ih.__file__).parent / "rules" / "llm_router.md"
T = PLACEHOLDER


def section() -> str:
    text = RULES.read_text(encoding="utf-8")
    start = text.index("## Commit messages and PR bodies")
    return text[start:text.index("\n## ", start + 5)]


def test_the_section_teaches_the_exact_placeholder_and_its_rules():
    s = section()
    assert T == '"${LLM_TEXT:?}"' and T in s
    for must in ("Stage in its own command first", "Keep the double quotes", "write the text yourself",
                 "second `-m", "--amend", "nothing was run", "parameter not set",
                 "can't be checked before it runs"):
        assert must in s, must
    assert "@llm" not in s  # the placeholder is not a GitHub mention


def test_every_example_in_the_section_is_a_command_the_hook_supports():
    block = re.search(r"```\n(.*?)```", section(), re.S).group(1)
    lines = [ln for ln in block.splitlines() if ln.strip()]
    assert len(lines) == 2, lines
    jobs = []
    for ln in lines:
        p = analyse(ln.replace("<short title>", "A title"), "/repo")
        assert p is not None, ln
        jobs.append(p.job)
    assert jobs == [JOB_COMMIT, JOB_PR]


@pytest.mark.parametrize("cmd", [
    f"git commit --amend -m {T}", f"git commit -m {T} path/to/file", f"git commit -F msg.txt -m {T}",
    f"gh pr create --fill --body {T}", f"gh pr create --web --body {T}",
    "git commit -m \"$(echo hi) ${LLM_TEXT:?}\"",
])
def test_every_unsupported_shape_the_section_lists_is_refused(cmd):
    with pytest.raises(Unsupported):
        analyse(cmd, "/repo")


def test_the_stage_first_rule_is_the_refusal_the_section_describes():
    with pytest.raises(Unsupported) as e:
        analyse(f"git add a.py && git commit -m {T}", "/repo")
    assert e.value.code == "mutating_predecessor"
    assert analyse(f"cd /repo && git commit -m {T}", "/x").repo_dir == "/repo"
    assert analyse(f"git -C /repo commit -m {T}", "/x").repo_dir == "/repo"
    assert analyse(f"git commit -m {T} -m 'Co-Authored-By: A <a@b>'", "/x") is not None
    assert analyse("gh pr create --title t --body \"${LLM_TEXT:?}\n\nfooter line\"", "/x") is not None


def test_the_rules_version_is_bumped_so_installed_rules_refresh(tmp_path, monkeypatch):
    assert ih._rules_version(RULES) >= 12
    claude = tmp_path / ".claude"
    (claude / "rules").mkdir(parents=True)
    monkeypatch.setattr(ih, "_CLAUDE_DIR", claude)
    monkeypatch.setattr(ih, "_RULES_DST", claude / "rules")
    monkeypatch.setattr(ih, "_HOOKS_DST", claude / "hooks")
    installed = claude / "rules" / "llm_router.md"
    installed.write_text("<!-- llm_router-rules-version: 11 -->\n# old rules\n")
    msg = ih.check_and_update_rules()
    assert msg and "11" in msg and "12" in msg
    body = installed.read_text(encoding="utf-8")
    assert '"${LLM_TEXT:?}"' in body, "the install-time rewrite of tool names must not touch the placeholder"
    assert ih._rules_version(installed) == ih._rules_version(RULES)


def test_the_hook_files_arrive_with_an_upgrade_but_registration_does_not():
    """The honest limit of the auto-refresh: check_and_update_hooks copies a missing hook file, it never
    registers one. Registration is `llm-router install`, which is why the rules and the hook can
    arrive out of step, and why the placeholder is a variable that aborts the shell when nothing replaced it."""
    import inspect

    src = inspect.getsource(ih.check_and_update_hooks)
    assert "_register_hook" not in src and "Restored missing" in src
