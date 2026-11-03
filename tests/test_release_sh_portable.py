"""release.sh on a stock Mac (W4): the sdist leak check, the timeout fallback, the interpreter.

The 2026-11-03 dry run of scripts/release/release.sh stopped on four defects that a
Linux box never shows:

1. the sdist leak check matched `PRICING` case-insensitively anywhere in a path, so the
   legitimate module src/llm_router/pricing.py BLOCKED the release;
2. the test step ran GNU `timeout --foreground`, which macOS does not have (exit 127);
3. get_version imported tomllib with the system python3, which is 3.9 there;
4. the test step's default limit (300 s) was shorter than a local run of the suite.

Sources release.sh (main() is guarded by BASH_SOURCE) and calls its functions in a
subprocess. release.sh itself is never run: it tags and pushes. No network, no git, no
model. The shell is /bin/bash, the script's shebang: bash 3.2 on macOS.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SH = REPO / "scripts" / "release" / "release.sh"
BASH = "/bin/bash" if Path("/bin/bash").exists() else (shutil.which("bash") or "bash")
ROOT = "llm_routing-16.0.0"  # the top directory of an sdist listing

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash release script")


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(snippet, *, path=None, cwd=None, stdin=None, env=None, timeout=25):
    """Run `snippet` in a bash that sourced release.sh. Errexit is switched off after
    the source (release.sh turns it on), so the script's status is the snippet's last."""
    full_env = {"PATH": os.environ["PATH"] if path is None else path, "LC_ALL": "C"}
    full_env.update(env or {})
    return subprocess.run(
        [BASH, "-c", f'source "{SH}"; set +e; {snippet}'],
        input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=full_env, cwd=cwd, timeout=timeout,
    )


def _stub(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def _tool_dir(parent: Path, *tools: str) -> str:
    """A PATH holding wrappers for exactly the named real tools, so that a host that
    has `timeout` on its PATH cannot change which branch of run_with_timeout runs."""
    directory = parent / "tools"
    directory.mkdir(parents=True, exist_ok=True)
    for tool in tools:
        real = shutil.which(tool)
        if real is None:
            pytest.skip(f"{tool} is not installed here")
        _stub(directory, tool, f'exec "{real}" "$@"\n')
    return str(directory)


def _pyproject_version() -> str:
    with open(REPO / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


# ── 1. the sdist leak check ───────────────────────────────────────────────────

def _leaks(paths) -> list[str]:
    r = _run("sdist_leaks", stdin="".join(f"{ROOT}/{p}\n" for p in paths))
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


@pytest.mark.parametrize("path", [
    "src/llm_router/pricing.py",        # the module that blocked the 2026-11-03 dry run
    "src/llm_router/pricing.pyi",
    "src/llm_router/strategy.py",
    "src/llm_router/roadmap.py",
    "src/llm_router/monetization.py",
    "src/llm_router/phase_completion.py",
    "src/llm_router/data/pricing.json",  # package data is not a document
    "docs/pricing.md5",                  # `.md` must end the extension: this is a checksum
])
def test_a_source_file_named_like_an_internal_doc_is_not_a_leak(path):
    assert _leaks([path]) == []


@pytest.mark.parametrize("path", [
    "PRICING.md", "ROADMAP.md", "MONETIZATION.md", "STRATEGY.md", "PHASE_COMPLETION_REVIEW.md",
    "docs/PRICING.md",
    "docs/Roadmap-2027.md",              # case-insensitive, as before
    "docs/MONETIZATION_STRATEGY.pdf",
    "ROADMAP.txt",
    "PRICING.md.bak", "docs/roadmap.md~", "STRATEGY.txt.orig",  # a backup of a document is one
    "src/llm_router/PRICING.md",         # a document under src/ is still a document
    "docs/strategy/overview.md",         # a folder named for one of them
])
def test_internal_documents_still_block(path):
    assert _leaks([path]) == [f"{ROOT}/{path}"]


@pytest.mark.parametrize("path", [
    ".env", "config/prod.env", "src/llm_router/.env",
    "CLAUDE.md", "docs/CLAUDE.md", "src/llm_router/claude.md",
    ".internal/notes.txt", "src/llm_router/.internal/x.py",
    "research/plan.md", "src/llm_router/research/x.py",
    "deprecation/old.py",
    "scripts/build.sh", "src/llm_router/scripts/x.py",
    "skills/a/SKILL.md", "src/llm_router/skills/x.py",
])
def test_the_other_internal_patterns_still_block_wherever_they_appear(path):
    assert _leaks([path]) == [f"{ROOT}/{path}"]


def test_only_the_offending_lines_are_reported():
    clean = ["PKG-INFO", "README.md", "pyproject.toml", "src/llm_router/pricing.py", "docs/guide.md"]
    bad = ["PRICING.md", ".env", "scripts/x.sh"]
    assert _leaks(clean[:2] + bad[:1] + clean[2:] + bad[1:]) == [f"{ROOT}/{p}" for p in bad]


def test_no_tracked_package_file_trips_the_leak_check():
    """The population the check really sees: every tracked file under src/ ships in the sdist."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed here")
    r = subprocess.run([git, "-C", str(REPO), "ls-files", "--", "src"],
                       capture_output=True, text=True, timeout=30)
    files = r.stdout.splitlines()
    if r.returncode != 0 or not files:
        pytest.skip("not a git checkout")
    assert len(files) > 100, "an empty or tiny set would pass anything"
    assert _leaks(files) == []


