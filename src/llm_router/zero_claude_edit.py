"""Scoped zero-Claude for edit-class prompts (``LLM_ROUTER_ZERO_CLAUDE_SCOPE=edit``).

The owner chose this scope explicitly (2026-09-28) to move the North Star metric
now, without turning on full zero-Claude (``LLM_ROUTER_ZERO_CLAUDE=1``, which
blocks EVERY unrouted turn). ``LLM_ROUTER_ZERO_CLAUDE_SCOPE=edit`` narrows the
same fail-closed contract to prompts this module can classify as a concrete
change to named files that already exist in the current repo — a "rename X to Y
in foo.py" shape, not "refactor the whole codebase" or a question. Everything
else is untouched: it falls through to the normal routing flow exactly as if
the scope variable were unset.

WHY THIS RUNS IN UserPromptSubmit, NOT AS A PreToolUse DENY
-------------------------------------------------------------
A PreToolUse deny can BLOCK a tool call, but it cannot DELIVER a result: the
model treats substituted text as prompt injection and either refuses it or
retries the original call (measured, 3 controlled runs — see
``docs/`` for the write-up this repeats). A UserPromptSubmit block, by
contrast, is shown to the user directly as the reason string. So the edit has
to happen HERE, inside the hook, before Claude ever sees the prompt: this
module performs the edit itself (reads the files, calls the local model,
validates and writes the result) and then blocks the turn with a message
describing what it did.

DECISION SHAPE
---------------
``maybe_replace()`` returns ``None`` only in the fast path where the scope
variable is unset — zero behavioural change, and cheap enough to check on
every invocation. Once ``LLM_ROUTER_ZERO_CLAUDE_SCOPE=edit`` is set, it always
returns a :class:`ScopedEditOutcome` so the caller can log WHY, matching the
"every branch that can skip routing logs why" invariant the rest of
``auto-route.py`` already holds (``tests/test_routing_outcome_logged.py``).

Two outcome actions:

``fallthrough``
    Claude handles the turn normally — not edit-class, the ``claude:`` escape
    hatch was used, no named file could be resolved, the target has
    uncommitted changes, or the quality breaker for this class is open. None
    of these are failures of the edit attempt; they are reasons no attempt
    was made.

``block``
    The turn is intercepted. ``applied=True`` means the edit was generated,
    validated (exact-once ``old_string`` match, syntax-checked — see
    ``edit.apply_edits``), lint-verified on a temp copy (plan 3.3, see
    ``local_agent.verify``) and WRITTEN to disk; the block message carries
    the model, the changed files and a short diff. ``applied=False`` means
    an edit was attempted and failed (could not be produced, validated, or
    verified) or a named file resolved outside the repo root — per the
    owner's explicit choice, failure blocks rather than silently falling
    through, and never half-applies (``edit.apply_edits`` is already
    all-or-nothing, and the lint gate runs before any write at all).

Every ``block`` message ends with the same escape hatch as full zero-Claude:
prefix the prompt with ``claude:`` to redo the turn natively.
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Mirrors hooks/auto-route.py._EXPLICIT_CLAUDE_PREFIX_RE exactly (that module
# cannot be imported here — it is loaded by file path, not as a package, and
# importing it would pull in the entire 5000-line hook for one regex). Native
# use is always explicit: the prefix stays in the prompt as a visible record.
_EXPLICIT_CLAUDE_PREFIX_RE = re.compile(r"^\s*(?:claude|native|opus)\s*:\s*", re.IGNORECASE)

# ── edit-class classifier ────────────────────────────────────────────────────
#
# Conservative on purpose: this class of prompt gets a real, unsupervised file
# write with no Claude review before it lands. Two independent signals must
# both hold — an imperative EDIT VERB and at least one NAMED, real-looking
# source file — and a prompt with too many named files (probably a repo-wide
# request) or that reads as a question is rejected rather than guessed at.
#
# The file-reference pattern is the same shape as okf.py's `_FILE_PAT` (the
# existing signal for "a real file path, not prose that happens to end in a
# dot" — see its docstring: "fix the webhook backoff in retry.py" ends in
# ``.py`` without being a file reference on its own; requiring a preceding
# boundary and a known extension is what makes it a path and not a sentence).
_EDIT_VERBS = (
    "rename", "add", "fix", "update", "remove", "delete", "replace",
    "insert", "refactor", "change", "modify", "append", "prepend",
    "correct", "implement", "extract", "inline", "split", "merge",
    "reformat", "rewrite",
)
_EDIT_VERB_RE = re.compile(r"\b(?:" + "|".join(_EDIT_VERBS) + r")\b", re.IGNORECASE)

# Interrogative / explanatory openers read as a question about code, not a
# change request — even one that happens to name a file ("what does
# router.py do?"). Checked before the verb match so a prompt that both asks
# a question AND contains an edit verb ("how do I fix router.py?") is still
# treated as a question, the safer read.
_QUESTION_RE = re.compile(
    r"^\s*(?:what|why|how|when|where|who|which|is|are|do|does|did|can|could|"
    r"should|would|explain|describe|tell me|show me|list|summarize|summarise)\b",
    re.IGNORECASE,
)

_CODE_EXTENSIONS = (
    "py", "ts", "tsx", "js", "jsx", "go", "rs", "java", "rb", "c", "cpp",
    "h", "hpp", "cs", "php", "swift", "kt", "sh", "yaml", "yml", "json",
    "toml", "cfg", "ini", "md",
)
_FILE_REF_RE = re.compile(
    r"(?:^|[\s`'\"(])([\w][\w./\-]*\.(?:" + "|".join(_CODE_EXTENSIONS) + r"))\b"
)

# A prompt naming more than this many files reads as "the whole codebase",
# not a scoped edit a single local-model call can safely produce and
# validate in one shot. Conservative rejection, not a hard technical limit.
_MAX_TARGET_FILES = 3

# ── identifier candidates (no literal path in the prompt) ────────────────────
#
# Real users name a function, class or module more often than a path
# ("count_vowels ignores uppercase, fix it"). When no literal file is named,
# the classifier hands on CODE-SHAPED identifiers only — backticked names,
# call-shaped ``name()``, snake_case, camelCase/PascalCase — never a plain
# English word, which could match anything. Resolution to a file happens
# later against the repo (``resolve_symbol_targets``), and only on a unique
# definition.
_URL_RE = re.compile(r"\S+://\S+")
_BACKTICK_IDENT_RE = re.compile(r"`([A-Za-z_][\w.]*?)(?:\(\))?`")
_CALL_IDENT_RE = re.compile(r"\b([A-Za-z_]\w*)\(\)")
_SNAKE_IDENT_RE = re.compile(r"(?<![\w./-])(_?[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)(?![\w/-])")
_CAMEL_IDENT_RE = re.compile(r"(?<![\w./-])([A-Z]?[a-z]+(?:[A-Z][a-z0-9]+)+)(?![\w/-])")
# More distinct identifiers than this reads as pasted content (a traceback,
# a spec) rather than "fix this one function" — conservative rejection.
_MAX_SYMBOL_CANDIDATES = 5
# Inferring a target (from an identifier, or a bare file name with no
# directory) is only attempted on short prompts. Without this cap, one
# machine's real edit-like prompts (2026-09-28, n=122 in a live repo) gained
# 14 inferred targets, 11 of them wrong — every one of the 11 from a prompt
# over 300 chars (a pasted log, a spec, or injected context whose markdown
# carried a class name). The cap was chosen on that same sample; it is a
# conservative guard, not a held-out-validated threshold.
_MAX_INFER_PROMPT_CHARS = 300


@dataclass(frozen=True)
class EditClassification:
    is_edit: bool
    files: tuple[str, ...]
    reason: str
    # Code-shaped identifiers, set only when ``files`` is empty: the caller
    # resolves them to files against the repo (``resolve_symbol_targets``).
    symbols: tuple[str, ...] = ()


def _identifier_candidates(text: str) -> tuple[str, ...]:
    stripped = _URL_RE.sub(" ", text)
    found: list[str] = []
    for m in _BACKTICK_IDENT_RE.finditer(stripped):
        found.append(m.group(1).rsplit(".", 1)[-1])
    for rx in (_CALL_IDENT_RE, _SNAKE_IDENT_RE, _CAMEL_IDENT_RE):
        found.extend(m.group(1) for m in rx.finditer(stripped))
    return tuple(dict.fromkeys(n for n in found if n and not n.isdigit()))


def classify_edit_prompt(prompt: str) -> EditClassification:
    """Conservative edit-class classifier: a concrete change to named files.

    ``is_edit`` is True only when the prompt reads as an imperative change
    AND either names 1-3 real-looking source files, or — with no path at
    all — carries 1-5 code-shaped identifiers ("add a docstring to bar()")
    and no question mark. Identifiers are only CANDIDATES: ``maybe_replace``
    resolves them with ``resolve_symbol_targets``, which accepts a unique
    definition and falls through on anything ambiguous.
    """
    text = (prompt or "").strip()
    if not text:
        return EditClassification(False, (), "empty prompt")
    if _QUESTION_RE.match(text):
        return EditClassification(False, (), "interrogative — reads as a question, not a change request")
    if not _EDIT_VERB_RE.search(text):
        return EditClassification(False, (), "no imperative edit verb found")
    files = tuple(dict.fromkeys(
        m.group(1).lstrip("./") for m in _FILE_REF_RE.finditer(text)
    ))
    if not files:
        # No literal path: fall back to code-shaped identifiers, resolved
        # against the repo later. Stricter than the path branch: any "?"
        # reads as a question ("could you fix count_vowels or is it fine?"),
        # and a pile of identifiers reads as pasted content.
        if "?" in text:
            return EditClassification(False, (), "no named file, and the prompt contains a question mark")
        if len(text) > _MAX_INFER_PROMPT_CHARS:
            return EditClassification(
                False, (), f"no named file, and the prompt is too long ({len(text)} chars) to infer a "
                f"target from identifiers (max {_MAX_INFER_PROMPT_CHARS})",
            )
        symbols = _identifier_candidates(text)
        if not symbols:
            return EditClassification(False, (), "no named file or code identifier in the prompt")
        if len(symbols) > _MAX_SYMBOL_CANDIDATES:
            return EditClassification(
                False, (), f"no named file and {len(symbols)} identifiers — too broad "
                f"(max {_MAX_SYMBOL_CANDIDATES})",
            )
        return EditClassification(True, (), f"edit verb + {len(symbols)} identifier(s), no path", symbols)
    if len(files) > _MAX_TARGET_FILES:
        return EditClassification(
            False, files,
            f"names {len(files)} files — too broad for scoped zero-Claude (max {_MAX_TARGET_FILES})",
        )
    return EditClassification(True, files, f"edit verb + {len(files)} named file(s)")


# ── safety: repo root, containment, dirty-tree check ─────────────────────────

def _repo_root(cwd: str) -> Path | None:
    """The git repo root for *cwd*, or None when it is not inside one.

    Resolved via ``git rev-parse``, not a ``.git``-ancestor walk, so it is
    the same notion of "repo" that the dirty-tree check below (``git
    status``) uses — the two must agree, or a file could pass containment
    against one root and be checked for cleanliness against another.
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
            env=os.environ.copy(),  # hardcoded argv, no credential-leak risk (R4 scope)
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return Path(top) if top else None


