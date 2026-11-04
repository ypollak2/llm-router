"""W7 text-job-bind: what the shipped rules teach is what the hook supports, and it reaches installs.

The rules file is loaded into every session, sub-agents included, and is the only thing that
makes a model write the placeholder. If it taught a shape the hook refuses, or if an upgrade did
not carry it, the feature would be dead on arrival. So each example line in the section is run
through the real planner, each "not supported" claim is checked against it, and the refresh the
MCP server does at start-up is exercised.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import llm_router.install_hooks as ih
from llm_router import eligibility_routing as er
from llm_router.text_job_bind import PLACEHOLDER
from llm_router.text_job_bind.plan import JOB_COMMIT, JOB_PR, Unsupported, analyse

RULES = Path(ih.__file__).parent / "rules" / "llm_router.md"
T = PLACEHOLDER
NAME = "llm_router-text-job-bind.py"
BEGIN = "<!-- llm_router-text-job-bind:begin -->"
END = "<!-- llm_router-text-job-bind:end -->"


REAL_LANE_CHECK = er.passed_lane_available


@pytest.fixture(autouse=True)
def lane(monkeypatch):
    """The router's lane check, stubbed: a passed commit_message lane exists unless a test says otherwise.
    ``raises`` makes it blow up, ``calls`` records what it was asked."""
    state = SimpleNamespace(available=True, raises=False, calls=[])

    def fake(job, input_len, now, **kw):
        state.calls.append((job, input_len, now))
        if state.raises:
            raise RuntimeError("the lane check failed")
        return state.available

    monkeypatch.setattr(er, "passed_lane_available", fake)
    return state


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A scratch Claude config dir (rules, hooks, settings) with the state dir beside it."""
    claude = tmp_path / ".claude"
    (claude / "rules").mkdir(parents=True)
    (claude / "hooks").mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_DIR", raising=False)
    monkeypatch.setattr(ih, "_CLAUDE_DIR", claude)
    monkeypatch.setattr(ih, "_RULES_DST", claude / "rules")
    monkeypatch.setattr(ih, "_HOOKS_DST", claude / "hooks")
    monkeypatch.setattr(ih, "_SETTINGS_PATH", claude / "settings.json")
    return claude


def register(claude: Path, command: str | None = None, event: str = "PreToolUse") -> None:
    command = command or f"/usr/bin/python3 {claude}/hooks/{NAME}"
    (claude / "settings.json").write_text(json.dumps({
        "hooks": {
            "PreToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": "/usr/bin/python3 /x/llm_router-enforce-route.py"}]}],
            event: [{"matcher": "Bash", "hooks": [{"type": "command", "command": command, "timeout": 130}]}],
        }
    }))


