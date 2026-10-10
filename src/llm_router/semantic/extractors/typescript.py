"""TypeScript / JavaScript structure by regex. No parser, no model, no Node.

PLAN v16 P1.5 task 3 (R-CTX-6). The Python extractor can use `ast` because the
standard library ships a parser. There is none for TS/JS, and a real one (tsc,
tree-sitter) is a runtime dependency for a feature that must work offline and
never spawn a toolchain, so this is the deliberate smaller thing: a regex pass
over a *skeleton* of the file.

THE SKELETON is the file with comments and string/template contents blanked to
spaces, same length, so offsets line up with the original. That is the whole
defence against the failure the Python docstring names (a regex cannot tell a
definition from a mention): `function foo(` inside a comment or a string is
spaces by the time the definition patterns run. Imports are read from a second
copy with comments blanked but string contents kept, because the module
specifier IS a string.

WHAT IS AND IS NOT CLAIMED

Recorded: top-level and nested `function`, `class`, arrow/function-expression
bindings (`const f = (...) =>`), `interface`/`type`/`enum` (kind ``type``),
`export` of each, `export { a, b }` lists, `import ... from "x"`, `import "x"`,
`require("x")`. Not recorded: class methods, call sites (no `call_candidate`
relations: without a type checker a call target is a guess), re-export chains,
JSX. End lines come from brace counting on the skeleton, so a regex literal
containing an unbalanced brace can lengthen a span; the start line and name are
unaffected, and spans are only a hint to the reader.

A regex cannot fail to parse, so `extract` never returns None for text input.
"""
from __future__ import annotations

import re

from llm_router.semantic.store import Entity, Relation

EXTRACTOR_VERSION = "1"

# One alternation so a `//` inside a string and a quote inside a comment are each
# read as what they are. Order matters: strings and comments are tried at every
# position, whichever starts first wins.
_LEX_RE = re.compile(
    r"""
    (?P<line>//[^\n]*)
  | (?P<block>/\*.*?\*/)
  | (?P<str>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)
    """,
    re.VERBOSE | re.DOTALL,
)


def _blank(text: str) -> str:
    """Same-length text with every non-newline character replaced by a space."""
    return re.sub(r"[^\n]", " ", text)


def _skeletons(source: str) -> tuple[str, str]:
    """(comments blanked, comments AND string contents blanked), same length."""
    no_comments: list[str] = []
    skeleton: list[str] = []
    pos = 0
    for m in _LEX_RE.finditer(source):
        keep = source[pos:m.start()]
        no_comments.append(keep)
        skeleton.append(keep)
        text = m.group(0)
        if m.lastgroup == "str":
            no_comments.append(text)
            skeleton.append(text[0] + _blank(text[1:-1]) + text[-1] if len(text) >= 2 else text)
        else:
            blank = _blank(text)
            no_comments.append(blank)
            skeleton.append(blank)
        pos = m.end()
    tail = source[pos:]
    no_comments.append(tail)
    skeleton.append(tail)
    return "".join(no_comments), "".join(skeleton)


_ID = r"[A-Za-z_$][\w$]*"

_FUNCTION_RE = re.compile(
    rf"^[ \t]*(?P<export>export\s+(?:default\s+)?)?(?:declare\s+)?(?:async\s+)?"
    rf"function\s*\*?\s*(?P<name>{_ID})?\s*(?:<[^>\n]*>)?\s*\(",
    re.MULTILINE,
)
_CLASS_RE = re.compile(
    rf"^[ \t]*(?P<export>export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?"
    rf"class\s+(?P<name>{_ID})",
    re.MULTILINE,
)
_TYPE_RE = re.compile(
    rf"^[ \t]*(?P<export>export\s+)?(?:declare\s+)?(?:const\s+)?"
    rf"(?P<kw>interface|type|enum)\s+(?P<name>{_ID})",
    re.MULTILINE,
)
# const f = (a, b) => ...   const f = async x => ...   const f = function (...)
_BOUND_FN_RE = re.compile(
    rf"^[ \t]*(?P<export>export\s+)?(?:const|let|var)\s+(?P<name>{_ID})"
    rf"\s*(?::[^=\n]+)?=\s*(?:async\s+)?"
    rf"(?:function\b|\([^)\n]*\)\s*(?::[^=\n]+)?=>|{_ID}\s*=>)",
    re.MULTILINE,
)
_EXPORT_LIST_RE = re.compile(r"^[ \t]*export\s*\{(?P<names>[^}]*)\}(?!\s*from)", re.MULTILINE)

