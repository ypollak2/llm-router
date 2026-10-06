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


# ── review round: test data, helpers, edited tests, symlinks, known limits ───


@pytest.fixture
def datarepo(tmp_path):
    root = tmp_path / "datarepo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "fixtures").mkdir()
    (root / "src" / "pkg.py").write_text("def val():\n    return 1\n")
    (root / "tests" / "data.json").write_text('{"v": 2}\n')
    (root / "fixtures" / "expected.csv").write_text("2\n")
    (root / "tests" / "test_val.py").write_text(
        "import json\nfrom pathlib import Path\n\nfrom pkg import val\n\n\n"
        "def test_json():\n    assert val() == json.load(open(Path(__file__).parent / 'data.json'))['v']\n\n\n"
        "def test_csv():\n    assert val() == int(open(Path(__file__).parent.parent / 'fixtures' / 'expected.csv').read())\n")
    return _init(root)


DATA_ATTACKS = {
    "tests/data.json": lambda r: (r / "tests" / "data.json").write_text('{"v": 1}\n'),
    "tests .txt fixture": lambda r: (r / "tests" / "expected.txt").write_text("1\n"),
    "tests .yaml fixture": lambda r: (r / "tests" / "cases.yaml").write_text("v: 1\n"),
    "tests .csv fixture": lambda r: (r / "tests" / "data.csv").write_text("1\n"),
    "snapshot file": lambda r: ((r / "tests" / "__snapshots__").mkdir(), (r / "tests" / "__snapshots__" / "t.ambr").write_text("1\n")),
    "snapshots dir txt": lambda r: ((r / "tests" / "snapshots").mkdir(), (r / "tests" / "snapshots" / "t.txt").write_text("1\n")),
    "fixture outside a test dir that a test names": lambda r: (r / "fixtures" / "expected.csv").write_text("1\n"),
    "README comment tweak": lambda r: (r / "README.md").write_text("# hi\n"),
}


@real_sandbox
@pytest.mark.parametrize("name", list(DATA_ATTACKS), ids=list(DATA_ATTACKS))
def test_test_data_and_support_changes_are_never_eligible(datarepo, tmp_path, name):
    def mutate(r: Path):
        (r / "src" / "pkg.py").write_text("def val():\n    return 1  # trivial change\n")
        DATA_ATTACKS[name](r)
    r = verify_unit(datarepo, _patch(datarepo, tmp_path, mutate))
    assert (r.verify_status, r.reason) == ("unavailable", "non_python_change"), (name, r)
    assert not r.used_eligible


@real_sandbox
def test_test_support_code_change_is_refused(datarepo, tmp_path):
    def mutate(r: Path):
        (r / "src" / "pkg.py").write_text("def val():\n    return 1  # c\n")
        (r / "tests" / "helpers.py").write_text("X = 1\n")
    r = verify_unit(datarepo, _patch(datarepo, tmp_path, mutate))
    assert (r.verify_status, r.reason) == ("fail", "test_support_changed"), r


@pytest.fixture
def dynrepo(tmp_path):
    """Fixtures outside a test dir, opened by a computed name, a glob and a listdir."""
    root = tmp_path / "dynrepo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "cfg").mkdir()
    (root / "src" / "pkg.py").write_text("def val():\n    return 1\n")
    (root / "cfg" / "expected.json").write_text('{"v": 2}\n')
    (root / "tests" / "test_dyn.py").write_text(
        "import glob, json, os\nfrom pathlib import Path\n\nfrom pkg import val\n\nCFG = Path(__file__).parent.parent / 'cfg'\n\n\n"
        "def test_built_name():\n    assert val() == json.load(open(CFG / ('exp' + 'ected.json')))['v']\n\n\n"
        "def test_glob():\n    assert val() == json.load(open(glob.glob(str(CFG / '*.json'))[0]))['v']\n\n\n"
        "def test_listdir():\n    assert val() == json.load(open(CFG / os.listdir(CFG)[0]))['v']\n")
    return _init(root)


