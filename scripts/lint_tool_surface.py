#!/usr/bin/env python3
"""CHZ-SURF-01 guard — no emitter may hardcode a tool name into a message.

The bug this prevents
─────────────────────
Routing hooks tell the caller which tool to call. Tool names are tier-dependent
(``LLM_ROUTER_SLIM``), and the DEFAULT tier (``consolidated``) registers none of the
legacy ``llm_query``/``llm_analyze``/``llm_code``/``llm_research``/``llm_generate``
names — they live behind ``llm(task=…)``. A hint that names one of them produces
``Error: No such tool available``; the caller then does the work on the expensive
model, and the savings dashboard cannot tell that apart from "chose not to route".
The failure is invisible in every metric we have, which is why it needs a lint.

The rule
────────
A legacy tool name may appear as a BARE string (``tool = "llm_code"``, a TOOL_MAP
value, a set member) — those are logical identifiers used for state, matching and
telemetry, and they are correct. It may NOT appear EMBEDDED IN PROSE — that string
is on its way to a human or a model, and it must be resolved through
``llm_router.tool_surface`` first.

    tool = "llm_code"                             # OK  — logical identifier
    TOOL_MAP = {"code": "llm_code"}               # OK  — logical identifier
    f"  • llm_code: for code tasks"               # FAIL — embedded in a message
    f"  • {route_tool('llm_code')}: for code…"    # OK  — resolved

Also flagged: ``{route_tool('x')}(`` — appending an argument list to a display
form yields the uncallable ``llm(task="code")(prompt=…)``. Use ``route_call``.

Run:  python3 scripts/lint_tool_surface.py [paths...]
Exit: 0 clean, 1 violations found.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# The whole package, not just hooks: the same defect shipped in CLI output
# (`run llm_savings to verify`) and in GENERATED rules/agent files, which teach a
# model the wrong name for as long as the file exists.
DEFAULT_TARGETS = [REPO / "src" / "llm_router"]

# Tool names that are NOT registered under at least one tier and therefore must
# never be spoken aloud unresolved.
#
# RED1-22: DERIVED from DEPRECATED_TOOLS, not hand-listed. The hand-maintained
# tuple carried 13 of 24 keys — missing llm_reason, llm_providers, llm_gain,
# llm_dashboard, llm_import_profile, llm_cache_clear, llm_policy, llm_budget and
# the four llm_router_agent_* names. Any of those could be emitted unresolved with
# this lint reporting clean, which is worse than no lint: a green check over an
# unchecked surface.
#
# A guard whose coverage is a hand-copied second list will always drift from the
# thing it guards. Deriving it means adding a deprecation extends the check for
# free — the same structural move WP-03 made for prices.
def _guarded_names() -> tuple[str, ...]:
    sys.path.insert(0, str(REPO / "src"))
    from llm_router.tool_surface import DEPRECATED_TOOLS

    # Longest-first so no name is matched as a prefix of a longer one.
    return tuple(sorted(DEPRECATED_TOOLS, key=len, reverse=True))


GUARDED = _guarded_names()

# A string is "prose" if it carries anything beyond the bare identifier: spaces,
# punctuation, formatting. A bare name (or a dotted/qualified variant) is a
# logical identifier and is allowed.
_BARE = re.compile(r"^[\w./:|-]*$")

# Files exempt from the lint, with the reason. The loader blocks legitimately name
# the tools in their explanatory comments — comments are not string constants, so
# they never reach this check — but the fallback lambdas do format names.
EXEMPT: dict[str, str] = {}

# Call names whose string arguments are logical inputs, not output.
RESOLVER_CALLS = {
    "route_tool", "route_call", "route_call_with_complexity", "call_parts",
    "resolve", "resolve_name", "is_registered", "_door_for",
    # localize() rewrites a whole blob, so a template passed to it is already safe.
    "localize", "_localize_banner",
}

# No \s* before the "(": prose like `{route_tool('llm_query')} (external)` is
# fine, only a directly-appended argument list `{route_tool('x')}(prompt=…)` is
# the uncallable double call.
DOUBLE_CALL = re.compile(r"\{\s*route_tool\([^)]*\)\s*\}\(")

# Escape hatch for a template whose names ARE resolved, just at render time rather
# than at the literal. Requires a reason so it can't become a silent blanket mute.
PRAGMA = re.compile(r"#\s*chz-surface-ok:\s*\S+")


def _stmt_spans(tree: ast.AST) -> list[tuple[int, int]]:
    """(lineno, end_lineno) for every statement, innermost-first by span width.

    Pragmas are anchored to the enclosing STATEMENT rather than to a fixed number
    of lines above the offending node, because node line numbers are NOT stable
    across Python versions: PEP 701 (3.12) gives f-string literal parts their real
    positions, where 3.11 had them inherit the enclosing node's. A proximity-based
    lookback therefore passed on 3.11 and failed on 3.12+ — which is exactly how
    this lint broke CI while looking clean locally. Statement positions do not move.
    """
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            end = getattr(node, "end_lineno", None) or node.lineno
            spans.append((node.lineno, end))
    spans.sort(key=lambda s: s[1] - s[0])
    return spans


def _has_pragma(src_lines: list[str], lineno: int, spans: list[tuple[int, int]]) -> bool:
    """True if the statement containing ``lineno`` carries a justified pragma.

    A pragma is honoured on the statement's own opening line, or on the comment
    lines directly above it. The line-above form is the one to use for
    triple-quoted templates: a trailing ``#`` after ``\"\"\"`` is not a comment at
    all, it is the first line of the string, and it would print inside the banner.
    """
    if not 1 <= lineno <= len(src_lines):
        return False
    if PRAGMA.search(src_lines[lineno - 1]):
        return True

    start, end = next(((s, e) for s, e in spans if s <= lineno <= e), (lineno, lineno))
    # Anywhere INSIDE the statement counts: a multi-line call puts the offending
    # argument several lines in, and the natural place to justify it is right
    # there next to it, not above the statement's opening line.
    for i in range(start - 1, min(end, len(src_lines))):
        if PRAGMA.search(src_lines[i]):
            return True
    # Walk up through the contiguous comment block immediately above the statement.
    i = start - 2
    while i >= 0 and src_lines[i].strip().startswith("#"):
        if PRAGMA.search(src_lines[i]):
            return True
        i -= 1
    return False


def _docstrings(tree: ast.AST) -> set[int]:
    """id() of every Constant node that is a docstring (documentation, not output)."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _resolver_args(tree: ast.AST) -> set[int]:
    """id() of string Constants passed directly to a resolver call."""
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
        if name not in RESOLVER_CALLS:
            continue
        # Walk INTO each argument: a template is often passed as
        # localize("""…""".strip()), so the Constant is nested under a method
        # call rather than being a direct argument.
        for a in list(node.args) + [k.value for k in node.keywords]:
            for sub in ast.walk(a):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    out.add(id(sub))
    return out


