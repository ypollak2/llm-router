"""Python structure via `ast`. No regex, no model.

The existing OKF extractor is `_SYM_PAT`, a regex for `def foo(` and `class
Bar:`. It is fast and it is wrong in one specific way that matters here: a
regex cannot tell a definition from a mention. `post_entry` inside a comment,
a docstring, a string literal or a call site all look the same to it, and a
symbol index built on that answers "where is X defined" with every file that
talks about X.

`ast` can only see definitions, because that is what the grammar says they
are. That is the entire argument for parsing.

WHAT IS AND IS NOT CLAIMED

Definitions are certain: the parser found a `def`/`class` node at that span.
References are candidates. `ast` sees the name `post_entry` being called in
`invoice.py`; it does not know whether that is `ledger.post_entry`, a local
rebinding, a decorator's substitution or an injected dependency. Those are
recorded with `resolution_status="candidate"` rather than dropped, because "a
reference we could not bind" and "no reference" are different answers and only
one of them is honest about how Python actually works.

A method carries its class in `qualified_name` (`Ledger.balance`), so two
classes with a `balance` stay distinguishable. Nesting deeper than that keeps
the full dotted path for the same reason.
"""
from __future__ import annotations

import ast

from llm_router.semantic.store import Entity, Relation


def _signature(node: ast.AST) -> str:
    """The line a reader needs, without the body they do not."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = []
        a = node.args
        for arg in list(a.posonlyargs) + list(a.args):
            args.append(arg.arg)
        if a.vararg:
            args.append("*" + a.vararg.arg)
        for arg in a.kwonlyargs:
            args.append(arg.arg)
        if a.kwarg:
            args.append("**" + a.kwarg.arg)
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({', '.join(args)})"
    if isinstance(node, ast.ClassDef):
        bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
        return f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"
    return ""


def extract(
    source: str,
    relative_path: str,
    source_hash: str,
) -> tuple[list[Entity], list[Relation]] | None:
    """Entities and relations for one file, or None if it does not parse.

    None rather than an empty list on purpose. Empty means "parsed, defines
    nothing", which is true of `__init__.py`. None means "this file is
    unavailable for this snapshot", and the caller has to record that rather
    than keeping whatever the last parseable version said.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None

    entities: list[Entity] = []
    relations: list[Relation] = []

    def walk(node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(child, ast.ClassDef) else "function"
                qualified = ".".join([*scope, child.name])
                entities.append(Entity(
                    relative_path=relative_path,
                    kind=kind,
                    name=child.name,
                    qualified_name=qualified,
                    start_line=child.lineno,
                    end_line=getattr(child, "end_lineno", child.lineno) or child.lineno,
                    signature=_signature(child),
                    source_hash=source_hash,
                ))
                walk(child, [*scope, child.name])
            else:
                walk(child, scope)

    walk(tree, [])

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                relations.append(Relation(
                    relative_path=relative_path, type="imports",
                    source_name=relative_path, target_name=alias.name,
                    resolution_status="declared", line=node.lineno,
                ))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            relations.append(Relation(
                relative_path=relative_path, type="imports",
                source_name=relative_path, target_name=module,
                resolution_status="declared", line=node.lineno,
            ))
            for alias in node.names:
                relations.append(Relation(
                    relative_path=relative_path, type="imports_name",
                    source_name=relative_path,
                    target_name=f"{module}.{alias.name}" if module else alias.name,
                    resolution_status="declared", line=node.lineno,
                ))
        elif isinstance(node, ast.Call):
            # Candidate, not fact — see the module docstring on what `ast` can
            # and cannot bind.
            func = node.func
            target = ""
            if isinstance(func, ast.Name):
                target = func.id
            elif isinstance(func, ast.Attribute):
                target = func.attr
            if target:
                relations.append(Relation(
                    relative_path=relative_path, type="call_candidate",
                    source_name=relative_path, target_name=target,
                    resolution_status="candidate", line=node.lineno,
                ))

    return entities, relations
