"""GH#47: the Textual install hint named a package that does not exist.

`commands/dashboard.py` printed:

    Install with: pip install llm_router[tui]

Three independent failures in one line:

  * `llm_router` is not the PyPI name — that is `llm-routing`. Not a
    hyphen/underscore variant, a different word, so even a correctly quoted
    copy-paste installs the wrong thing (or nothing).
  * Unquoted square brackets are a glob pattern in zsh: the command fails
    outright with "no matches found: llm_router[tui]".
  * `llm-routing` is normally pipx-installed into its own venv. A bare `pip
    install` — even with the name fixed — installs into whatever Python is
    active in the shell, not the venv `llm-router` actually runs from, so it
    would not fix anything.

The working command, confirmed by the reporter: `pipx inject llm-routing textual`.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DASHBOARD = _REPO / "src" / "llm_router" / "commands" / "dashboard.py"


def _hint_lines() -> list[str]:
    return [
        ln for ln in _DASHBOARD.read_text().splitlines()
        if "textual" in ln.lower() and ("install" in ln.lower() or "inject" in ln.lower())
    ]


def test_hint_exists():
    assert _hint_lines(), "the missing-Textual hint disappeared entirely"


def test_hint_does_not_name_a_nonexistent_package():
    """The distribution is `llm-routing`; `llm_router` is the import name."""
    for ln in _hint_lines():
        assert "llm_router[" not in ln, (
            f"hint installs a package that does not exist on PyPI: {ln.strip()}"
        )


def test_hint_uses_the_real_distribution_name():
    with (_REPO / "pyproject.toml").open("rb") as fh:
        dist = tomllib.load(fh)["project"]["name"]
    assert dist == "llm-routing", "distribution renamed — update this hint and test"
    assert any(dist in ln for ln in _hint_lines()), (
        f"hint does not name the real distribution ({dist}): {_hint_lines()}"
    )


def test_hint_targets_the_venv_the_command_runs_from():
    """A bare `pip install` misses the pipx venv entirely."""
    joined = " ".join(_hint_lines())
    assert "pipx inject" in joined, (
        f"hint must use `pipx inject`, which installs into the venv `llm-router` "
        f"actually runs from: {_hint_lines()}"
    )


def test_hint_is_safe_to_paste_into_zsh():
    """Unquoted [tui] is a zsh glob: 'no matches found'."""
    for ln in _hint_lines():
        for m in re.finditer(r"\S*\[[a-z]+\]\S*", ln):
            frag = m.group(0)
            assert frag.startswith(("'", '"')) or frag.endswith(("'", '"')), (
                f"unquoted extras bracket breaks in zsh: {frag!r} in {ln.strip()}"
            )


def test_the_extra_being_suggested_actually_exists():
    """Guards the guard: suggesting a nonexistent extra is the same class of bug."""
    with (_REPO / "pyproject.toml").open("rb") as fh:
        extras = tomllib.load(fh)["project"].get("optional-dependencies", {})
    joined = " ".join(_hint_lines())
    if "[tui]" in joined:
        assert "tui" in extras, "hint suggests a [tui] extra that pyproject does not define"
        assert any("textual" in d for d in extras["tui"]), "the tui extra does not provide textual"
