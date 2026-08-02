"""Tests for the WS0 identity gate (scripts/ci/check_identity.py).

Asserts the gate itself passes against the current repo state, and that two
key runtime/public surfaces never leak the "chuzom" brand name: the CLI
--help output, and the public names defined in llm_router.contracts.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_check_identity_script_passes():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "ci" / "check_identity.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"check_identity.py failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout


def test_cli_help_has_no_brand_leak():
    result = subprocess.run(
        [sys.executable, "-m", "llm_router.cli", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    combined = (result.stdout + result.stderr).lower()
    assert "chuzom" not in combined


def test_contracts_module_has_no_brand_leak_in_public_names():
    from llm_router import contracts

    public_names = [name for name in dir(contracts) if not name.startswith("_")]
    for name in public_names:
        assert "chuzom" not in name.lower()

    # Also check the string *values* of public constants (not just names) —
    # e.g. a stray literal like "chuzom_x" embedded in a value would still
    # be a leak if this module were ever imported by runtime/public code.
    for name in public_names:
        value = getattr(contracts, name)
        if isinstance(value, str):
            assert "chuzom" not in value.lower()
        elif isinstance(value, (tuple, frozenset, list, set)):
            for item in value:
                if isinstance(item, str):
                    assert "chuzom" not in item.lower()
