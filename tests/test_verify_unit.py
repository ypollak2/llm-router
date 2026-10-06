"""verify_unit (PR A): candidate selection + the fail-to-pass gate, on the REAL sandbox.

Nothing here mocks the sandbox or pytest: every run is a real sandboxed pytest on a throwaway
copy of a small fixture repo. The only patch of the sandbox is the one test that has to make
the proof come back "not proven" (a Mac cannot be made un-provable any other way).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from llm_router.toolkit import sandbox
from llm_router.toolkit import verify_unit as VU
from llm_router.toolkit.verify_unit import verify_unit
from tests.test_toolkit_verifier_integrity import ATTACKS
from tests.toolkit_fixtures import digest, make_source

SANDBOX_OK = sandbox.prove_sandbox().proven
real_sandbox = pytest.mark.skipif(not SANDBOX_OK, reason="sandbox not proven: the verifier does not run")
pytestmark = pytest.mark.timeout(240)

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
FIELDS = {"verify_status", "reason", "n_candidates", "n_f2p", "ms", "sandboxed", "flags"}


def _git(root: Path, *args: str) -> str:
    return subprocess.run([*GIT, *args], cwd=root, check=True, capture_output=True, text=True).stdout


def _init(root: Path) -> Path:
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _patch(repo: Path, tmp_path: Path, mutate) -> str:
    """Apply `mutate(clone_root)` to a clone and return the diff; `repo` is never touched."""
    clone = tmp_path / "clone"
    shutil.rmtree(clone, ignore_errors=True)
    shutil.copytree(repo, clone)
    mutate(clone)
    _git(clone, "add", "-A")
    return _git(clone, "diff", "--cached", "HEAD")


FIXED = "def add(a, b):\n    return a + b\n"


def _fix(r: Path) -> None:
    (r / "src" / "pkg.py").write_text(FIXED)


@pytest.fixture
def broken(tmp_path):
    """make_source: add() is wrong, tests/test_pkg.py::test_add fails, test_add_zero passes."""
    return _init(make_source(tmp_path / "broken"))


@pytest.fixture
def green(tmp_path):
    """Every test passes on the baseline."""
    root = make_source(tmp_path / "green")
    (root / "src" / "pkg.py").write_text(FIXED)
    return _init(root)


# ── the gate ─────────────────────────────────────────────────────────────────


@real_sandbox
def test_a_genuine_fix_is_f2p_and_used_eligible(broken, tmp_path):
    patch = _patch(broken, tmp_path, _fix)
    before = digest(broken)
    r = verify_unit(broken, patch)
    assert (r.verify_status, r.reason) == ("pass_f2p", "f2p"), r
    assert r.n_f2p == 1 and r.n_candidates == 1 and r.sandboxed and r.ms > 0
    assert r.used_eligible
    assert digest(broken) == before, "the user's tree was touched"
    assert set(r.as_dict()) == FIELDS          # no tail, no command, nothing else is persisted


@real_sandbox
def test_a_weak_p2p_change_is_never_used_eligible(green, tmp_path):
    patch = _patch(green, tmp_path, lambda r: (r / "src" / "pkg.py").write_text("def add(a, b):\n    return b + a\n"))
    r = verify_unit(green, patch)
    assert (r.verify_status, r.reason, r.n_f2p) == ("pass_p2p", "no_f2p", 0), r
    assert r.sandboxed and not r.used_eligible


@real_sandbox
def test_a_model_test_that_passes_on_the_baseline_is_not_counted(green, tmp_path):
    def mutate(r: Path):
        (r / "src" / "pkg.py").write_text("def add(a, b):\n    return b + a\n")
        (r / "tests" / "test_new.py").write_text("from pkg import add\n\n\ndef test_commutes():\n    assert add(1, 2) == add(2, 1)\n")
    r = verify_unit(green, _patch(green, tmp_path, mutate))
    assert r.verify_status == "pass_p2p" and r.n_f2p == 0, r
    assert {"model_tests_added", "model_test_passes_on_baseline"} <= set(r.flags)
    assert not r.used_eligible


@real_sandbox
def test_a_model_test_that_fails_on_the_baseline_counts_but_is_never_the_sole_evidence(tmp_path):
    root = tmp_path / "greet"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "greet.py").write_text('def greet(n):\n    return "hi"\n')
    (root / "tests" / "test_greet.py").write_text('from greet import greet\n\n\ndef test_starts():\n    assert greet("a").startswith("hi")\n')
    _init(root)

    def mutate(r: Path):
        (r / "src" / "greet.py").write_text('def greet(n):\n    return "hi " + n\n')
        (r / "tests" / "test_greet_name.py").write_text('from greet import greet\n\n\ndef test_name():\n    assert greet("a") == "hi a"\n')
    r = verify_unit(root, _patch(root, tmp_path, mutate))
    assert r.verify_status == "pass_p2p" and r.n_f2p == 1, r
    assert "f2p_model_tests_only" in r.flags and not r.used_eligible


@real_sandbox
def test_model_tests_that_fail_on_baseline_add_to_the_count_next_to_a_real_f2p(broken, tmp_path):
    def mutate(r: Path):
        _fix(r)
        (r / "tests" / "test_more.py").write_text("from pkg import add\n\n\ndef test_two():\n    assert add(2, 2) == 4\n")
    r = verify_unit(broken, _patch(broken, tmp_path, mutate))
    assert r.verify_status == "pass_f2p" and r.n_f2p == 2, r


@real_sandbox
def test_a_patch_that_breaks_another_test_fails(green, tmp_path):
    r = verify_unit(green, _patch(green, tmp_path, lambda r: (r / "src" / "pkg.py").write_text("def add(a, b):\n    return a - b\n")))
    assert r.verify_status == "fail" and not r.used_eligible, r
    assert r.reason in ("new_failures", "tests_fail")


@real_sandbox
@pytest.mark.parametrize("name", list(ATTACKS), ids=list(ATTACKS))
@pytest.mark.parametrize("with_fix", [False, True], ids=["attack-only", "fix-plus-attack"])
def test_harness_tampering_is_refused(broken, tmp_path, name, with_fix):
    write = ATTACKS[name][0]

    def mutate(r: Path):
        if with_fix:
            _fix(r)
        write(r)
    r = verify_unit(broken, _patch(broken, tmp_path, mutate))
    assert r.verify_status == "fail" and r.reason == "harness_tampered", (name, r)
    assert not r.used_eligible


# ── fail closed ──────────────────────────────────────────────────────────────


def test_unproven_sandbox_is_unavailable_never_pass(broken, tmp_path, monkeypatch):
    patch = _patch(broken, tmp_path, _fix)
    monkeypatch.setattr(sandbox, "prove_sandbox", lambda **kw: sandbox.SandboxStatus(False, "not proven in this test"))
    r = verify_unit(broken, patch)
    assert (r.verify_status, r.reason, r.sandboxed) == ("unavailable", "sandbox_unproven", False), r
    assert not r.used_eligible


def test_kill_switch_is_unavailable(broken, tmp_path, monkeypatch):
    patch = _patch(broken, tmp_path, _fix)
    monkeypatch.setenv("LLM_ROUTER_TOOLLAYER", "off")
    r = verify_unit(broken, patch)
    assert (r.verify_status, r.reason) == ("unavailable", "kill_switch"), r


@real_sandbox
def test_an_apply_conflict_is_unavailable(broken, tmp_path):
    patch = _patch(broken, tmp_path, _fix)
    (broken / "src" / "pkg.py").write_text("def add(a, b):\n    return a * b  # moved on\n")   # HEAD moved under the patch
    before = digest(broken)
    r = verify_unit(broken, patch)
    assert (r.verify_status, r.reason) == ("unavailable", "apply_conflict"), r
    assert digest(broken) == before


@real_sandbox
def test_budget_exhausted_is_unavailable_timeout(broken, tmp_path):
    r = verify_unit(broken, _patch(broken, tmp_path, _fix), budget_s=0.5)
    assert (r.verify_status, r.reason) == ("unavailable", "timeout"), r


def test_a_non_pytest_repo_is_not_applicable(tmp_path):
    root = tmp_path / "js"
    root.mkdir()
    (root / "package.json").write_text('{"scripts": {"test": "node test.js"}}\n')
    (root / "index.js").write_text("exports.add = (a, b) => a - b;\n")
    (root / "test.js").write_text("require('./index');\n")
    _init(root)
    r = verify_unit(root, _patch(root, tmp_path, lambda c: (c / "index.js").write_text("exports.add = (a, b) => a + b;\n")))
    assert (r.verify_status, r.reason) == ("not_applicable", "no_runner"), r
    assert not r.used_eligible


def test_empty_patch_and_untested_source_are_not_applicable(broken, tmp_path):
    assert verify_unit(broken, "").reason == "empty_patch"
    if SANDBOX_OK:
        patch = _patch(broken, tmp_path, lambda r: (r / "src" / "orphan.py").write_text("X = 1\n"))
        r = verify_unit(broken, patch)
        assert (r.verify_status, r.reason) == ("not_applicable", "no_candidates"), r


# ── candidate selection (no sandbox needed) ──────────────────────────────────


def _fixture_repo(root: Path) -> Path:
    files = {
        "src/pkg/core.py": "X = 1\n",
        "src/pkg/util.py": "Y = 1\n",
        "src/pkg/test_local.py": "def test_local():\n    pass\n",
        "tests/test_imports_core.py": "from pkg.core import X\n",
        "tests/test_imports_pkg_core.py": "from pkg import core\n",
        "tests/test_imports_util.py": "import pkg.util\n",
        "tests/test_core.py": "def test_by_name():\n    pass\n",
        "tests/test_unrelated.py": "import os\n",
        "tests/test_broken_syntax.py": "def (\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def test_selection_picks_importers_then_name_then_neighbours(tmp_path):
    root = _fixture_repo(tmp_path / "fx")
    got, capped = VU.select_candidates(root, ["src/pkg/core.py"])
    assert got == ["tests/test_imports_core.py", "tests/test_imports_pkg_core.py",
                   "tests/test_core.py", "src/pkg/test_local.py"]
    assert not capped
    assert VU.select_candidates(root, ["src/pkg/util.py"])[0] == ["tests/test_imports_util.py", "src/pkg/test_local.py"]


def test_selection_is_capped_and_skips_model_changed_tests(tmp_path):
    root = _fixture_repo(tmp_path / "fx")
    got, capped = VU.select_candidates(root, ["src/pkg/core.py"], cap=2)
    assert got == ["tests/test_imports_core.py", "tests/test_imports_pkg_core.py"] and capped
    got, _ = VU.select_candidates(root, ["src/pkg/core.py"], exclude={"tests/test_imports_core.py"})
    assert "tests/test_imports_core.py" not in got
    assert VU.MAX_CANDIDATES == 60
