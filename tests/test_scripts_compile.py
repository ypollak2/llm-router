"""REC-004: every script under scripts/ must compile on the project's Python.

Three scripts used Python 3.12-only f-string syntax and could not even be
parsed on the pinned 3.11. An earlier audit (2026-09-22) found them and they
survived 52 commits and two releases, because CI lints src/ and tests/ only.
Compiling is the cheapest check that catches the whole class.
"""
from __future__ import annotations

import pathlib

import pytest

SCRIPTS = sorted((pathlib.Path(__file__).resolve().parents[1] / "scripts").rglob("*.py"))


def test_there_are_scripts_to_check():
    assert len(SCRIPTS) > 20, f"found only {len(SCRIPTS)} scripts — the glob broke"


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: str(p.relative_to(p.parents[1])))
def test_script_compiles(path):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