@real_sandbox
def test_dynamic_name_glob_listdir_fixture_edit_is_not_eligible(dynrepo, tmp_path):
    def mutate(r: Path):
        (r / "cfg" / "expected.json").write_text('{"v": 1}\n')
        (r / "src" / "pkg.py").write_text("def val():\n    return 1  # note\n")
    r = verify_unit(dynrepo, _patch(dynrepo, tmp_path, mutate))
    assert (r.verify_status, r.reason) == ("unavailable", "non_python_change"), r
    assert not r.used_eligible


@real_sandbox
@pytest.mark.parametrize("where", ["cfg", "tests"])
def test_a_fixture_rename_is_not_eligible(dynrepo, tmp_path, where):
    (dynrepo / "tests" / "fx.json").write_text('{"v": 2}\n')
    _git(dynrepo, "add", "-A")
    _git(dynrepo, "commit", "-qm", "fx")
    src = "cfg/expected.json" if where == "cfg" else "tests/fx.json"

    def mutate(r: Path):
        _git(r, "mv", src, src.replace(".json", "2.json"))
        (r / "src" / "pkg.py").write_text("def val():\n    return 1  # note\n")
    patch = _patch(dynrepo, tmp_path, mutate)
    assert "rename from" in patch
    r = verify_unit(dynrepo, patch)
    assert (r.verify_status, r.reason) == ("unavailable", "non_python_change"), r


@real_sandbox
def test_a_cyrillic_lookalike_dir_is_suspicious(dynrepo, tmp_path):
    def mutate(r: Path):
        (r / "t\u0435sts").mkdir()                    # Cyrillic small ie
        (r / "t\u0435sts" / "data.json").write_text("{}\n")
        (r / "src" / "pkg.py").write_text("def val():\n    return 1  # note\n")
    r = verify_unit(dynrepo, _patch(dynrepo, tmp_path, mutate))
    assert (r.verify_status, r.reason) == ("unavailable", "suspicious_path"), r


def test_suspicious_paths_unit():
    assert VU.suspicious_paths(["src/ok.py", "README.md"], {"src/ok.py"}) == []
    assert VU.suspicious_paths(["t\u0435sts/a.py"]) == ["t\u0435sts/a.py"]
    assert VU.suspicious_paths(["\uff54ests/a.py"]) == ["\uff54ests/a.py"]          # fullwidth t: NFKC changes it
    assert VU.suspicious_paths(["Src/pkg.py"], {"src/pkg.py"}) == ["Src/pkg.py"]      # case-fold collision


@real_sandbox
def test_a_readme_tweak_next_to_a_real_fix_is_not_eligible_recall_cost(broken, tmp_path):
    def mutate(r: Path):
        _fix(r)
        (r / "README.md").write_text("# demo, now fixed\n")
    r = verify_unit(broken, _patch(broken, tmp_path, mutate))
    assert (r.verify_status, r.reason) == ("unavailable", "non_python_change"), r
    assert not r.used_eligible


@real_sandbox
def test_a_pure_python_fix_is_still_eligible(broken, tmp_path):
    assert verify_unit(broken, _patch(broken, tmp_path, _fix)).used_eligible


@pytest.fixture
def helperrepo(tmp_path):
    """A failing test whose assert lives in a source module; and one whose input can be special-cased."""
    root = tmp_path / "helperrepo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "pkg" / "__init__.py").write_text("")
    (root / "src" / "pkg" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "src" / "pkg" / "check.py").write_text("def must_equal(a, b):\n    assert a == b\n")
    (root / "tests" / "test_calc.py").write_text(
        "from pkg.calc import add\nfrom pkg.check import must_equal\n\n\n"
        "def test_add():\n    must_equal(add(1, 2), 3)\n")
    return _init(root)


@real_sandbox
def test_known_limit_gutting_an_assert_helper_in_source_is_eligible_today(helperrepo, tmp_path):
    """KNOWN LIMIT (not fixable statically; measured by the hidden-test precision bar). A fix flips this."""
    r = verify_unit(helperrepo, _patch(helperrepo, tmp_path, lambda c: (c / "src" / "pkg" / "check.py").write_text(
        "def must_equal(a, b):\n    pass\n")))
    assert r.verify_status == "pass_f2p" and r.used_eligible, r


