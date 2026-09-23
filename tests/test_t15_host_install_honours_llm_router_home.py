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

import os
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
    # `_CLAUDE_JSON_PATH` is deliberately NOT here. conftest's autouse
    # `_redirect_claude_json` monkeypatches that name for every test, and
    # `_override()` honours a patch above everything else — correctly — so this
    # parametrisation can never observe the underlying resolution. It is tested
    # through the resolver function instead, below.
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


# ── The two writers T-15 missed ───────────────────────────────────────────


def test_the_sibling_writers_honour_the_sandbox(monkeypatch, tmp_path):
    """`~/.claude.json` and the Claude Desktop config are host config too.

    T-15 made four names resolve at access time and missed both of these,
    because one is a SIBLING of `~/.claude` rather than a child and the other
    lives under `~/Library/Application Support/`. Neither is shaped like
    `~/.claude`, so the sweep that found the others walked past them, and an
    install with `LLM_ROUTER_HOME` set still wrote the operator's real files.

    Tested through the resolver functions rather than the module attributes:
    conftest's autouse `_redirect_claude_json` patches `_CLAUDE_JSON_PATH`, and
    a patch correctly wins over everything, which would mask the resolution.
    """
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_DIR", raising=False)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "sandbox"))
    monkeypatch.setattr(ih, "_CLAUDE_JSON_PATH", ih._LazyHostPath(ih.claude_json_path))

    assert str(ih.claude_json_path()).startswith(str(tmp_path))
    assert ih.claude_json_path().name == ".claude.json"

    desktop = ih.claude_desktop_config_path()
    assert desktop is not None
    assert str(desktop).startswith(str(tmp_path)), (
        f"the Claude Desktop config escaped the sandbox: {desktop}"
    )


def test_the_real_paths_are_used_when_nothing_is_set(monkeypatch):
    """Anti-vacuity: the fix must not permanently redirect a normal install."""
    monkeypatch.delenv("LLM_ROUTER_HOME", raising=False)
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_DIR", raising=False)
    monkeypatch.setattr(ih, "_CLAUDE_JSON_PATH", ih._LazyHostPath(ih.claude_json_path))

    assert ih.claude_json_path() == pathlib.Path.home() / ".claude.json"
    desktop = ih.claude_desktop_config_path()
    assert desktop is not None and str(desktop).startswith(str(pathlib.Path.home()))


def test_the_module_constant_itself_resolves_late(tmp_path):
    """`_CLAUDE_JSON_PATH`, as READ FROM THE MODULE, must honour the sandbox.

    Run in a subprocess on purpose. conftest's autouse `_redirect_claude_json`
    monkeypatches this exact name for every test in this suite, and `_override()`
    honours a patch above all else — correctly — so in-process the attribute can
    only ever show the patch. A child interpreter has no conftest, which is the
    only place the real binding is observable.

    Without this, reverting the constant to `Path.home() / ".claude.json"` left
    every test green while a real `LLM_ROUTER_HOME=... llm-router install` wrote
    the operator's actual file.
    """
    import subprocess
    import sys

    sandbox = tmp_path / "sandbox"
    code = (
        "from llm_router import install_hooks as ih; print(ih._CLAUDE_JSON_PATH)"
    )
    env = {**os.environ, "LLM_ROUTER_HOME": str(sandbox)}
    env.pop("LLM_ROUTER_CLAUDE_DIR", None)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, cwd=str(pathlib.Path(ih.__file__).parents[2]))
    assert r.returncode == 0, r.stderr[:400]
    resolved = r.stdout.strip()
    assert resolved.startswith(str(sandbox)), (
        f"_CLAUDE_JSON_PATH resolved to {resolved!r}, outside the sandbox "
        f"{str(sandbox)!r} — it froze at import again"
    )


def test_a_sandboxed_install_does_not_shell_out_to_the_claude_binary():
    """`claude mcp add --scope user` writes ITS OWN ~/.claude.json from the
    subprocess's HOME, which no path constant on this side can redirect.

    This was the actual leak: with every host path correctly sandboxed,
    `install()` still produced `.claude.json` in the operator's home because a
    child process put it there. Asserted on the AST so a comment carrying the
    phrase cannot satisfy it.
    """
    import ast

    src = pathlib.Path(ih.__file__).read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_install_claude_code_cli"
    )
    guarded = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_sandbox_claude_dir"
    ]
    assert guarded, (
        "_install_claude_code_cli no longer consults _sandbox_claude_dir(), so a "
        "sandboxed install can shell out to the real `claude` binary again"
    )