def unregister(claude: Path) -> None:
    (claude / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": []}}))


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {str(f.relative_to(root)): (f.read_bytes(), f.stat().st_mtime_ns) for f in sorted(root.rglob("*")) if f.is_file()}


def section() -> str:
    text = RULES.read_text(encoding="utf-8")
    start = text.index("## Commit messages and PR bodies")
    return text[start:text.index("\n## ", start + 5)].replace(END, "")


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


def test_the_rules_version_is_bumped_so_installed_rules_refresh(home):
    assert ih._rules_version(RULES) >= 12
    register(home)
    installed = home / "rules" / "llm_router.md"
    installed.write_text("<!-- llm_router-rules-version: 11 -->\n# old rules\n")
    msg = ih.check_and_update_rules()
    assert msg and "11" in msg and "12" in msg
    body = installed.read_text(encoding="utf-8")
    assert '"${LLM_TEXT:?}"' in body, "the install-time rewrite of tool names must not touch the placeholder"
    assert ih._rules_version(installed) == ih._rules_version(RULES)


def test_the_hook_files_arrive_with_an_upgrade_but_registration_does_not(tmp_path, monkeypatch):
    """The honest limit of the auto-refresh, shown by running it: `check_and_update_hooks` copies a missing
    hook file next to the others, it never writes a registration. Registration is `llm-router install`, which
    is why the rules and the hook can arrive out of step, and why the placeholder is a variable that aborts
    the shell when nothing replaced it."""
    import json

    claude = tmp_path / ".claude"
    (claude / "hooks").mkdir(parents=True)
    monkeypatch.setattr(ih, "_CLAUDE_DIR", claude)
    monkeypatch.setattr(ih, "_HOOKS_DST", claude / "hooks")
    monkeypatch.setattr(ih, "_RULES_DST", claude / "rules")
    monkeypatch.setattr(ih, "_SETTINGS_PATH", claude / "settings.json")
    (claude / "settings.json").write_text(json.dumps({"hooks": {}}))
    updates = ih.check_and_update_hooks()
    assert any("llm_router-text-job-bind.py" in u and "Restored missing" in u for u in updates), updates
    assert (claude / "hooks" / "llm_router-text-job-bind.py").is_file()
    assert json.loads((claude / "settings.json").read_text()) == {"hooks": {}}, "an upgrade must not register hooks"


# ---- the placeholder section is installed only while the hook is registered (F13) -----------------------

def rendered() -> str:
    return ih._localized_rules_text(RULES)


def test_the_template_wraps_the_section_and_its_pointer_in_balanced_markers():
    text = RULES.read_text(encoding="utf-8")
    assert text.count(BEGIN) == text.count(END) == 2
    regions = re.findall(re.escape(BEGIN) + r"\n(.*?)" + re.escape(END), text, re.S)
    assert len(regions) == 2
    assert regions[0].startswith("## Commit messages and PR bodies") and T in regions[0]
    assert "the placeholder above" in regions[1], "the pointer to the section goes with the section"
    outside = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), "", text, flags=re.S)
    assert "LLM_TEXT" not in outside and "placeholder above" not in outside


def test_the_section_is_absent_while_the_hook_is_not_registered(home):
    for setup in (lambda: None, lambda: unregister(home), lambda: (home / "settings.json").write_text("{not json")):
        setup()
        text = rendered()
        assert T not in text and "LLM_TEXT" not in text
        assert "## Commit messages and PR bodies" not in text and "placeholder above" not in text
        assert BEGIN not in text and END not in text, "markers are never shown to the model"
        assert "## Self-contained text jobs" in text and "llm_text_job" in text, "the fallback teaching stays"
        assert "\n\n\n" not in text, "removing the section leaves no extra blank line"
        assert text.splitlines()[0] == RULES.read_text(encoding="utf-8").splitlines()[0]
        assert ih._rules_version(RULES) == int(re.match(r"<!-- llm_router-rules-version: (\d+) -->", text).group(1))


def test_the_section_is_present_while_the_hook_is_registered(home):
    register(home)
    text = rendered()
    assert T in text and "## Commit messages and PR bodies" in text and "placeholder above" in text
    assert BEGIN not in text and END not in text, "markers are never shown to the model"
    assert "\n\n\n" not in text
    assert text.index("## Commit messages and PR bodies") < text.index("## Self-contained text jobs")
    # the registered rendering is the template with only its marker lines removed
    assert text == ih.localize(RULES.read_text(encoding="utf-8").replace(BEGIN + "\n", "").replace(END + "\n", ""))


@pytest.mark.parametrize("command,event,registered", [
    ("/usr/bin/python3 /h/.claude/hooks/llm_router-text-job-bind.py", "PreToolUse", True),
    ("python3 '/h/my dir/hooks/llm_router-text-job-bind.py'", "PreToolUse", True),
    ("/h/dist/llm-router run-hook /h/.claude/hooks/llm_router-text-job-bind.py", "PreToolUse", True),
    ("/usr/bin/python3 /h/.claude/hooks/llm_router-text-job-bind.py", "PostToolUse", False),
    ("/usr/bin/python3 /h/.claude/hooks/llm_router-text-job-bind.py.bak", "PreToolUse", False),
    ("/usr/bin/python3 /h/.claude/hooks/llm_router-text-job-bind.py --check", "PreToolUse", False),
    ("/usr/bin/python3 /h/.claude/hooks/llm_router-enforce-route.py", "PreToolUse", False),
])
def test_registration_means_a_pretooluse_handler_whose_command_ends_with_the_hook_name(home, command, event, registered):
    register(home, command, event)
    assert ih.text_job_bind_registered() is registered
    assert (T in rendered()) is registered