@real_sandbox
def test_known_limit_special_casing_the_test_input_in_source_is_eligible_today(helperrepo, tmp_path):
    """KNOWN LIMIT: add() still wrong in general, right for the one input the test uses."""
    r = verify_unit(helperrepo, _patch(helperrepo, tmp_path, lambda c: (c / "src" / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    if (a, b) == (1, 2):\n        return 3\n    return a - b\n")))
    assert r.verify_status == "pass_f2p" and r.used_eligible, r


@pytest.fixture
def twotests(tmp_path):
    root = tmp_path / "twotests"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "pkg.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "tests" / "test_pkg.py").write_text(
        "from pkg import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
        "def test_zero():\n    assert add(0, 0) == 0\n")
    (root / "tests" / "test_other.py").write_text("from pkg import add\n\n\ndef test_other():\n    assert add(2, 2) == 4\n")
    return _init(root)


@real_sandbox
def test_renaming_a_passing_test_and_adding_a_dummy_is_tests_disappeared(twotests, tmp_path):
    def mutate(r: Path):
        _fix(r)
        f = r / "tests" / "test_pkg.py"
        f.write_text(f.read_text().replace("def test_zero", "def xtest_zero") + "\n\ndef test_dummy():\n    assert True\n")
    r = verify_unit(twotests, _patch(twotests, tmp_path, mutate))
    assert (r.verify_status, r.reason) == ("fail", "tests_disappeared"), r
    assert not r.used_eligible


@real_sandbox
def test_editing_an_existing_test_file_is_never_eligible(twotests, tmp_path):
    def mutate(r: Path):
        _fix(r)
        f = r / "tests" / "test_pkg.py"
        f.write_text(f.read_text() + "\n\ndef test_three():\n    assert add(1, 1) == 2\n")
    r = verify_unit(twotests, _patch(twotests, tmp_path, mutate))
    assert (r.verify_status, r.reason) == ("pass_p2p", "edited_tests") and r.n_f2p >= 2, r
    assert "edited_existing_test" in r.flags and not r.used_eligible


@real_sandbox
def test_dropping_a_test_function_is_refused_by_the_early_weakened_check(twotests, tmp_path):
    """Needs the early V.weakened_tests gate: verify() alone would report it as no_test_passed/other."""
    def mutate(r: Path):
        _fix(r)
        f = r / "tests" / "test_pkg.py"
        f.write_text("from pkg import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    r = verify_unit(twotests, _patch(twotests, tmp_path, mutate))
    assert (r.verify_status, r.reason) == ("fail", "tests_weakened"), r


@real_sandbox
@pytest.mark.parametrize("target", ["/dev/zero", "/etc/passwd"])
def test_a_symlink_in_the_patch_fails_closed_fast(broken, tmp_path, target):
    import os
    import time

    def mutate(r: Path):
        _fix(r)
        os.symlink(target, r / "src" / "z.py")
    patch = _patch(broken, tmp_path, mutate)
    t0 = time.monotonic()
    r = verify_unit(broken, patch, budget_s=20)
    assert (r.verify_status, r.reason) == ("unavailable", "symlink_in_patch"), r
    assert time.monotonic() - t0 < 15 and not r.used_eligible


def test_an_oversized_patch_is_unavailable(broken):
    r = verify_unit(broken, "+" * (VU.MAX_PATCH_BYTES + 1))
    assert (r.verify_status, r.reason) == ("unavailable", "patch_too_large")


# ── third review: report channels + threat model (honest-but-sloppy) ─────────

JUNIT_REWRITE = '''import atexit
import os
import re
import sys


def _scrub():
    for a in sys.argv:
        if a.startswith("--junit-xml="):
            path = a.split("=", 1)[1]
            txt = open(path).read()
            txt = re.sub(r"<failure.*?</failure>", "", txt, flags=re.S)
            txt = re.sub(r"<error.*?</error>", "", txt, flags=re.S)
            open(path, "w").write(txt)
    sys.stdout.flush()
    os._exit(0)


atexit.register(_scrub)


def add(a, b):
    return a - b
'''


@real_sandbox
def test_junit_rewrite_plus_forced_exit_code_is_not_eligible(broken, tmp_path):
    """The reviewer's repro: source rewrites the junit file in an atexit hook and exits 0."""
    patch = _patch(broken, tmp_path, lambda r: (r / "src" / "pkg.py").write_text(JUNIT_REWRITE))
    r = verify_unit(broken, patch)
    assert (r.verify_status, r.reason) == ("unavailable", "report_mismatch"), r
    assert not r.used_eligible


@real_sandbox
def test_the_junit_repro_really_works_without_the_cross_check(broken, tmp_path, monkeypatch):
    """Control: with the cross-check off the same patch is called eligible, so the test above proves the fix."""
    patch = _patch(broken, tmp_path, lambda r: (r / "src" / "pkg.py").write_text(JUNIT_REWRITE))
    monkeypatch.setattr(VU, "report_mismatch", lambda run: False)
    assert verify_unit(broken, patch).used_eligible


def test_stdout_summary_parser():
    assert VU.stdout_summary("x\n1 failed, 2 passed in 0.10s\n") == {"failed": 1, "passed": 2}
    assert VU.stdout_summary("==== 3 passed, 1 skipped in 1.2s ====") == {"passed": 3, "skipped": 1}
    assert VU.stdout_summary("no tests ran in 0.01s") == {}
    assert VU.stdout_summary("nothing useful") is None


OBFUSCATED = '''import importlib

_m = importlib.import_module("_py" + "test.rep" + "orts")
_T = getattr(_m, "Test" + "Report")
_orig = _T.from_item_and_call.__func__


def _forge(cls, item, call):
    rep = _orig(cls, item, call)
    rep.outcome = "passed"
    return rep


_T.from_item_and_call = classmethod(_forge)


def add(a, b):
    return a - b
'''


@real_sandbox
def test_known_limit_obfuscated_in_process_report_forging_is_eligible_today(broken, tmp_path):
    """KNOWN LIMIT (honest-but-sloppy threat model): forging pytest's reports in-process makes junit,
    summary and exit code agree, so the cross-check cannot see it. Measured by the hidden-test bar."""
    r = verify_unit(broken, _patch(broken, tmp_path, lambda c: (c / "src" / "pkg.py").write_text(OBFUSCATED)))
    assert r.verify_status == "pass_f2p" and r.used_eligible, r


@pytest.fixture
def expectedrepo(tmp_path):
    root = tmp_path / "expectedrepo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "expected.py").write_text("ADD_1_2 = 3\n")
    (root / "src" / "pkg.py").write_text("def add(a, b):\n    return a - b\n\n\nclass Box:\n    def __init__(self, v):\n        self.v = v\n\n\ndef box(v):\n    return Box(v)\n")
    (root / "tests" / "test_pkg.py").write_text(
        "from expected import ADD_1_2\nfrom pkg import add, box\n\n\n"
        "def test_add():\n    assert add(1, 2) == ADD_1_2\n\n\n"
        "def test_box():\n    assert box(5) == 6\n")
    return _init(root)


@real_sandbox
def test_known_limit_source_data_module_as_expected_values_is_eligible_today(expectedrepo, tmp_path):
    """KNOWN LIMIT: the expected value lives in a source module and is edited to match the buggy output."""
    def mutate(r: Path):
        (r / "src" / "expected.py").write_text("ADD_1_2 = -1\n")
        (r / "src" / "pkg.py").write_text("def add(a, b):\n    return a - b\n\n\ndef box(v):\n    return v + 1\n")
    r = verify_unit(expectedrepo, _patch(expectedrepo, tmp_path, mutate))
    assert r.verify_status == "pass_f2p" and r.used_eligible, r


@real_sandbox
def test_known_limit_always_true_eq_object_is_eligible_today(expectedrepo, tmp_path):
    """KNOWN LIMIT: a return object whose __eq__ is always True satisfies any assert on it."""
    def mutate(r: Path):
        (r / "src" / "expected.py").write_text("ADD_1_2 = -1\n")
        (r / "src" / "pkg.py").write_text(
            "class Any:\n    def __eq__(self, other):\n        return True\n\n\n"
            "def add(a, b):\n    return Any()\n\n\ndef box(v):\n    return Any()\n")
    r = verify_unit(expectedrepo, _patch(expectedrepo, tmp_path, mutate))
    assert r.verify_status == "pass_f2p" and r.used_eligible, r