_IMPORT_FROM_RE = re.compile(
    r"""^[ \t]*(?:import|export)\s+(?P<clause>[^;'"]*?)\s*from\s*(?P<q>['"])(?P<mod>[^'"\n]+)(?P=q)""",
    re.MULTILINE,
)
_IMPORT_BARE_RE = re.compile(r"""^[ \t]*import\s*(?P<q>['"])(?P<mod>[^'"\n]+)(?P=q)""", re.MULTILINE)
_REQUIRE_RE = re.compile(r"""\brequire\s*\(\s*(?P<q>['"])(?P<mod>[^'"\n]+)(?P=q)\s*\)""")


def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _end_line(skeleton: str, start: int) -> int:
    """Line of the brace matching the first `{` at/after *start*, else the start line.

    A declaration with no body before the next `;` or newline-terminated
    expression (an `interface` always has one, a `type` alias may not) ends where
    it starts. Counting runs on the skeleton, so braces in strings and comments
    are already spaces.
    """
    open_at = skeleton.find("{", start)
    semi_at = skeleton.find(";", start)
    if open_at == -1 or (semi_at != -1 and semi_at < open_at):
        return _line_of(skeleton, start)
    depth = 0
    for i in range(open_at, len(skeleton)):
        ch = skeleton[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return _line_of(skeleton, i)
    return _line_of(skeleton, open_at)


def _signature(source: str, start: int) -> str:
    """The declaration text up to its body or arrow, one line, capped."""
    end = len(source)
    for stop in ("{", "=>", "\n"):
        i = source.find(stop, start)
        if i != -1:
            end = min(end, i)
    return " ".join(source[start:end].split())[:200]


def extract(
    source: str,
    relative_path: str,
    source_hash: str,
) -> tuple[list[Entity], list[Relation]] | None:
    """Entities and relations for one TS/JS file."""
    no_comments, skeleton = _skeletons(source)
    entities: list[Entity] = []
    relations: list[Relation] = []
    seen: set[tuple[str, int]] = set()

    def add(kind: str, name: str, m: re.Match, exported: bool) -> None:
        start = m.start() + (len(m.group(0)) - len(m.group(0).lstrip()))
        line = _line_of(skeleton, start)
        if (name, line) in seen:
            return
        seen.add((name, line))
        entities.append(Entity(
            relative_path=relative_path, kind=kind, name=name, qualified_name=name,
            start_line=line, end_line=max(line, _end_line(skeleton, start)),
            signature=_signature(skeleton, start), source_hash=source_hash,
        ))
        if exported:
            relations.append(Relation(
                relative_path=relative_path, type="exports", source_name=relative_path,
                target_name=name, resolution_status="declared", line=line))

    for m in _FUNCTION_RE.finditer(skeleton):
        name = m.group("name")
        if name:
            add("function", name, m, bool(m.group("export")))
    for m in _CLASS_RE.finditer(skeleton):
        add("class", m.group("name"), m, bool(m.group("export")))
    for m in _TYPE_RE.finditer(skeleton):
        add("type", m.group("name"), m, bool(m.group("export")))
    for m in _BOUND_FN_RE.finditer(skeleton):
        add("function", m.group("name"), m, bool(m.group("export")))

    for m in _EXPORT_LIST_RE.finditer(skeleton):
        for part in m.group("names").split(","):
            # `a as b` exports the binding under the name b.
            name = part.strip().split(" as ")[-1].strip()
            if re.fullmatch(_ID, name):
                relations.append(Relation(
                    relative_path=relative_path, type="exports", source_name=relative_path,
                    target_name=name, resolution_status="declared",
                    line=_line_of(skeleton, m.start())))

    # Imports read the comment-blanked copy: the specifier is a string.
    for m in _IMPORT_FROM_RE.finditer(no_comments):
        module = m.group("mod")
        line = _line_of(no_comments, m.start())
        relations.append(Relation(
            relative_path=relative_path, type="imports", source_name=relative_path,
            target_name=module, resolution_status="declared", line=line))
        clause = m.group("clause")
        braces = re.search(r"\{([^}]*)\}", clause)
        names: list[str] = []
        if braces:
            names = [p.strip().split(" as ")[0].strip() for p in braces.group(1).split(",")]
        default = clause.split("{")[0].split(",")[0].strip()
        if default and default != "*" and not default.startswith("* as") and re.fullmatch(_ID, default):
            names.append("default")
        for name in names:
            if name:
                relations.append(Relation(
                    relative_path=relative_path, type="imports_name",
                    source_name=relative_path, target_name=f"{module}.{name}",
                    resolution_status="declared", line=line))
    for m in _IMPORT_BARE_RE.finditer(no_comments):
        relations.append(Relation(
            relative_path=relative_path, type="imports", source_name=relative_path,
            target_name=m.group("mod"), resolution_status="declared",
            line=_line_of(no_comments, m.start())))
    for m in _REQUIRE_RE.finditer(no_comments):
        relations.append(Relation(
            relative_path=relative_path, type="imports", source_name=relative_path,
            target_name=m.group("mod"), resolution_status="declared",
            line=_line_of(no_comments, m.start())))

    return entities, relations