@pytest.mark.parametrize("settings", [[], {"hooks": []}, {"hooks": {"PreToolUse": "x"}}, {"hooks": {"PreToolUse": [None, 3, {"hooks": None}, {"hooks": [None, {"command": 5}]}]}}])
def test_a_settings_file_of_the_wrong_shape_is_simply_not_registered(home, settings):
    (home / "settings.json").write_text(json.dumps(settings))
    assert ih.text_job_bind_registered() is False
    assert T not in rendered()


def test_registration_is_read_where_the_installer_writes_settings(tmp_path, monkeypatch):
    """The installer's own resolution: LLM_ROUTER_CLAUDE_DIR wins, a registration in ~/.claude does not count
    when the installer would write elsewhere."""
    resolvers = {"_CLAUDE_DIR": ih.claude_dir, "_HOOKS_DST": ih.hooks_dst, "_RULES_DST": ih.rules_dir, "_SETTINGS_PATH": ih.settings_path}
    for name, resolve in resolvers.items():  # the module's own lazy resolution, not a patched path
        monkeypatch.setattr(ih, name, ih._LazyHostPath(resolve))
    real_home, cfg = tmp_path / "home", tmp_path / "cfg"
    for d in (real_home / ".claude", cfg):
        d.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.delenv("LLM_ROUTER_HOME", raising=False)
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_DIR", str(cfg))
    register(real_home / ".claude")
    assert ih.settings_path() == cfg / "settings.json"
    assert ih.text_job_bind_registered() is False
    register(cfg)
    assert ih.text_job_bind_registered() is True


def test_rendering_never_writes_anything(home, tmp_path):
    for setup in (lambda: None, lambda: register(home), lambda: unregister(home),
                  lambda: (home / "settings.json").write_text("{not json")):
        setup()
        (home / "rules" / "llm_router.md").write_text("<!-- llm_router-rules-version: 12 -->\nold\n")
        before = snapshot(tmp_path)
        rendered()
        ih.text_job_bind_registered()
        ih.text_job_bind_rules_gap()
        ih._rules_content_differs(RULES, home / "rules" / "llm_router.md")
        assert snapshot(tmp_path) == before, "rendering and the checks around it are read-only"


def test_a_template_without_markers_is_rendered_without_looking_at_settings(home, monkeypatch):
    """Every other host's rules: no section to decide, so settings.json is not even read."""
    def boom():
        raise AssertionError("settings were read")

    monkeypatch.setattr(ih, "_load_settings", boom)
    other = ih._RULES_SRC / "codex-rules.md"
    assert BEGIN not in other.read_text(encoding="utf-8")
    assert ih._localized_rules_text(other) == ih.localize(other.read_text(encoding="utf-8"))


def test_registering_the_hook_adds_the_section_at_the_next_refresh_and_removing_it_takes_it_out(home):
    installed = home / "rules" / "llm_router.md"
    first = ih.check_and_update_rules()  # nothing installed yet, hook not registered
    assert first and T not in installed.read_text(encoding="utf-8")
    assert ih.check_and_update_rules() is None, "stable while nothing changes"

    register(home)
    msg = ih.check_and_update_rules()  # same rules version, different rendering
    assert msg and "added" in msg and "registered" in msg, msg
    assert T in installed.read_text(encoding="utf-8")
    assert ih._rules_version(installed) == ih._rules_version(RULES), "the version marker is unchanged"
    assert ih.check_and_update_rules() is None

    unregister(home)
    msg = ih.check_and_update_rules()
    assert msg and "removed" in msg, msg
    body = installed.read_text(encoding="utf-8")
    assert T not in body and "## Commit messages and PR bodies" not in body
    assert ih.check_and_update_rules() is None

    register(home)
    assert ih.check_and_update_rules() and T in installed.read_text(encoding="utf-8")


