#!/usr/bin/env python3
"""Run mutmut over G-F's scope without disturbing Gate 13's config.

Protocol doc 20, Phase 0. Every result records the commit SHA, the exact command
line, the environment and the universe hash — reproducibility is the point, and a
mutation score whose provenance is unrecorded is not evidence.

WHY A SWAP
----------
mutmut 3.6 resolves config in `configuration.py::_config_reader`:

    1. pyproject.toml [tool.mutmut]   -- if present, setup.cfg is IGNORED ENTIRELY
    2. else setup.cfg [mutmut]

There is no `--config` flag and no support for a second named section. So the
obvious way to add a second configuration — `[tool.mutmut]` in pyproject.toml —
would have **silently disabled** the Gate-13 scope that lives in setup.cfg,
rather than sitting alongside it. mutmut would then have run cheerfully over the
wrong files and reported a score, which is the failure mode this whole audit
exists to remove.

So: swap `config/mutmut_gf.cfg` into `setup.cfg`, run, and restore the original
**byte-for-byte** in a `finally`. The restore is verified by hash, and
`tests/test_gate13_mutmut_config_intact.py` fails if a crashed run ever leaves
the wrong config in place.

This deliberately breaks the session rule "do not change a measuring instrument
mid-measurement" in a narrow, bounded way: the swap happens *before* the
measurement starts and is undone *after* it ends, never during. The hash check
is what makes that claim checkable rather than asserted.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SETUP_CFG = REPO / "setup.cfg"
GF_CFG = REPO / "config" / "mutmut_gf.cfg"
PYPROJECT = REPO / "pyproject.toml"

#: pytest-timeout budget for runs inside mutmut's working copy, in seconds.
#:
#: pyproject sets `timeout = 30`, calibrated against the real tree. mutmut's working
#: copy is a DIFFERENT SIZE BY CONSTRUCTION: 14MB -> 460MB, 442k -> 23.4M AST nodes.
#: Eight tests `ast.walk` the whole source tree; measured, that walk takes 27.7s there
#: versus 0.3s here. At a 30s limit it lands inside the noise band and fires.
#:
#: THIS IS NOT A CONVENIENCE. When pytest-timeout fires, SIGALRM interrupts a C-level
#: frame whose tb_lineno is None, and pytest's own _getreprcrash dies formatting it
#: ("unsupported operand type(s) for -: 'NoneType' and 'int'"), so mutmut sees
#: "failed to collect stats. runner returned 3" and the run cannot start at all.
#:
#: AND THE SCORING CONSEQUENCE IS WORSE THAN THE CRASH. mutmut marks a mutant KILLED
#: when the suite fails. A test that fails for an ENVIRONMENTAL reason fails on every
#: mutant run it covers, marking all of them killed no matter what the mutation did.
#: That INFLATES the mutation score, and it inflates it in the direction that flatters
#: the result -- the exact shape of the frozen sample's bogus 1.00.
#:
#: Raising this is therefore a correctness fix, not a leniency. It is applied to the
#: subprocess environment only; pyproject's committed 30s is untouched, so the ordinary
#: suite keeps its tight bound. The value used is recorded in run_metadata.json.
MUTATION_PYTEST_TIMEOUT = "300"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()


def _assert_pyproject_would_not_hijack() -> None:
    """If pyproject gains [tool.mutmut], setup.cfg stops being read at all.

    Checked every run rather than assumed: a future contributor adding that
    section would silently redirect BOTH this run and Gate 13's, and nothing
    else in the repo would notice.
    """
    if "[tool.mutmut]" in PYPROJECT.read_text():
        sys.exit(
            "REFUSING TO RUN: pyproject.toml now has a [tool.mutmut] section.\n"
            "mutmut prefers it and IGNORES setup.cfg entirely, so this swap would\n"
            "have no effect and the scope would silently be someone else's.\n"
            "Remove it, or rework this script to write pyproject instead."
        )


def _assert_all_root_packages_are_copied() -> None:
    """Every top-level package must be in also_copy, or the run dies at stats.

    mutmut builds an isolated working copy from `source_paths` + `also_copy`. Any
    root-level package the suite imports and that is missing from those lists
    produces `ModuleNotFoundError` roughly two minutes in, at the stats stage.

    This cost two failed baseline runs — `bench`, then `soak`. Fixing them one at
    a time is the checklist error; enumerating the class is the fix. A package
    added to the repo later fails here, instantly and by name, instead of
    mid-run.
    """
    # PARSE the also_copy values. The first version of this check did
    # `name not in GF_CFG.read_text()` — a substring match over the whole file,
    # which the config's own COMMENTS satisfied. Deleting the real `soak` entry
    # left the word "soak" in a comment and the guard passed happily. A guard
    # that a comment can satisfy is a guard that cannot fail, which is the exact
    # defect class this protocol exists to remove — found here in my own guard,
    # by deliberately removing the entry and watching it not fire.
    import configparser

    parser = configparser.ConfigParser()
    parser.read(GF_CFG)
    copied = {
        line.strip()
        for line in parser.get("mutmut", "also_copy", fallback="").splitlines()
        if line.strip()
    } | {
        line.strip()
        for line in parser.get("mutmut", "source_paths", fallback="").splitlines()
        if line.strip()
    }
    # EVERY root dir containing Python, not just packages with __init__.py. The
    # first version required __init__.py and therefore missed scripts/, which
    # tests reach via a sys.path insert -- a third failed run. Static reasoning
    # about "what is importable" was wrong twice; enumerating what exists is not.
    _EXCLUDE = {"mutants", "src"}  # mutmut's own output; src is source_paths
    roots = {
        p.name for p in REPO.iterdir()
        if p.is_dir()
        and not p.name.startswith(".")
        and p.name not in _EXCLUDE
        and any(p.rglob("*.py"))
    }
    missing = sorted(r for r in roots if r not in copied)
    if missing:
        sys.exit(
            f"REFUSING TO RUN: top-level package(s) {missing} are not in "
            f"{GF_CFG.name}'s also_copy. mutmut's working copy would not contain "
            "them and the run would die at the stats stage with ModuleNotFoundError."
        )


def _assert_setup_cfg_is_not_already_swapped() -> None:
    """Refuse to start if setup.cfg is already holding the G-F scope.

    THE RESTORE IS SELF-CONSISTENT, NOT CORRECT. `run()` saves whatever setup.cfg
    contains, swaps, and restores that — verifying the sha256 matches what it read at
    the start. If the file was ALREADY contaminated when the run began, every check
    passes and the contamination is faithfully carried forward.

    That happened: a foreground run was killed at a 2-minute command timeout, before its
    `finally` could restore. The next two runs then read the swapped file as their
    "original" and dutifully restored the G-F scope, hash-verified, twice. Gate 13's
    scope was gone from setup.cfg and every guard inside this script reported success.

    Only `tests/test_gate13_mutmut_config_intact.py` caught it, because it compares
    against an INDEPENDENT declaration of Gate 13's six modules rather than against the
    file itself. This check makes the harness refuse rather than rely on that.
    """
    parser = configparser.ConfigParser()
    parser.read(SETUP_CFG)
    current = {
        line.strip()
        for line in parser.get("mutmut", "only_mutate", fallback="").splitlines()
        if line.strip()
    }
    gf_parser = configparser.ConfigParser()
    gf_parser.read(GF_CFG)
    gf_scope = {
        line.strip()
        for line in gf_parser.get("mutmut", "only_mutate", fallback="").splitlines()
        if line.strip()
    }
    if current == gf_scope:
        sys.exit(
            "REFUSING TO RUN: setup.cfg already holds the G-F scope, so a previous run "
            "was killed before it could restore Gate 13's config.\n"
            "Restore it first (`git checkout setup.cfg`) — otherwise this run would save "
            "the contaminated file as its 'original' and put it back afterwards, "
            "hash-verified and still wrong."
        )


#: AMENDMENT 2 — the one-line upstream fix this campaign depends on.
_MUTMUT_PATCH_MARKER = "LLM_ROUTER PATCH (G-F AMENDMENT 2"


def _assert_mutmut_is_patched() -> tuple[str, int]:
    """mutmut must carry the Amendment-2 patch, and its premise must still hold.

    `record_trampoline_hit` upstream does a STRICT resolve of the (relative)
    `source_paths` on every trampoline hit during the stats pass, then reads the result
    only inside `if max_stack_depth != -1`. At the default -1 the value is dead, but the
    resolve still runs — and resolving a relative path against the current working
    directory raises for any test that has chdir()'d away from the repo root:

        FileNotFoundError: [Errno 2] No such file or directory: 'src'

    Eight test files (104 tests) chdir; one aborted the entire stats stage.

    The patch moves the computation inside the branch that consumes it. It is
    semantically identical whenever the value is actually used, so it cannot change a
    score — it only stops a crash. That argument depends on `max_stack_depth == -1`,
    which is why this checks the value rather than assuming it, and returns both facts
    for `run_metadata.json`.

    A `.venv` rebuild silently drops the patch. Without this check the next run would
    die mid-stats with a FileNotFoundError that looks like a repository problem rather
    than a missing dependency patch.
    """
    from mutmut import __main__ as mutmut_main
    from mutmut.configuration import _load_config

    source = Path(mutmut_main.__file__).read_text(encoding="utf-8")
    if _MUTMUT_PATCH_MARKER not in source:
        sys.exit(
            "REFUSING TO RUN: mutmut is not carrying the Amendment-2 patch.\n"
            f"Expected marker {_MUTMUT_PATCH_MARKER!r} in {mutmut_main.__file__}.\n"
            "A .venv rebuild drops it. Re-apply it (see doc 20, AMENDMENT 2) before "
            "running, or the stats stage dies on the first test that chdir()s."
        )

    depth = _load_config().max_stack_depth
    if depth != -1:
        sys.exit(
            f"REFUSING TO RUN: max_stack_depth is {depth}, not -1.\n"
            "AMENDMENT 2's justification is that the patched value is UNUSED at -1. "
            "With a real depth set it is used again, so the patch must be re-argued "
            "before this campaign's numbers can rely on it."
        )
    return Path(mutmut_main.__file__).name, depth


def run(extra_args: list[str], out_dir: Path) -> int:
    _assert_pyproject_would_not_hijack()
    _assert_all_root_packages_are_copied()
    _assert_setup_cfg_is_not_already_swapped()
    _patched_file, _max_stack_depth = _assert_mutmut_is_patched()

    original = SETUP_CFG.read_text()
    original_hash = _sha256(original)
    gf_config = GF_CFG.read_text()

    started = time.time()
    cmd = [str(REPO / ".venv" / "bin" / "mutmut"), "run", *extra_args]

    env = {**os.environ, "PYTEST_TIMEOUT": MUTATION_PYTEST_TIMEOUT}

    try:
        SETUP_CFG.write_text(gf_config)
        proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, env=env)
        stdout = proc.stdout
        stderr = proc.stderr
        rc = proc.returncode
    finally:
        # Restore FIRST, then verify. If this raises, the guard test catches it
        # on the next suite run; leaving Gate 13's config swapped out silently
        # is the one outcome that must not happen.
        SETUP_CFG.write_text(original)
        restored = _sha256(SETUP_CFG.read_text())
        if restored != original_hash:
            sys.exit(
                f"CRITICAL: setup.cfg was not restored byte-for-byte "
                f"({original_hash[:12]} -> {restored[:12]}). Gate 13's config may "
                "be damaged; restore it from git before doing anything else."
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    # Strip the spinner glyphs mutmut writes; they make the log unreadable and
    # balloon it to ~100KB of animation frames.
    clean = "".join(c for c in stdout if c not in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
    (out_dir / "mutmut_stdout.txt").write_text(clean)

    # STDERR IS WHERE THE REASON LIVES. The first version of this script captured it
    # and then dropped it on the floor, writing only stdout. A generation failure
    # therefore produced a log of nothing but "Generating mutants" spinner frames and a
    # returncode, with the actual traceback discarded — an instrument that recorded
    # everything except the one thing needed to act on it.
    (out_dir / "mutmut_stderr.txt").write_text(stderr)
    if rc != 0 and stderr.strip():
        print("--- mutmut stderr (tail) ---", file=sys.stderr)
        print("\n".join(stderr.strip().splitlines()[-25:]), file=sys.stderr)

    # The mutant-name list runs to ~2000 entries. Embedding it here produced a 92KB
    # metadata file that was unreadable as provenance, which is the opposite of the
    # point. A hash plus a count identifies the exact list just as strongly and stays
    # diffable; the names themselves are already committed in train.txt/validation.txt.
    argv_names = [a for a in extra_args if not a.startswith("-")]
    flags = [a for a in extra_args if a.startswith("-")]

    meta = {
        "commit_sha": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "command": " ".join([*cmd[:2], *flags, f"<{len(argv_names)} mutant names>"]),
        "mutant_names_count": len(argv_names),
        "mutant_names_sha256": _sha256("\n".join(argv_names)),
        "pytest_timeout_sec": MUTATION_PYTEST_TIMEOUT,
        "mutmut_amendment2_patch": True,
        "mutmut_max_stack_depth": _max_stack_depth,
        "pytest_timeout_note": (
            "Overrides pyproject's 30s for this subprocess only. mutmut's working copy "
            "is 33x larger, where the 8 source-scanning tests take 27.7s; at 30s they "
            "fire pytest-timeout, which both crashes pytest's reporter and would mark "
            "every covered mutant killed for an environmental reason, inflating the score."
        ),
        "config_source": str(GF_CFG.relative_to(REPO)),
        "config_sha256": _sha256(gf_config),
        "setup_cfg_restored_sha256": original_hash,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "mutmut_version": _git_pkg_version("mutmut"),
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "elapsed_sec": round(time.time() - started, 1),
        "returncode": rc,
    }
    (out_dir / "run_metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta, indent=2))
    return rc


def _git_pkg_version(pkg: str) -> str:
    try:
        from importlib.metadata import version

        return version(pkg)
    except Exception as exc:  # noqa: BLE001 — provenance must not break the run
        return f"unknown ({exc})"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="mutmut over G-F scope (Gate 13 untouched)")
    ap.add_argument("--out", default=".llm_router/gf/latest", help="artefact directory")
    ap.add_argument(
        "--names-file",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "file of newline-separated mutant names. PREFER THIS over positional args: "
            "passing ~2000 names through the shell silently collapsed them into ONE "
            "argument under zsh, which does not word-split unquoted expansions. Repeatable."
        ),
    )
    ap.add_argument("mutmut_args", nargs="*", help="passed through to `mutmut run`")
    ns = ap.parse_args()

    names: list[str] = []
    for path in ns.names_file:
        names += [ln.strip() for ln in Path(path).read_text().splitlines() if ln.strip()]

    # A single positional carrying whitespace is the zsh-collapse signature. It reached
    # mutmut once and produced a well-formed metadata file recording one mutant, which is
    # exactly the kind of wrong-but-plausible artefact this audit exists to catch.
    for arg in ns.mutmut_args:
        if not arg.startswith("-") and (" " in arg or "\n" in arg):
            sys.exit(
                "REFUSING TO RUN: a positional argument contains whitespace, so the shell "
                f"collapsed a name list into one argument ({len(arg.split())} names in "
                "argv[1]). Use --names-file instead; it does not go through the shell."
            )

    sys.exit(run([*ns.mutmut_args, *names], REPO / ns.out))