def _make_sdist(tmp_path: Path, members) -> Path:
    path = tmp_path / "sdist.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        for m in members:
            tf.addfile(tarfile.TarInfo(f"{ROOT}/{m}"))
    return path


def test_verify_sdist_clean_passes_a_clean_archive_with_pricing_py(tmp_path):
    sdist = _make_sdist(tmp_path, ["PKG-INFO", "src/llm_router/pricing.py", "docs/guide.md"])
    r = _run(f'verify_sdist_clean "{sdist}"')
    assert r.returncode == 0, r.stdout + r.stderr
    assert "sdist is clean" in r.stdout


def test_verify_sdist_clean_blocks_a_leak_and_names_it(tmp_path):
    sdist = _make_sdist(tmp_path, ["PKG-INFO", "src/llm_router/pricing.py", "PRICING.md"])
    r = _run(f'verify_sdist_clean "{sdist}"')
    assert r.returncode == 1
    assert "BLOCKED" in r.stdout and f"{ROOT}/PRICING.md" in r.stdout
    assert f"{ROOT}/src/llm_router/pricing.py" not in r.stdout


def test_verify_sdist_clean_refuses_an_archive_that_lists_nothing(tmp_path):
    """An empty listing passes every pattern; that is a refusal, not a clean sdist."""
    r = _run(f'verify_sdist_clean "{_make_sdist(tmp_path, [])}"')
    assert r.returncode == 1
    assert "NOT checked" in r.stdout and "sdist is clean" not in r.stdout


def test_verify_sdist_clean_refuses_an_unreadable_archive(tmp_path):
    bad = tmp_path / "not-a-tarball.tar.gz"
    bad.write_text("this is not gzip data")
    r = _run(f'verify_sdist_clean "{bad}"')
    assert r.returncode == 1
    assert "Cannot read" in r.stdout and "sdist is clean" not in r.stdout