def test_the_flip_keeps_the_previous_file_and_never_downgrades(home):
    installed = home / "rules" / "llm_router.md"
    register(home)
    ih.check_and_update_rules()
    full = installed.read_text(encoding="utf-8")
    unregister(home)
    msg = ih.check_and_update_rules()
    assert "previous saved to" in msg
    saved = [*(home / "rules").glob("llm_router.md.bak"), *ih._state_dir().rglob("llm_router.md.*.bak")]
    assert any(full == b.read_text(encoding="utf-8") for b in saved), "the removed text is recoverable"
    # a NEWER installed version is never replaced, whatever the registration says
    installed.write_text("<!-- llm_router-rules-version: 99 -->\nfuture\n")
    register(home)
    assert ih.check_and_update_rules() is None
    assert installed.read_text(encoding="utf-8").endswith("future\n")


def test_an_older_full_text_installed_before_this_change_is_refreshed_when_the_hook_is_missing(home):
    """What a pip upgrade left behind: the v12 text with the section, no markers, and no registration."""
    installed = home / "rules" / "llm_router.md"
    installed.write_text(RULES.read_text(encoding="utf-8").replace(BEGIN + "\n", "").replace(END + "\n", ""))
    gap = ih.text_job_bind_rules_gap()
    assert gap and gap.level == "danger"
    msg = ih.check_and_update_rules()
    assert msg and "removed" in msg
    assert T not in installed.read_text(encoding="utf-8")
    assert ih.text_job_bind_rules_gap() is None


# ---- ... and only while a passed commit_message lane exists (S9) -----------------------------------------

@pytest.mark.parametrize("is_registered,has_lane,section", [
    (True, True, True), (True, False, False), (False, True, False), (False, False, False),
])
def test_the_section_needs_the_hook_registered_and_a_passed_lane(home, lane, is_registered, has_lane, section):
    if is_registered:
        register(home)
    lane.available = has_lane
    text = rendered()
    assert (T in text) is section
    assert ("## Commit messages and PR bodies" in text) is section and ("placeholder above" in text) is section
    assert "## Self-contained text jobs" in text and "llm_text_job" in text, "the plain guidance is always there"
    assert BEGIN not in text and END not in text


def test_the_lane_is_asked_for_commit_message_with_a_typical_input_and_only_once_the_hook_is_registered(home, lane):
    rendered()
    assert lane.calls == [], "an unregistered hook decides it: no lane lookup at all"
    register(home)
    rendered()
    ((job, input_len, now),) = lane.calls
    assert job == "commit_message" and input_len == 8000 and isinstance(now, float)


def test_a_lane_check_that_raises_means_plain_guidance_never_an_exception(home, lane):
    register(home)
    lane.raises = True
    text = rendered()
    assert T not in text and "## Self-contained text jobs" in text
    assert ih.text_job_bind_lane_available() is False
    assert ih.text_job_bind_section_wanted() is False
    (home / "rules" / "llm_router.md").write_text("<!-- llm_router-rules-version: 12 -->\nold\n")
    assert ih.check_and_update_rules() is not None  # refresh still works, without the section
    assert T not in (home / "rules" / "llm_router.md").read_text(encoding="utf-8")
    assert ih.text_job_bind_rules_gap().level == "info"


