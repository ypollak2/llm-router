"""Structural grounding — does a routed draft cite things that exist?

Lived in `hooks/auto-route.py` until 13.3.0, which made it unreachable to anything
else: the hook's filename is hyphenated, so `llm_router.hooks.auto-route` is not a
valid module path and even its own tests had to load it with
`spec_from_file_location`. The gateway could not import it at all, so the one
capability this project has that no competitor does was locked inside a file only
one caller could reach.

The checks are deliberately narrow, and stay that way. They do not judge whether a
draft is RIGHT — a judge that is wrong is worse than no judge. They settle the
claims that can be settled without asking another model: a cited file either exists
or it does not; a called function is either in the index, the context, the prompt,
or nowhere.

Both fail OPEN. A bug in a guard that silently stops all routing looks exactly like
the regression this project spent a week fixing.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# S2-6. Paths the draft asserts must be traceable to something real. Mirrors
# okf._FILE_PAT: the store restricts itself to verified structure for the same
# reason — a path is checkable, prose is not.
_DRAFT_PATH_RE = re.compile(
    r"(?:^|[\s`'\"(\[])([\w./-]*[\w-]/[\w./-]*\w\.(?:py|ts|tsx|js|jsx|go|rs|java|md|json|toml|ya?ml|sh))\b"
)

# A filename with no directory was never checked, because the pattern above
# requires a "/". Measured 2026-09-15 across 88 real drafts: 22 bare filenames
# were cited and 10 of them exist nowhere in the repo — `router.json`,
# `llm-router.yaml`, `30_CI_GAP_PLAN.md`. That is the same fabrication the path
# check exists to catch, wearing a shorter name.
_DRAFT_BARE_FILE_RE = re.compile(
    r"(?:^|[\s`'\"(\[])([\w-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|md|json|toml|ya?ml|sh))\b"
)

# Names too generic to accuse anyone of inventing. A draft saying "add it to
# package.json" is describing a convention, not claiming this repo has the file.
_GENERIC_FILENAMES = frozenset({
    "package.json", "package-lock.json", "tsconfig.json", "setup.py", "setup.cfg",
    "pyproject.toml", "requirements.txt", "readme.md", "license.md", "makefile",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml", ".env",
    "conftest.py", "__init__.py", "index.js", "index.ts", "main.py", "app.py",
    "config.yaml", "config.yml", "config.json", "settings.py", "cargo.toml",
    "go.mod", "go.sum", "changelog.md", "contributing.md", "claude.md",
})


def _file_exists_in_repo(name: str, root: str | None = None) -> bool:
    """Does a file with this basename exist anywhere in the working tree?

    `git ls-files` respects .gitignore, so a match inside .venv cannot ground a
    filename the project does not have. Untracked files are included because a
    file written minutes ago is exactly the case worth admitting.
    """
    cached = _REPO_FILE_CACHE.get(name)
    if cached is not None:
        return cached
    found = False
    try:
        import subprocess

        r = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*/" + name, name],
            cwd=root or None, capture_output=True, text=True, timeout=3.0,
        )
        found = r.returncode == 0 and bool(r.stdout.strip())
    except Exception:                                        # noqa: BLE001
        found = False
    _REPO_FILE_CACHE[name] = found
    return found


_REPO_FILE_CACHE: dict[str, bool] = {}


def grounding_violations(draft: str, context: str, prompt: str = "") -> list[str]:
    """File paths the draft names that appear in neither its inputs nor the repo.

    Every other item in Stage 2 makes the routed model more willing to answer, and
    S2-5 flips the failure mode: a model with no context refuses, which is safe; a
    model with the WRONG context answers just as fluently about the wrong thing.
    This catches the mechanical version of that — the draft citing a file nobody
    mentioned and that does not exist. It is the shape of the 2026-09-06 failure,
    where routed reviews "listed tests that do not exist".

    Deliberately narrow. It does not judge whether the draft is RIGHT; a judge that
    is wrong is worse than no judge. It checks the one claim that can be settled
    without asking another model.
    """
    if not draft:
        return []
    haystack = f"{context or ''}\n{prompt or ''}"
    out: list[str] = []
    bare = [m.group(1) for m in _DRAFT_BARE_FILE_RE.finditer(draft)
            if m.group(1).lower() not in _GENERIC_FILENAMES]
    for m in list(_DRAFT_PATH_RE.finditer(draft)) + list(_DRAFT_BARE_FILE_RE.finditer(draft)):
        path = m.group(1)
        if "/" not in path and (path.lower() in _GENERIC_FILENAMES
                                or path not in bare):
            continue
        # `a/` and `b/` are git's diff prefixes, not directories. A draft quoting a
        # diff of a file that IS in context was being reported as citing two
        # invented paths, which would have rejected a correct answer.
        if path[:2] in ("a/", "b/"):
            path = path[2:]
        if path in haystack:
            continue
        try:
            # Existing on disk is evidence too: the model may have been shown the
            # file in an earlier turn that has since fallen out of the budget.
            if Path(path).exists():
                continue
        except OSError:
            pass
        # A bare filename has no directory to resolve against, so ask the repo
        # whether a file of that name exists ANYWHERE before calling it invented.
        if "/" not in path and _file_exists_in_repo(path):
            continue
        if path not in out:
            out.append(path)
    return out


# S5. A CALL, not a word before a bracket. Two requirements, both learned by
# measuring against real model output rather than assumed:
#
#   * the paren must be adjacent. `\s*\(` matched ordinary English — "threads (or
#     processes)" and "keywords (from the prompt)" were both reported as invented
#     functions, a 2-in-3 false-positive rate on real answers.
#   * the name must look like an identifier: an underscore, or internal capitals.
#     A bare lowercase word before a paren is prose far more often than it is code,
#     and a guard that rejects correct answers costs routing silently — the
#     fallthrough is indistinguishable from a model that simply did not answer.
_DRAFT_SYMBOL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:_[A-Za-z0-9_]+|[a-z][A-Z][A-Za-z0-9_]*))\(")

# The second form, added after the call shape missed a live fabrication. Probing
# the real MCP tool produced: retrieval correct (`src/llm_router/okf.py` is right,
# and only OKF could have supplied it), then two invented callers —
# `write_concept` and `write_concept_from_mcp`, neither of which exists. Both were
# written as fenced prose rather than calls, so the paren requirement never saw
# them.
#
# A fenced identifier is an explicit code claim: nobody writes `write_concept` in
# backticks meaning an English word. Same identifier-shape requirement as the call
# form, plus two exclusions that matter in practice:
#
#   * anything containing "/" or "." is a path or a dotted attribute — the path
#     check owns those, and double-reporting one mistake as two is noise;
#   * ALL_CAPS is an env var or a module constant, which is the most common
#     backticked identifier-shaped token in this project's own writing. Flagging
#     `LLM_ROUTER_PROJECT_ROOT` would reject correct answers about configuration.
_DRAFT_FENCED_SYMBOL_RE = re.compile(
    r"`([A-Za-z_][A-Za-z0-9_]*(?:_[A-Za-z0-9_]+|[a-z][A-Z][A-Za-z0-9_]*))`"
)

# Words that appear with parens in ordinary writing and in shell, and would
# otherwise be read as invented functions. `test()`, `build()` and `run()` are
# English before they are identifiers.
_SYMBOL_NOISE = frozenset({
    "and", "build", "check", "def", "deploy", "elif", "for", "fix", "function",
    "get", "here", "http", "https", "if", "install", "int", "json", "list", "log",
    "not", "note", "open", "print", "return", "run", "set", "sudo", "test", "the",
    "this", "try", "update", "use", "using", "while", "with", "yaml",
})


def known_symbols() -> set[str]:
    """Every symbol name the OKF index knows for this project.

    Only meaningful once `okf index` has run. An empty set means "nothing is
    checkable", never "everything is invented" — see _symbol_violations.
    """
    out: set[str] = set()
    try:
        from llm_router import okf as _okf

        for concept in _okf._get_bundle():
            for sym in concept.extra.get("key_symbols") or []:
                out.add(str(sym))
    except Exception:  # noqa: BLE001
        return set()
    return out


def symbol_violations(draft: str, context: str, prompt: str = "") -> list[str]:
    """Functions/classes the draft calls that exist nowhere checkable.

    S2-6 validates paths; this validates the other half of the verified structure
    `okf index` now holds. A draft sounds specific when it cites
    `reconcile_invoice_totals()`, and specificity is exactly what makes a fabricated
    answer persuasive.

    Deliberately NOT a fix for the U7-class failure, which invented prose and named
    no symbols at all — nothing structural can catch that, which is why S2-5b fixed
    it at the gate. This covers the case in between.

    A symbol in the index, the context, or the prompt is grounded. Only a name in
    none of them is a violation, and only when the index is populated: unknown is
    not invented, and unknown must never reject.
    """
    if not draft:
        return []
    # Only meaningful when the answer is ABOUT the indexed project. The index knows
    # this repository's symbols and nothing else, so absence from it is not evidence
    # of non-existence — measured on real output, a mutex/semaphore answer naming
    # `pthread_mutex_lock` and `sem_init` was reported as inventing them, and a
    # question about configuration was rejected for naming `load_dotenv`. Those are
    # real functions; they are simply not in this repo.
    #
    # Retrieval having fired is the signal that the model was answering about this
    # codebase. With no injected knowledge, the draft is general-purpose and the
    # index has no standing to judge the names in it.
    if "<knowledge_context>" not in (context or ""):
        return []
    try:
        known = known_symbols()
    except Exception:  # noqa: BLE001
        return []
    if not known:
        return []  # nothing indexed → nothing checkable
    haystack = f"{context or ''}\n{prompt or ''}"
    out: list[str] = []
    names = [m.group(1) for m in _DRAFT_SYMBOL_RE.finditer(draft)]
    names += [
        m.group(1) for m in _DRAFT_FENCED_SYMBOL_RE.finditer(draft)
        if not m.group(1).isupper()   # env var / constant, not a function
    ]
    unresolved: list[str] = []
    for name in names:
        if name.lower() in _SYMBOL_NOISE or name in known or name in haystack:
            continue
        if name not in unresolved:
            unresolved.append(name)
    # Last escape before calling a name invented: look on DISK. The index is a
    # snapshot and the working tree is the truth, so a symbol written five minutes
    # ago is absent from the index and present in the repo. Measured 2026-09-14: a
    # function created seconds earlier was reported as a violation while the index
    # held 11,697 symbols, so a correct draft was rejected and the turn escalated
    # to a premium model. `grounding_violations` has had exactly this escape since
    # S2-6 ("Existing on disk is evidence too"); the symbol half never got it.
    #
    # Re-indexing at session start does NOT fix this. The gap is WITHIN a session:
    # write the function at 14:20, ask about it at 14:21, and any index built
    # beforehand is already stale.
    on_disk = _defined_on_disk(unresolved)
    out.extend(n for n in unresolved if n not in on_disk)
    return out


def _defined_on_disk(names: list[str], root: str | None = None) -> set[str]:
    """Which of *names* are defined in the working tree right now.

    ONE `git grep` for the whole set, not one per symbol: the cost here is process
    spawn, so eight names in a draft must not mean eight subprocesses (measured:
    893ms that way, ~110ms batched).

    `--untracked` matters as much as the batching. A file written five minutes ago
    is exactly the case this exists for, and it is usually not committed yet; plain
    `git grep` searches only tracked content and would still reject it.
    `.gitignore` is still respected, so a match inside `.venv` cannot ground a
    symbol the project does not define.

    Fails to the EMPTY set on any error, so the caller falls back to the index
    verdict — the pre-existing behaviour, never something looser.
    """
    wanted = [n for n in names if n and n.isidentifier()]
    if not wanted:
        return set()
    known = {n: _DISK_SYMBOL_CACHE[n] for n in wanted if n in _DISK_SYMBOL_CACHE}
    todo = [n for n in wanted if n not in known]
    found = {n for n, hit in known.items() if hit}
    if not todo:
        return found
    try:
        import subprocess

        # POSIX ERE, not PCRE. `git grep -E` rejects `(?:...)` outright with
        # "repetition-operator operand invalid" — and because the failure is a
        # non-zero exit rather than an exception, a swallowed error here looks
        # exactly like "no symbol found" and silently restores the bug this
        # function exists to fix. Hence the explicit returncode check below.
        alternation = "|".join(re.escape(n) for n in todo)
        # Scoped to source extensions. `--untracked` otherwise walks every
        # untracked file in the tree — on this repo that included stray data/
        # and experiments/ directories and cost 3.3s, against a first-model
        # budget of ~37s. With the pathspec it is ~0.2s.
        r = subprocess.run(
            ["git", "grep", "--untracked", "-hoE",
             rf"(def|class)[[:space:]]+({alternation})",
             "--", "*.py", "*.pyi", "*.ts", "*.tsx", "*.js", "*.jsx",
             "*.go", "*.rs", "*.java", "*.rb"],
            cwd=root or None, capture_output=True, text=True, timeout=5.0,
        )
        if r.returncode not in (0, 1):                       # 1 == no matches
            raise RuntimeError(f"git grep failed: {(r.stderr or '').strip()[:120]}")
        hits = {line.strip().split()[-1] for line in r.stdout.splitlines()
                if line.strip()} & set(todo)
        for n in todo:
            _DISK_SYMBOL_CACHE[n] = n in hits
        found |= hits
    except Exception as exc:                                 # noqa: BLE001
        try:
            from llm_router import failopen
            failopen.record("CHZ-FO-GROUNDING-DISK-SYMBOL", exc)
        except Exception:                                    # noqa: BLE001
            pass
    return found


# Per-process only. The hook is a fresh process per prompt, so this caches within
# one draft and can never go stale across turns.
_DISK_SYMBOL_CACHE: dict[str, bool] = {}


# A draft is written into session memory and becomes CONTEXT for the next turn
# (auto-route.py records it as an assistant event, session_store always injects
# the 3 newest). Until 2026-09-14 the only gate on that write was file/symbol
# grounding, which has no opinion about a draft that asks a question or claims an
# action. So a fabricated status became the next turn's ground truth and the
# model escalated its own invention across turns — observed in real session data:
# "63.2% complete (5,309/8,400)" became "78.5% complete (6,600/8,400)" one turn
# later, for a project that does not exist.
#
# Grounding catches an invented FILE. This catches an invented ACTION, which is
# the more dangerous of the two because nothing else looks for it.
_UNOBSERVABLE_CLAIM = re.compile(
    r"all tests? (pass|passed)|completed successfully|"
    r"(have|has|i) (been )?(merged|pushed|committed|deployed|installed)|"
    r"✅|task .{0,20}(complete|done)|successfully (ran|executed|created|updated)",
    re.I)
_DEFERRAL = re.compile(
    r"would you like me to|shall i |let me know if|do you want me to|"
    r"could you (share|provide|clarify|confirm)|please (share|provide|clarify)",
    re.I)


def response_is_usable(text: str) -> bool:
    """Did this response actually answer, or merely arrive?

    The routing bandit's reward is ``success_rate / avg_cost`` and every write
    site passed ``success=True`` unconditionally — there was no ``success=False``
    anywhere on the routing path. So the reward collapsed to ``1 / avg_cost``:
    ranking by cheapness with a constant numerator, learning nothing about
    quality. An empty string logged success too.

    Measured 2026-09-14 on 200 real prompts: of 144 drafts produced, 35 were
    unusable — 20 asked the user a question instead of answering and 10 claimed an
    action they could not have performed. Under the old signal each of those
    reinforced the model that produced it.

    This is deliberately the SAME predicate as :func:`draft_is_memorable`, so
    "worth remembering" and "counts as success" cannot drift apart.
    """
    ok, _ = draft_is_memorable(text)
    return ok


def draft_is_memorable(text: str) -> tuple[bool, str]:
    """Is this draft safe to persist as an assistant turn for future context?

    Stricter than :func:`draft_is_relayable`, and deliberately so: relaying a
    weak draft costs one turn, remembering one costs every turn after it.
    Returns (ok, reason-if-not).
    """
    body = (text or "").strip()
    # 20, not 40: a correct answer can be short ("os.path.join joins path
    # components." is 35 chars and worth remembering). The real signals are the
    # two below — an invented action and a deferral — not length. This floor only
    # drops bare acknowledgements like "Sure."
    if len(body) < 20:
        return False, "too short to be worth remembering"
    if _UNOBSERVABLE_CLAIM.search(body):
        return False, "claims an action it could not have performed"
    if _DEFERRAL.search(body):
        return False, "defers to the user rather than answering"
    return True, ""


def draft_is_relayable(draft: str, context: str, prompt: str = "") -> bool:
    """Whether a DIRECT draft may be shown, or should fall through to Claude.

    Fail-open on any internal error: a bug in the guard must not silently stop all
    routing, which would look exactly like the regression this branch is fixing.
    `LLM_ROUTER_GROUNDING_CHECK=off` disables it.
    """
    if os.environ.get("LLM_ROUTER_GROUNDING_CHECK", "on").strip().lower() in (
        "0", "off", "false", "no"
    ):
        return True
    try:
        if grounding_violations(draft, context, prompt):
            return False
        # Symbol checking has its own switch: path checking is cheap and certain,
        # while this depends on the OKF index being present and reasonably fresh.
        if os.environ.get("LLM_ROUTER_SYMBOL_GROUNDING", "on").strip().lower() in (
            "0", "off", "false", "no"
        ):
            return True
        return not symbol_violations(draft, context, prompt)
    except Exception:  # noqa: BLE001
        return True

# S2-5. A gated prompt is routable when its reference can be RESOLVED, not merely
# when it names something. Minimums below are what separates "there is an exchange
# to resolve against" from "there is a scrap that invites a confident wrong guess";
# without context a model refuses, which is safe, so thin context is strictly worse
# than none.