# Variables that hold a LOGICAL tool name (used for state, matching, telemetry).
# Interpolating one into an f-string emits the unresolved name — which is the
# original bug. The display-resolved siblings must be used instead.
#
# This check exists because the literal-scan above cannot see it: `{tool:32}` in
# an ASCII box contains no tool name in the source at all. That variant survived
# the first fix and was only caught by the end-to-end trace.
LOGICAL_TOOL_VARS = {
    "tool": "tool_disp / route_call(tool, …)",
    "_ctx_tool": "_ctx_disp / _ctx_call",
    "expected_tool": "_expected_disp / _expected_call",
}


def _logical_var_interpolations(
    tree: ast.AST, src_lines: list[str], spans: list[tuple[int, int]]
) -> list[tuple[int, str, str]]:
    """(lineno, var, suggestion) for each f-string interpolation of a logical var."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for part in node.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            v = part.value
            if isinstance(v, ast.Name) and v.id in LOGICAL_TOOL_VARS:
                ln = getattr(part, "lineno", getattr(node, "lineno", 0))
                if _has_pragma(src_lines, ln, spans):
                    continue
                out.append((ln, v.id, LOGICAL_TOOL_VARS[v.id]))
    return out


def check_file(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8", errors="replace")
    src_lines = src.splitlines()
    problems: list[str] = []

    for m in DOUBLE_CALL.finditer(src):
        line = src[: m.start()].count("\n") + 1
        problems.append(
            f"{path}:{line}: route_tool(...) followed by '(' builds an uncallable "
            f"double call like llm(task=\"code\")(prompt=…) — use route_call() instead"
        )

    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return problems + [f"{path}: SyntaxError: {e}"]

    spans = _stmt_spans(tree)

    # Only the routing hooks carry these variable names with this meaning.
    if path.parent.name == "hooks":
        for ln, var, better in _logical_var_interpolations(tree, src_lines, spans):
            problems.append(
                f"{path}:{ln}: f-string interpolates the LOGICAL tool variable "
                f"{var!r} — this emits an unresolved name (the CHZ-SURF-01 bug). "
                f"Use {better}, or add '# chz-surface-ok: <reason>' if the value "
                f"is internal (telemetry, state key, debug log)."
            )

    skip = _docstrings(tree) | _resolver_args(tree)

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in skip:
            continue
        text = node.value
        hit = next((g for g in GUARDED if g in text), None)
        if hit is None:
            continue
        if _BARE.match(text.strip()):
            continue  # bare logical identifier — allowed
        if _has_pragma(src_lines, getattr(node, "lineno", 0), spans):
            continue  # explicitly resolved elsewhere; reason required on the line
        problems.append(
            f"{path}:{getattr(node, 'lineno', '?')}: tool name {hit!r} embedded in an "
            f"emitted string — wrap it in route_tool()/route_call() so it names a tool "
            f"registered under the active LLM_ROUTER_SLIM tier. Offending text: "
            f"{text.strip()[:70]!r}"
        )
    return problems


#: Documents whose text IS the final artifact a model reads. Unlike a workflow
#: or a shell script, there is no "assertion vs mention" distinction here — every
#: occurrence is content, so every occurrence counts.
_DOC_SUFFIXES = (".md", ".json")

#: RED1-22: the bundled rules templates are the INPUT to localize(), so legacy
#: names in them are correct and deliberate — localize() rewrites each one to the
#: active tier at install time. Flagging them would be a false positive, and a
#: lint with false positives on its highest-traffic directory gets muted.
#:
#: The property that actually matters for these files is that the LOCALIZED
#: OUTPUT names only registered tools, which a literal scan of the template
#: cannot express. It is asserted in
#: tests/routing/test_rules_tool_resolution.py, per file, against the live
#: registered MCP surface.
_TEMPLATE_DIRS = ("src/llm_router/rules",)


def _is_localize_template(path: Path) -> bool:
    posix = path.as_posix()
    return any(f"/{d}/" in posix or posix.endswith(d) for d in _TEMPLATE_DIRS)


def check_non_python(path: Path) -> list[str]:
    """Scan a non-Python file for tool names that reach a human or a model.

    Two modes, because two kinds of file are being checked.

    **Scripts and workflows** (``.sh``, ``.yml``): only assertions and emitted
    output matter. The Python AST scan cannot see these, and a CI smoke test
    asserting ``'llm_research' in ctx`` stayed green while the emitted hint was
    unroutable — the test was encoding the bug, the worst place for it to hide.
    A name merely mentioned in a comment or a state fixture is the LOGICAL name
    and is fine.

    **Documents** (``.md``, ``.json``): every occurrence counts, because the text
    IS the artifact. RED1-22: this mode did not exist. `.md` was not scanned at
    all, and when it was added naively the script heuristic came with it — so the
    lint "checked" the rules files while being structurally unable to flag
    anything in one, since a markdown table contains no ``assert`` or ``echo``.
    Reporting clean over a surface it cannot inspect is the failure mode this
    whole guard exists to prevent, so it is worth naming here.
    """
    problems: list[str] = []
    is_doc = path.suffix in _DOC_SUFFIXES
    if is_doc and _is_localize_template(path):
        return problems

    for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") and not is_doc:
            continue
        if PRAGMA.search(line):
            continue
        hit = next((g for g in GUARDED if g in line), None)
        if hit is None:
            continue
        if not is_doc and not any(k in line for k in ("assert", "echo ", "print(", "print ")):
            continue
        where = "a document read by a model" if is_doc else "an assertion or output"
        problems.append(
            f"{path}:{i}: tool name {hit!r} hardcoded in {where} — "
            f"resolve it via llm_router.tool_surface.route_tool so it tracks the active "
            f"LLM_ROUTER_SLIM tier. Line: {stripped[:70]!r}"
        )
    return problems


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv[1:]] or DEFAULT_TARGETS
    files: list[Path] = []
    for t in targets:
        files.extend(sorted(t.rglob("*.py")) if t.is_dir() else [t])

    problems: list[str] = []
    for f in files:
        if f.name in EXEMPT or "__pycache__" in f.parts:
            continue
        problems.extend(check_file(f))

    # RED1-22: .md and .json are scanned too, and src/llm_router is included — not
    # just workflows and scripts.
    #
    # The gap this closes is the whole point of the guard. src/llm_router/rules/*.md
    # ARE the rules files: the artifact installed into every host, loaded into
    # every session, and the single strongest teacher of which tool to call. They
    # were the one thing the tool-name lint never looked at. The check covered
    # the code that writes the file and not the file.
    _NON_PY_SUFFIXES = (".md", ".json", ".yml", ".yaml", ".sh")
    if len(argv) <= 1:
        for extra in (
            REPO / "src" / "llm_router",
            REPO / ".github" / "workflows",
            REPO / "scripts",
        ):
            if not extra.is_dir():
                continue
            for f in sorted(extra.rglob("*")):
                if f.suffix in _NON_PY_SUFFIXES and f.is_file():
                    problems.extend(check_non_python(f))
                    files.append(f)

    if problems:
        print(f"CHZ-SURF-01: {len(problems)} violation(s)\n")
        for p in problems:
            print("  " + p)
        print(
            "\nEvery tool name reaching a human or a model must go through "
            "llm_router.tool_surface. See src/llm_router/tool_surface.py for why."
        )
        return 1

    print(f"CHZ-SURF-01: clean ({len(files)} files checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