def _tracked_files(repo_root: Path) -> list[str] | None:
    """``git ls-files`` for *repo_root*, or None when git cannot answer."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            capture_output=True, text=True, timeout=5,
            env=os.environ.copy(),  # hardcoded argv, no credential-leak risk (R4 scope)
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return [p for p in result.stdout.split("\0") if p]


def resolve_target_files(
    files: tuple[str, ...], repo_root: Path, *, infer_bare: bool = True,
) -> tuple[list[str], list[str]]:
    """Resolve prompt-named files against *repo_root*.

    With *infer_bare*, a bare file name with no directory ("helpers.py")
    that is not at the repo root resolves to the one TRACKED file with that
    basename — only when exactly one exists; two ``helpers.py`` means we do
    not know which was meant, and the name is dropped like any other
    not-found name.

    Returns ``(resolved, problems)``. A name that does not exist on disk is
    simply dropped — not found is not a security issue, just nothing to act
    on (the caller falls through to Claude when ``resolved`` ends up empty).
    A name that resolves OUTSIDE ``repo_root`` — an absolute path elsewhere,
    or ``../`` traversal — is the one case worth naming loudly: it goes into
    ``problems`` and the caller blocks rather than silently skipping it.
    """
    root = repo_root.resolve()
    resolved: list[str] = []
    problems: list[str] = []
    tracked: list[str] | None = None
    for raw in files:
        candidate = (root / raw).resolve()
        try:
            rel = candidate.relative_to(root)
        except ValueError:
            problems.append(f"{raw}: resolves outside the repo root")
            continue
        if not candidate.is_file():
            if "/" in raw or not infer_bare:
                continue
            if tracked is None:
                tracked = _tracked_files(root) or []
            matches = [p for p in tracked if p.rsplit("/", 1)[-1] == raw]
            if len(matches) != 1 or not (root / matches[0]).is_file():
                continue
            rel = Path(matches[0])
        resolved.append(str(rel))
    return resolved, problems


# Definition shapes, per language, for a name already known to occur as a
# whole word. A line that merely CALLS or imports the name (a test, a caller)
# is not a definition, so it never makes a name look ambiguous — nor does it
# become the edit target.
_DEFINITION_TEMPLATES = (
    r"^\s*(?:async\s+)?def\s+{n}\s*[\(\[]",                                  # Python
    r"^\s*class\s+{n}\b",                                                     # Python/Ruby
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s+{n}\s*[\(<]",  # JS/TS
    r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?:class|interface|type|enum)\s+{n}\b",
    r"^\s*(?:export\s+)?(?:const|let|var)\s+{n}\s*=\s*(?:async\s*)?(?:function\b|\()",
    r"^\s*func\s+(?:\([^)]*\)\s*)?{n}\s*[\(\[]",                               # Go
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn|struct|enum|trait)\s+{n}\b",  # Rust
)
# Code files only: a ``def foo():`` inside a Markdown example or a YAML string
# is not the thing to edit.
_DEFINITION_PATHSPECS = tuple(
    f"*.{ext}" for ext in _CODE_EXTENSIONS
    if ext not in ("md", "json", "yaml", "yml", "toml", "cfg", "ini")
)


def resolve_symbol_targets(symbols: tuple[str, ...], repo_root: Path) -> tuple[list[str], str]:
    """Resolve prompt identifiers to the TRACKED files that define them.

    Each identifier maps to the files where it is defined (a def/class/func
    line, via ``git grep`` over tracked code files) plus any tracked code file
    whose stem is that name (a bare module name, "the zero_claude_edit
    module"). Conservative on purpose — a wrong-file edit is worse than a
    fallthrough:

    * an identifier that maps to MORE THAN ONE file makes the whole prompt
      ambiguous -> ``[]``, even if another identifier is unique;
    * identifiers that map to nothing are ignored (most candidates are
      prose-adjacent names that are simply not in this repo);
    * more than ``_MAX_TARGET_FILES`` files in total -> ``[]``.

    Returns ``(files, reason)``; ``reason`` names each ``symbol -> file``
    mapping on success, or why nothing was resolved.
    """
    if not symbols:
        return [], "no identifiers to resolve"
    argv = ["git", "-C", str(repo_root), "grep", "-I", "-n", "--null", "-w", "-F"]
    for sym in symbols:
        argv += ["-e", sym]
    argv += ["--", *_DEFINITION_PATHSPECS]
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=5,
            env=os.environ.copy(),  # fixed argv shape, no credential-leak risk (R4 scope)
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], "git grep failed — cannot resolve identifiers safely"
    if result.returncode not in (0, 1):  # 1 = no match
        return [], "git grep failed — cannot resolve identifiers safely"

    patterns = {
        sym: [re.compile(t.format(n=re.escape(sym))) for t in _DEFINITION_TEMPLATES]
        for sym in symbols
    }
    hits: dict[str, set[str]] = {sym: set() for sym in symbols}
    for line in result.stdout.splitlines():
        path, sep, rest = line.partition("\0")
        if not sep:
            continue
        _lineno, _, content = rest.partition("\0")
        for sym, rxs in patterns.items():
            if sym in content and any(rx.search(content) for rx in rxs):
                hits[sym].add(path)

    code_exts = {p[2:] for p in _DEFINITION_PATHSPECS}
    for path in _tracked_files(repo_root) or []:
        base = path.rsplit("/", 1)[-1]
        stem, dot, ext = base.rpartition(".")
        if dot and ext in code_exts and stem in hits:
            hits[stem].add(path)

    ambiguous = {s: sorted(f) for s, f in hits.items() if len(f) > 1}
    if ambiguous:
        detail = "; ".join(f"{s} in {len(f)} files" for s, f in ambiguous.items())
        return [], f"identifier ambiguous — {detail}"
    mapping = {s: next(iter(f)) for s, f in hits.items() if f}
    if not mapping:
        return [], f"no definition of {list(symbols)} in tracked files"
    files = list(dict.fromkeys(mapping.values()))
    if len(files) > _MAX_TARGET_FILES:
        return [], f"identifiers resolve to {len(files)} files — too broad (max {_MAX_TARGET_FILES})"
    return files, ", ".join(f"{s} -> {f}" for s, f in mapping.items())


def dirty_files(repo_root: Path, relpaths: list[str]) -> list[str]:
    """Which of *relpaths* have uncommitted changes (staged, unstaged, or
    untracked) per ``git status --porcelain``.

    Fails SAFE: if git cannot be run or errors, every path is reported dirty
    — the caller treats that as "cannot verify cleanliness, don't touch it",
    never as "assume clean".
    """
    if not relpaths:
        return []
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--", *relpaths],
            capture_output=True, text=True, timeout=5,
            env=os.environ.copy(),  # hardcoded argv, no credential-leak risk (R4 scope)
        )
    except (OSError, subprocess.TimeoutExpired):
        return list(relpaths)
    if result.returncode != 0:
        return list(relpaths)
    dirty: set[str] = set()
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path_part = line[3:].strip()
        if " -> " in path_part:  # rename: "old -> new"
            path_part = path_part.split(" -> ", 1)[1]
        dirty.add(path_part.strip('"'))
    return [p for p in relpaths if p in dirty]


# ── model selection + generation loop ────────────────────────────────────────

MAX_EDIT_ATTEMPTS = 3  # mirrors edit.MAX_EDIT_ATTEMPTS
_PER_ATTEMPT_CEILING_S = 30.0
_MIN_ATTEMPT_S = 3.0

_EDIT_SYSTEM_PROMPT = (
    "You are a precise code editor. Return ONLY a JSON array of edit "
    "instructions. No prose, no explanation outside the JSON."
)


def edit_model() -> str:
    """The local model this lever calls — qwen3.5 unless overridden.

    ``LLM_ROUTER_OLLAMA_MODEL`` (the same override every other direct-execution
    path in this hook honours) wins when set. Otherwise the best INSTALLED
    model preferring the qwen3.5 family, via ``model_discovery.first_installed``
    — never a hardcoded name that might not be pulled (that module's own
    docstring: a hardcoded default that is not installed 404s silently).
    """
    override = os.environ.get("LLM_ROUTER_OLLAMA_MODEL", "").strip()
    if override:
        return override
    try:
        from llm_router import model_discovery
        found = model_discovery.first_installed(prefer=("qwen3.5", "qwen3-coder", "qwen3.8"))
        if found:
            return found
    except Exception:                                            # noqa: BLE001
        pass
    return "qwen3.5:latest"


def generate_edits(
    task: str,
    file_contents: dict[str, str],
    model: str,
    deadline_s: float,
) -> tuple[dict[str, str] | None, list, str | None]:
    """Retry loop: ask *model* for edit instructions, validate, retry with
    feedback on rejection — same shape as ``tools/text.py``'s ``llm_edit``,
    run synchronously (the hook has no event loop) via
    ``direct_executor.call_ollama``, which already sends ``think: false``.

    Returns ``(new_contents, instructions, failure_reason)``. On success
    ``new_contents`` is the full ``path -> text`` map with edits applied and
    ``failure_reason`` is None. On failure ``new_contents`` is None and
    ``failure_reason`` names why (parse failure, validation failure, or ran
    out of the hook's time budget).
    """
    from llm_router.edit import apply_edits, build_edit_prompt, parse_edit_response
    from llm_router.hooks.direct_executor import call_ollama
    from llm_router.local_agent.constrain import EDIT_PAIRS_SCHEMA
    from llm_router.warm import edit_keep_alive

    history: list[str] = []
    last_instructions: list = []
    for attempt in range(1, MAX_EDIT_ATTEMPTS + 1):
        remaining = deadline_s - time.monotonic()
        if remaining < _MIN_ATTEMPT_S:
            reason = "; ".join(history) if history else "out of hook time budget before any attempt"
            return None, last_instructions, reason
        call_timeout = max(_MIN_ATTEMPT_S, min(_PER_ATTEMPT_CEILING_S, remaining))

        feedback = history[-1] if history else None
        prompt = build_edit_prompt(task, file_contents, feedback=feedback)
        response, _usage = call_ollama(
            prompt, model, int(round(call_timeout)), system_prompt=_EDIT_SYSTEM_PROMPT,
            format=EDIT_PAIRS_SCHEMA, keep_alive=edit_keep_alive(),
        )
        if not response:
            history.append(f"attempt {attempt}: model returned no response")
            continue

        instructions, parse_warnings = parse_edit_response(response)
        if not instructions:
            history.append(f"attempt {attempt}: " + ("; ".join(parse_warnings) or "no edit instructions in response"))
            continue
        last_instructions = instructions

        new_contents, reject_reasons = apply_edits(file_contents, instructions)
        if new_contents is not None:
            return new_contents, instructions, None
        history.append(f"attempt {attempt}: " + "; ".join(reject_reasons))

    return None, last_instructions, "; ".join(history) if history else "no attempts made"


# ── message formatting ───────────────────────────────────────────────────────

_REDO_HINT = "To redo this with Claude, resubmit the prompt prefixed with `claude:`."


def short_diff(old: str, new: str, label: str, max_lines: int = 12) -> str:
    diff_lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"a/{label}", tofile=f"b/{label}", lineterm="",
    ))
    if not diff_lines:
        return "(no textual change)"
    if len(diff_lines) > max_lines:
        shown = diff_lines[:max_lines]
        shown.append(f"... ({len(diff_lines) - max_lines} more diff line(s))")
        return "\n".join(shown)
    return "\n".join(diff_lines)


def applied_message(model: str, changed_files: list[str], diffs: dict[str, str], inferred: str = "") -> str:
    lines = [f"ZERO_CLAUDE_EDIT APPLIED — model={model}", f"Files changed: {', '.join(changed_files)}"]
    if inferred:
        lines.append(f"Target inferred from: {inferred}")
    lines.append("")
    for f in changed_files:
        lines.append(f"--- {f} ---")
        lines.append(diffs[f])
        lines.append("")
    lines.append(_REDO_HINT)
    return "\n".join(lines)


def failure_message(reason: str) -> str:
    return (
        f"ZERO_CLAUDE_EDIT BLOCKED: {reason}\n\n"
        f"The edit could not be produced or validated, so nothing was changed. {_REDO_HINT}"
    )


# ── orchestration ────────────────────────────────────────────────────────────

@dataclass
class ScopedEditOutcome:
    """What ``maybe_replace`` decided, for the caller (``hooks/auto-route.py``
    ``main()``) to act on."""

    action: str            # "fallthrough" | "block"
    log_reason: str        # always set — what to write to the debug log
    message: str = ""      # set when action == "block": the full BLOCK reason
    applied: bool = False  # meaningful only when action == "block"


def _scope_enabled() -> bool:
    return os.environ.get("LLM_ROUTER_ZERO_CLAUDE_SCOPE", "").strip().lower() == "edit"


def maybe_replace(
    *,
    prompt: str,
    cwd: str,
    deadline_s: float,
) -> ScopedEditOutcome | None:
    """Entry point called from ``hooks/auto-route.py``'s ``main()``.

    Returns ``None`` in the (overwhelmingly common) case where
    ``LLM_ROUTER_ZERO_CLAUDE_SCOPE`` is not ``edit`` — the caller does
    nothing further, exactly as if this module did not exist. Once the scope
    is on, always returns a :class:`ScopedEditOutcome` so every decision is
    logged, matching the rest of this hook's "every skip logs why" contract.
    """
    if not _scope_enabled():
        return None

    if _EXPLICIT_CLAUDE_PREFIX_RE.match(prompt):
        return ScopedEditOutcome("fallthrough", "ZERO_CLAUDE_EDIT: explicit claude: prefix — native use")

    classification = classify_edit_prompt(prompt)
    if not classification.is_edit:
        return ScopedEditOutcome("fallthrough", f"ZERO_CLAUDE_EDIT: not edit-class — {classification.reason}")

    # Kill switch (quality breaker "zero_claude_edit"/"code"): checked before
    # any model call is attempted, same position the "direct" lever's check
    # holds in the main hook — a class that keeps failing stops attempting.
    try:
        from llm_router import quality_breaker
        qb_decision = quality_breaker.should_route("zero_claude_edit", "code")
    except Exception as exc:                                     # noqa: BLE001
        # Fail OPEN on the breaker's own machinery (quality_breaker.py's own
        # documented contract: an unreadable state file must never crash a
        # hook), but the breaker check itself is best-effort here only for
        # that reason — a routing decision is still made either way.
        qb_decision = None
        _breaker_exc = exc
    if qb_decision is not None and not qb_decision.allowed:
        return ScopedEditOutcome("fallthrough", f"ZERO_CLAUDE_EDIT: quality breaker open — {qb_decision.reason}")

    root = _repo_root(cwd)
    if root is None:
        return ScopedEditOutcome("fallthrough", "ZERO_CLAUDE_EDIT: cwd is not inside a git repository")

    inferred = ""
    targets = classification.files
    if not targets:
        # No literal path: the identifiers must resolve to a unique, small set
        # of tracked files, else fall through (and log why).
        sym_files, inferred = resolve_symbol_targets(classification.symbols, root)
        if not sym_files:
            return ScopedEditOutcome("fallthrough", f"ZERO_CLAUDE_EDIT: {inferred}")
        targets = tuple(sym_files)

    # Symbol-resolved paths go through the same containment check as named ones.
    resolved, problems = resolve_target_files(
        targets, root, infer_bare=len(prompt.strip()) <= _MAX_INFER_PROMPT_CHARS,
    )
    if problems:
        reason = "; ".join(problems)
        return ScopedEditOutcome(
            "block", f"ZERO_CLAUDE_EDIT BLOCKED: {reason}",
            message=failure_message(reason), applied=False,
        )
    if not resolved:
        return ScopedEditOutcome(
            "fallthrough",
            f"ZERO_CLAUDE_EDIT: none of {targets} exist in the repo",
        )

    dirty = dirty_files(root, resolved)
    if dirty:
        return ScopedEditOutcome(
            "fallthrough", f"ZERO_CLAUDE_EDIT: target file(s) have uncommitted changes — {dirty}",
        )

    model = edit_model()

    # Ollama availability gate — same signals direct_executor.execute_chain
    # uses (§2.4): an unpulled or unreachable model 404s, which must not
    # read as "nothing to route" and quietly fall through. Once we are past
    # the fallthrough gates above, an unreachable model is a FAILED attempt,
    # not a reason to hand the turn to Claude — per the owner's choice,
    # failure blocks.
    from llm_router.hooks.direct_executor import available_ollama_models, ollama_is_alive
    installed = available_ollama_models(timeout=0.5)
    if installed is not None:
        model_ok = model in installed or (":" not in model and f"{model}:latest" in installed)
    else:
        model_ok = ollama_is_alive(timeout=0.5)
    if not model_ok:
        reason = f"local model {model!r} is not reachable/installed"
        return ScopedEditOutcome(
            "block", f"ZERO_CLAUDE_EDIT BLOCKED: {reason}", message=failure_message(reason), applied=False,
        )

    # Plan 3.7: a cold model plus the hook's ~37 s deadline is a predictable
    # failure (0/5 cold-or-contended edits served live). One cheap /api/ps
    # call; when the model is not resident and the deadline cannot cover a load
    # plus an edit, hand the turn to Claude NOW (not a block: nothing went
    # wrong) and start the warm-up so the next edit finds the model resident.
    # Unknown state (Ollama unreachable, odd /api/ps) proceeds as before.
    from llm_router import warm
    cold_reason = warm.should_skip_cold(model, deadline_s)
    if cold_reason:
        warm.warm_edit_model_bg(model)
        return ScopedEditOutcome("fallthrough", f"ZERO_CLAUDE_EDIT: cold model — {cold_reason}")

    from llm_router.edit import read_file_for_edit
    file_contents: dict[str, str] = {}
    for rel in resolved:
        content, _truncated = read_file_for_edit(str(root / rel))
        file_contents[rel] = content

    new_contents, instructions, fail_reason = generate_edits(prompt, file_contents, model, deadline_s)

    if new_contents is None:
        reason = fail_reason or "no edit instructions could be validated"
        for instr in instructions:
            _record_edit_ledger(instr.file, model, applied=False)
        return ScopedEditOutcome(
            "block", f"ZERO_CLAUDE_EDIT BLOCKED: {reason}", message=failure_message(reason), applied=False,
        )

    changed_files = [f for f in new_contents if new_contents[f] != file_contents.get(f)]
    diffs = {f: short_diff(file_contents[f], new_contents[f], f) for f in changed_files}

    # Plan 3.3: one more gate before the write, after validation — lint the
    # ORIGINAL and CANDIDATE text, block only on violations the edit adds
    # (never the real file; see verify.py's module docstring
    # for why a temp copy and not write-then-rollback). A failure here is
    # routed through the exact same failed-edit path as a failed
    # generate_edits() above: block, write nothing, escalate to Claude. The
    # real files are untouched in every branch that returns before the write
    # loop below.
    from llm_router.local_agent.verify import verify_changed_files
    verify_result = verify_changed_files(new_contents, file_contents, changed_files, root, deadline_s)
    if not verify_result.ok:
        reason = f"verification failed — {verify_result.reason}"
        for instr in instructions:
            _record_edit_ledger(instr.file, model, applied=False)
        return ScopedEditOutcome(
            "block", f"ZERO_CLAUDE_EDIT BLOCKED: {reason}", message=failure_message(reason), applied=False,
        )

    for f in changed_files:
        (root / f).write_text(new_contents[f], encoding="utf-8")
    for instr in instructions:
        _record_edit_ledger(instr.file, model, applied=True)

    message = applied_message(model, changed_files, diffs, inferred)
    return ScopedEditOutcome(
        "block", f"ZERO_CLAUDE_EDIT APPLIED: model={model} files={changed_files}"
        + (f" inferred=[{inferred}]" if inferred else ""),
        message=message, applied=True,
    )


def _record_edit_ledger(file: str, model: str, applied: bool) -> None:
    try:
        from llm_router.edit_ledger import record_edit_outcome
        record_edit_outcome(file=file, model=f"ollama/{model}", applied=applied)
    except Exception:                                            # noqa: BLE001
        pass