def test_a_lane_that_appears_or_goes_flips_the_section_at_the_next_refresh_in_both_directions(home, lane):
    installed = home / "rules" / "llm_router.md"
    register(home)
    lane.available = False
    assert ih.check_and_update_rules() and T not in installed.read_text(encoding="utf-8")  # registered, no lane: plain

    lane.available = True
    msg = ih.check_and_update_rules()
    assert msg and "added" in msg and "lane" in msg, msg
    assert T in installed.read_text(encoding="utf-8")

    lane.available = False
    msg = ih.check_and_update_rules()
    assert msg and "removed" in msg and "lane" in msg, msg
    body = installed.read_text(encoding="utf-8")
    assert T not in body and "## Commit messages and PR bodies" not in body

    lane.available = True
    assert ih.check_and_update_rules() and T in installed.read_text(encoding="utf-8")


@pytest.mark.parametrize("registered_,lane_", [(True, True), (True, False), (False, True), (False, False)])
def test_the_same_state_never_flip_flops(home, lane, registered_, lane_):
    """Several refreshes in a row with nothing changed: the first may write, every later one is None."""
    if registered_:
        register(home)
    lane.available = lane_
    ih.check_and_update_rules()
    installed = home / "rules" / "llm_router.md"
    first = installed.read_text(encoding="utf-8")
    mtime = installed.stat().st_mtime_ns
    for _ in range(5):
        assert ih.check_and_update_rules() is None
    assert installed.read_text(encoding="utf-8") == first and installed.stat().st_mtime_ns == mtime
    assert (T in first) is (registered_ and lane_)


def test_a_failing_lane_check_does_not_flip_flop_either(home, lane):
    register(home)
    lane.raises = True
    ih.check_and_update_rules()
    assert all(ih.check_and_update_rules() is None for _ in range(3))


def test_rendering_with_every_lane_state_never_writes(home, lane, tmp_path):
    register(home)
    (home / "rules" / "llm_router.md").write_text("<!-- llm_router-rules-version: 12 -->\nold\n")
    for available, raises in ((True, False), (False, False), (True, True)):
        lane.available, lane.raises = available, raises
        before = snapshot(tmp_path)
        rendered()
        ih.text_job_bind_section_wanted()
        ih.text_job_bind_rules_gap()
        assert snapshot(tmp_path) == before


def test_the_routers_own_lane_check_end_to_end_on_real_lane_files(home, lane, tmp_path, monkeypatch):
    """No stub on the lane check: a real passed, enabled, digest-verified lane in a tmp state dir."""
    from tests.test_lc8_routing import BARE, CTX, DIGEST, JOB, NOW, lane_file
    from llm_router.local_check import display, routing as lr

    assert JOB == "commit_message"
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setattr(er, "passed_lane_available", REAL_LANE_CHECK)
    monkeypatch.setattr(display, "inventory", lambda: ([BARE], {BARE: DIGEST}))
    monkeypatch.setattr(er, "_route_time_ctx", lambda m: CTX)
    monkeypatch.setattr(er, "_route_time_think", lambda: False)
    monkeypatch.setattr(ih, "time", SimpleNamespace(time=lambda: NOW, strftime=ih.time.strftime))
    er.reset_cache()
    register(home)
    installed = home / "rules" / "llm_router.md"

    assert ih.text_job_bind_lane_available() is False, "nothing enabled, nothing passed"
    assert ih.check_and_update_rules() and T not in installed.read_text(encoding="utf-8")

    lane_file(state)
    lr.enable(BARE, JOB, "local_check")
    before = snapshot(tmp_path)
    assert ih.text_job_bind_lane_available() is True
    assert T in rendered()
    assert snapshot(tmp_path) == before, "the real lane check reads files only"
    msg = ih.check_and_update_rules()
    assert msg and "added" in msg and T in installed.read_text(encoding="utf-8")
    assert ih.check_and_update_rules() is None

    lr.disable(BARE, JOB, "manual")
    msg = ih.check_and_update_rules()
    assert msg and "removed" in msg and T not in installed.read_text(encoding="utf-8")
    assert ih.check_and_update_rules() is None
    er.reset_cache()
