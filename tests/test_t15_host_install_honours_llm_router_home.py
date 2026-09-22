"""`install` must not write to the operator's real `~/.claude` — T-15.

`_CLAUDE_DIR = Path.home() / ".claude"` and its three derived constants were
evaluated at IMPORT time — the same import-time path binding the repo-wide T00b
sweep removed everywhere else. It survived because that sweep targeted
`~/.llm-router` STATE paths, and this is host CONFIG.

Measured during the audit: `llm-router update` wrote 15 hook files, a rules file
and a statusline script into the real `~/.claude/` while `LLM_ROUTER_HOME`
pointed at a tmp directory. Content was byte-identical, so nothing broke
loudly — but any isolated test or CI job running `install` / `update` /
`dev-refresh` touches the operator's real config, and that is what flipped three
config tests during the audit.
"""

from __future__ import annotations

import pathlib

import pytest

from llm_router import install_hooks as ih


def test_the_real_home_is_used_when_nothing_is_set(monkeypatch):
    """Anti-vacuity: the fix must not permanently redirect a normal install."""
    monkeypatch.delenv("LLM_ROUTER_HOME", raising=False)
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_DIR", raising=False)
    assert ih.claude_dir() == pathlib.Path.home() / ".claude"


def test_llm_router_home_redirects_the_host_config(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_DIR", raising=False)
    assert str(ih.claude_dir()).startswith(str(tmp_path))


def test_an_explicit_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_DIR", str(tmp_path / "explicit"))
    assert ih.claude_dir() == tmp_path / "explicit"


@pytest.mark.parametrize("name, suffix", [
    ("_CLAUDE_DIR", ""),
    ("_HOOKS_DST", "hooks"),
    ("_RULES_DST", "rules"),
    ("_SETTINGS_PATH", "settings.json"),
])
def test_every_exported_constant_resolves_at_access_time(
    monkeypatch, tmp_path, name, suffix
):
    """The defect was the TIMING, not the base.

    All four were module-level constants, so the value froze at import and no
    later environment change could move it. ~118 call sites read these names,
    so they keep working — they just re-resolve on every read now.
    """
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_DIR", raising=False)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "a"))
    # Snapshot the STRING at each point. The exported name is a lazy proxy that
    # re-resolves on use, so holding the object and comparing it to itself would
    # compare two fresh resolutions and always match — which is the proxy
    # working, not the bug.
    first = str(getattr(ih, name))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "b"))
    second = str(getattr(ih, name))

    assert first != second, f"{name} froze at import — it cannot be redirected"
    assert first.startswith(str(tmp_path / "a"))
    assert second.startswith(str(tmp_path / "b"))
    if suffix:
        assert second.endswith(suffix)


def test_install_writes_into_the_sandbox_and_nowhere_else(monkeypatch, tmp_path, importing_a_submodule):
    """The gate, end to end: run the real installer, then look at the real config.

    `~/.claude/projects`, `todos`, `shell-snapshots` and `statsig` are Claude
    Code's OWN live session state, written continuously by the running IDE. They
    are excluded because a change there is not evidence about this installer —
    including them made this check fail on the transcript of the very session
    running it.
    """
    import hashlib

    skip = ("/projects/", "/todos/", "/shell-snapshots/", "/statsig/")
    real = pathlib.Path.home() / ".claude"

    def snapshot() -> dict[str, str]:
        out: dict[str, str] = {}
        if not real.exists():
            return out
        for f in real.rglob("*"):
            p = str(f)
            if not f.is_file() or any(s in p for s in skip):
                continue
            try:
                out[p] = hashlib.sha256(f.read_bytes()).hexdigest()
            except OSError:
                pass
        return out

    before = snapshot()
    if not before:
        pytest.skip("no real ~/.claude config on this machine to protect")

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_DIR", raising=False)
    actions = ih.install()

    after = snapshot()
    changed = [k for k in after if before.get(k) != after[k]]
    removed = [k for k in before if k not in after]

    sandbox_files = [f for f in (tmp_path / "claude-home").rglob("*") if f.is_file()]

    # Vacuity guard first: if the installer did nothing, "nothing changed" is
    # not evidence of anything.
    assert actions, "install() performed no actions — the check below is vacuous"
    assert sandbox_files, "install() wrote nothing to the sandbox either"

    assert not changed, f"install() modified the real ~/.claude: {changed[:5]}"
    assert not removed, f"install() removed files from the real ~/.claude: {removed[:5]}"