@pytest.mark.slow
def test_the_real_sdist_passes_the_leak_check(tmp_path):
    """End to end on the artifact release.sh step 4 really builds (slow: runs `uv build`)."""
    if shutil.which("uv") is None:
        pytest.skip("uv not available to build the sdist")
    out = tmp_path / "dist"
    proc = subprocess.run(["uv", "build", "--sdist", "--out-dir", str(out)],
                          cwd=REPO, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        pytest.skip(f"sdist build unavailable here: {proc.stderr[-300:]}")
    sdists = list(out.glob("*.tar.gz"))
    assert sdists, "build produced no sdist"
    r = _run(f'verify_sdist_clean "{sdists[0]}"', timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


# ── 2. the timeout wrapper ────────────────────────────────────────────────────

@pytest.fixture
def perl_only(tmp_path):
    """A PATH with perl, sh, sleep and cat and no timeout/gtimeout: the fallback branch on any host."""
    return _tool_dir(tmp_path, "perl", "sh", "sleep", "cat")


@pytest.mark.parametrize("code", [0, 1, 7, 42])
def test_fallback_passes_the_commands_own_exit_status_through(perl_only, code):
    r = _run(f"run_with_timeout 60 sh -c 'exit {code}'", path=perl_only)
    assert r.returncode == code, r.stderr


@pytest.mark.parametrize("limit", ["0.5", "1s"])
def test_fallback_exits_124_when_the_limit_is_hit(perl_only, limit):
    # `sleep 30` cannot finish inside the limit; if the fallback waited for it, the 25 s
    # bound on the subprocess would raise instead of this returning.
    try:
        r = _run(f"run_with_timeout {limit} sleep 30", path=perl_only)
    except subprocess.TimeoutExpired:
        pytest.fail("the fallback waited for the 30 s command instead of timing it out")
    assert r.returncode == 124, r.stderr


def test_fallback_escalates_to_sigkill_when_term_is_ignored(perl_only):
    # `trap "" TERM` in the parent shell: an ignored signal is inherited, so the command
    # ignores TERM from its first instruction (no race with a trap set inside it). Only
    # SIGKILL can end `sleep 30` early, so returning inside the 25 s bound proves it.
    try:
        r = _run('trap "" TERM; run_with_timeout 0.5 sleep 30', path=perl_only,
                 env={"RELEASE_TIMEOUT_KILL_AFTER": "1"})
    except subprocess.TimeoutExpired:
        pytest.fail("a command that ignores SIGTERM was never killed")
    assert r.returncode == 124, r.stderr


def test_fallback_reports_a_missing_command_as_127(perl_only):
    r = _run("run_with_timeout 60 definitely-not-a-command-xyz", path=perl_only)
    assert r.returncode == 127
    assert "definitely-not-a-command-xyz" in r.stderr


def test_fallback_reports_a_command_it_cannot_run_as_126(perl_only, tmp_path):
    not_executable = tmp_path / "plain.txt"
    not_executable.write_text("data")
    not_executable.chmod(0o644)
    r = _run(f'run_with_timeout 60 "{not_executable}"', path=perl_only)
    assert r.returncode == 126, r.stderr


@pytest.mark.parametrize("sig,expected", [("TERM", 143), ("KILL", 137), ("INT", 130)])
def test_fallback_reports_128_plus_n_when_a_signal_killed_the_command(perl_only, sig, expected):
    r = _run(f"run_with_timeout 60 sh -c 'kill -{sig} $$'", path=perl_only)
    assert r.returncode == expected, r.stderr


@pytest.mark.parametrize("bad", ["abc", "-5", ""])
def test_fallback_rejects_a_bad_duration_as_125(perl_only, bad):
    r = _run(f"run_with_timeout '{bad}' sh -c 'exit 0'", path=perl_only)
    assert r.returncode == 125
    assert "invalid time interval" in r.stderr


def test_fallback_leaves_the_commands_output_alone(perl_only):
    r = _run("run_with_timeout 60 sh -c 'echo to-out; echo to-err >&2'", path=perl_only)
    assert r.returncode == 0
    assert r.stdout == "to-out\n" and r.stderr == "to-err\n"


def test_a_hung_run_that_printed_100_percent_is_still_exit_124_with_the_marker_captured(perl_only):
    """What step 2 depends on: `out=$(run_with_timeout ...) || code=$?`, then 124 + [100%] = pass."""
    r = _run("""out=$(run_with_timeout 3 sh -c 'echo "[ 99%]"; echo "[100%]"; exec sleep 30' 2>&1)
code=$?
echo "code=$code"
echo "$out" """, path=perl_only)
    assert "code=124" in r.stdout
    assert "[100%]" in r.stdout


def test_timeout_is_the_first_choice_and_gets_the_foreground_flag(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    log = tmp_path / "calls"
    _stub(d, "timeout", f'echo "timeout $*" >> "{log}"\nexit 3\n')
    _stub(d, "gtimeout", f'echo "gtimeout $*" >> "{log}"\nexit 4\n')
    _stub(d, "perl", f'echo "perl $*" >> "{log}"\nexit 5\n')
    r = _run("run_with_timeout 7 echo hi there", path=str(d))
    assert r.returncode == 3
    assert log.read_text() == "timeout --foreground 7 echo hi there\n"


def test_gtimeout_is_the_second_choice(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    log = tmp_path / "calls"
    _stub(d, "gtimeout", f'echo "gtimeout $*" >> "{log}"\nexit 4\n')
    _stub(d, "perl", f'echo "perl $*" >> "{log}"\nexit 5\n')
    r = _run("run_with_timeout 7 echo hi there", path=str(d))
    assert r.returncode == 4
    assert log.read_text() == "gtimeout --foreground 7 echo hi there\n"


def test_with_no_timeout_gtimeout_or_perl_it_refuses_instead_of_running_unbounded(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    r = _run("run_with_timeout 7 echo hi", path=str(empty))
    assert r.returncode == 125
    assert "coreutils" in r.stderr and "hi" not in r.stdout


def _is_gnu_timeout() -> bool:
    exe = shutil.which("timeout")
    if exe is None:
        return False
    r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
    return "GNU coreutils" in r.stdout


@pytest.mark.parametrize("snippet,expected", [
    ("sh -c 'exit 0'", 0), ("sh -c 'exit 1'", 1), ("sh -c 'exit 7'", 7),
    ("sleep 30", 124), ("definitely-not-a-command-xyz", 127),
])
def test_fallback_exit_codes_match_gnu_timeout_where_both_exist(tmp_path, snippet, expected):
    """Same contract: run each case through GNU timeout and through the perl fallback."""
    if not _is_gnu_timeout():
        pytest.skip("GNU timeout is not on this host's PATH (stock macOS)")
    limit = "0.5" if expected == 124 else "60"
    gnu = _run(f"run_with_timeout {limit} {snippet}",
               path=_tool_dir(tmp_path / "gnu", "timeout", "sh", "sleep"))
    fallback = _run(f"run_with_timeout {limit} {snippet}",
                    path=_tool_dir(tmp_path / "pl", "perl", "sh", "sleep", "cat"))
    assert gnu.returncode == fallback.returncode == expected


# ── 3. the project interpreter ────────────────────────────────────────────────

OLD_PYTHON = '''echo "python3 $*" >> "{log}"
[ "$1" = "-c" ] || exit 2
exec "{real}" -c 'import sys
sys.modules["tomllib"] = None
sys.version_info = (3, 9, 6, "final", 0)
exec(compile(sys.argv[1], "<-c>", "exec"))' "$2"
'''  # a Python that reports 3.9.6 and has no tomllib: the macOS system python3

FAKE_UV = '''echo "uv $*" >> "{log}"
[ "$1" = run ] && shift
[ "$1" = --frozen ] && shift
[ "$1" = python ] && shift
exec "{real}" "$@"
'''  # `uv run --frozen python ARGS` -> the interpreter that runs these tests

OLD_COMMAND = "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"


def _old_python(d: Path, log: Path) -> None:
    _stub(d, "python3", OLD_PYTHON.format(log=log, real=sys.executable))


def test_the_old_python_stand_in_fails_the_way_the_dry_run_did(tmp_path):
    """Control for the tests below: the pre-fix command dies on this stand-in."""
    d = tmp_path / "bin"
    d.mkdir()
    _old_python(d, tmp_path / "py.log")
    r = subprocess.run([str(d / "python3"), "-c", OLD_COMMAND], cwd=REPO,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "tomllib" in r.stderr


def test_get_version_uses_the_project_interpreter_not_the_system_python3(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    pylog, uvlog = tmp_path / "py.log", tmp_path / "uv.log"
    _old_python(d, pylog)
    _stub(d, "uv", FAKE_UV.format(log=uvlog, real=sys.executable))
    r = _run("get_version", path=str(d), cwd=REPO)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == _pyproject_version()
    assert uvlog.read_text().startswith("uv run --frozen python -c ")
    assert not pylog.exists(), "the old system python3 was called"


def test_project_python_prefers_uv_over_an_old_python3(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    pylog, uvlog = tmp_path / "py.log", tmp_path / "uv.log"
    _old_python(d, pylog)
    _stub(d, "uv", FAKE_UV.format(log=uvlog, real=sys.executable))
    r = _run("project_python -c 'import sys; print(sys.version_info >= (3, 11))'", path=str(d))
    assert r.returncode == 0 and r.stdout.strip() == "True"
    assert not pylog.exists()


def test_without_uv_an_old_python3_stops_with_a_clear_message(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    pylog = tmp_path / "py.log"
    _old_python(d, pylog)
    r = _run("version=$(get_version); code=$?; echo \"code=$code stdout=[$version]\"",
             path=str(d), cwd=REPO)
    assert "code=1 stdout=[]" in r.stdout, r.stdout + r.stderr
    assert "Python >= 3.11" in r.stderr and "3.9.6" in r.stderr and "uv" in r.stderr
    assert "Traceback" not in r.stderr and "ModuleNotFoundError" not in r.stderr


def test_without_uv_a_new_enough_python3_is_used(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    _stub(d, "python3", f'exec "{sys.executable}" "$@"\n')
    r = _run("get_version", path=str(d), cwd=REPO)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == _pyproject_version()


def test_no_bare_python3_is_left_outside_the_interpreter_gate():
    text = SH.read_text()
    start = text.index("project_python() {")
    end = text.index("\n}\n", start)
    code = [ln for ln in (text[:start] + text[end:]).splitlines()
            if "python3" in ln and not ln.lstrip().startswith("#")]
    assert code == []
    assert "project_python scripts/ci/verify-version-sync.py" in text
    assert "project_python scripts/ci/verify-plugin-sync.py" in text


# ── 4. the test step ──────────────────────────────────────────────────────────

def test_the_test_step_uses_the_wrapper_and_keeps_its_124_rule():
    text = SH.read_text()
    assert '_test_output=$(run_with_timeout "$_test_timeout" uv run pytest tests/' in text
    assert "_test_output=$(timeout" not in text
    # exit 124 with the pytest completion marker is still a pass
    assert 'elif [ $_test_exit -eq 124 ] && echo "$_test_output" | grep -q "\\[100%\\]"; then' in text


def test_the_default_test_limit_covers_a_local_run_of_the_suite():
    match = re.search(r'RELEASE_TEST_TIMEOUT:-(\d+)', SH.read_text())
    assert match, "RELEASE_TEST_TIMEOUT default not found"
    # A local run takes over an hour single-threaded; the old default was 300 s.
    assert int(match.group(1)) >= 7200
