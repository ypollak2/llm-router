"""R3 — the allowlist is a footgun guardrail, not a containment boundary.

SECURITY.md said, correctly and reproducibly, that **10 of 12** commands in the
corpus are refused by `guard_command`. The number was right. It was also worse
than a wrong number, because it invited exactly the conclusion the design
cannot support: that the allowlist constrains what an agent can do.

The corpus measured the wrong population. Every one of its twelve entries is an
obviously destructive or exfiltrating command — `rm -rf /`, `git push --force`,
`curl -d @.env`. It measures how well the allowlist stops a command you would
have caught by reading it.

Measured 2026-09-22 against the real `guard_command`, using the SAME
capabilities reached through programs the allowlist PERMITS:

    10 of 10 interpreter probes ALLOWED

`cat ../../.ssh/id_rsa` is refused. `python3 -c` reading the same file is not.
`curl -X POST` is refused. `python -c` with `urllib.request` is not. The
allowlist contains ten general-purpose interpreters, and each one is a complete
bypass of every rule the list expresses.

THIS IS NOT A BUG TO PATCH. The design goal (let a model run dev tools) and the
security goal (constrain execution) are in direct conflict, and the allowlist
resolves it toward capability while presenting as a control. Blocking `git -c`
closes one door of eleven. The decision taken was (a) HONESTY: say plainly what
it does, and recommend OS-level containment for untrusted repos.

What this file enforces is that the honesty cannot rot:

  * every corpus row's verdict is re-derived from the code, so the numbers in
    SECURITY.md are measured rather than remembered;
  * every allowlisted program is classified as an interpreter or not, with a
    reason, so a NEW interpreter fails here instead of silently widening the
    escape;
  * SECURITY.md may not contain a claim that survives "does the allowlist
    prevent arbitrary code execution?"
"""

from __future__ import annotations

import importlib.util
import pathlib
import shlex

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
CORPUS = REPO / "docs" / "security_command_matrix.txt"
SECURITY = REPO / "SECURITY.md"


def _guard():
    """Load `agent_writes` by path — it is a hook, not an importable module."""
    path = REPO / "src" / "llm_router" / "hooks" / "agent_writes.py"
    spec = importlib.util.spec_from_file_location("_aw_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _corpus() -> list[tuple[str, str]]:
    rows = []
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        verdict, _, cmd = line.partition("\t")
        assert verdict in {"ALLOWED", "REFUSED"}, f"bad verdict in corpus: {line!r}"
        rows.append((verdict, cmd))
    return rows


def test_the_corpus_covers_both_populations():
    """Anti-vacuity, and the whole point of R3.

    A corpus of only obviously-dangerous commands produces a flattering number.
    This asserts the corpus still contains the interpreter block that makes the
    flattering number meaningless.
    """
    rows = _corpus()
    assert len(rows) >= 20, f"corpus has only {len(rows)} rows"
    allowed = [c for v, c in rows if v == "ALLOWED"]
    assert len(allowed) >= 10, (
        f"only {len(allowed)} ALLOWED rows. If the interpreter block was "
        "removed, the corpus is back to measuring the population that "
        "produced '10 of 12 refused' — accurate, and the reason this file "
        "exists."
    )


@pytest.mark.parametrize("verdict,cmd", _corpus(), ids=lambda x: str(x)[:40])
def test_every_corpus_verdict_is_what_the_code_actually_does(verdict, cmd):
    """The numbers in SECURITY.md are derived here, never hand-edited."""
    aw = _guard()
    try:
        argv = shlex.split(cmd)
    except ValueError:
        argv = cmd.split()
    ok, _msg = aw.guard_command(argv)
    got = "ALLOWED" if ok else "REFUSED"
    assert got == verdict, (
        f"corpus says {verdict}, guard_command says {got}: {cmd}\n"
        "Update the corpus to what the code does — do not adjust the code to "
        "flatter the corpus."
    )


#: Allowlisted programs that can execute code the caller supplies, or invoke
#: another program. Each is a complete bypass of the allowlist's rules.
INTERPRETERS = {
    "python": "-c runs arbitrary source",
    "python3": "-c runs arbitrary source",
    "node": "-e runs arbitrary source",
    "awk": "BEGIN blocks run, and `|getline` spawns a shell",
    "sed": "GNU `e` command executes its argument",
    "find": "-exec runs any program with any arguments",
    "git": "-c core.pager=… and the alias mechanism both execute",
    "pytest": "loads plugins and executes the repo's own code by design",
    "go": "`go run` compiles and executes a file in the repo",
    "cargo": "`cargo run` compiles and executes the crate",
}

#: The rest: they read or transform data and cannot execute caller-supplied
#: code. Being on this list is a CLAIM, and the corpus is where it is tested.
NON_INTERPRETERS = {
    "ls", "cat", "head", "tail", "wc", "file", "stat", "du",
    "grep", "rg", "ag", "sort", "uniq", "cut", "diff", "tree",
    "which", "echo",
}


def test_every_allowlisted_program_is_classified():
    """A new interpreter must be declared, not silently added.

    This is R3's RED-CHECK: adding `ruby` (or `perl`, or `bash`) to
    `_ALLOWED_PROGRAMS` fails here and names it, because it is neither declared
    an interpreter nor claimed to be safe.
    """
    aw = _guard()
    programs = set(aw._ALLOWED_PROGRAMS)
    unclassified = sorted(programs - set(INTERPRETERS) - NON_INTERPRETERS)
    assert not unclassified, (
        f"allowlisted program(s) with no classification: {unclassified}\n\n"
        "Add it to INTERPRETERS (with how it executes code) or to "
        "NON_INTERPRETERS. Ten of the twenty-eight allowed programs are "
        "general-purpose interpreters; an eleventh arriving unnoticed is how "
        "that happened."
    )
    stale = sorted((set(INTERPRETERS) | NON_INTERPRETERS) - programs)
    assert not stale, f"classified program(s) no longer on the allowlist: {stale}"


def test_the_interpreter_count_is_what_security_md_says():
    """SECURITY.md quotes a count. It must be derived, not remembered."""
    aw = _guard()
    n = len(set(INTERPRETERS) & set(aw._ALLOWED_PROGRAMS))
    assert n == 10, (
        f"{n} interpreters are on the allowlist; SECURITY.md says 10. Update "
        "the document in the same commit that changes the list."
    )
    text = SECURITY.read_text(encoding="utf-8")
    assert f"{n} of the" in text or f"**{n}**" in text, (
        "SECURITY.md no longer states the interpreter count"
    )


#: Claims that cannot survive the question "does the allowlist prevent
#: arbitrary code execution?" — the honest answer is NO.
FORBIDDEN_CLAIMS = (
    "the allowlist prevents",
    "cannot execute arbitrary",
    "sandboxed execution",
    "is a containment boundary",
)


def test_security_md_makes_no_claim_the_allowlist_cannot_support():
    text = SECURITY.read_text(encoding="utf-8").lower()
    found = [c for c in FORBIDDEN_CLAIMS if c in text]
    assert not found, (
        f"SECURITY.md contains claim(s) the allowlist does not support: {found}"
    )


def test_security_md_states_the_honest_answer():
    """Absence of a false claim is not presence of a true one."""
    text = SECURITY.read_text(encoding="utf-8")
    for required in (
        "arbitrary code",          # says what an agent with run_command can do
        "not a containment",       # names what the allowlist is not
    ):
        assert required.lower() in text.lower(), (
            f"SECURITY.md does not state {required!r}. Removing the false "
            "claim without stating the true one leaves a reader with the same "
            "wrong impression and no sentence to argue with."
        )
