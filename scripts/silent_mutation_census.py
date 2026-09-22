#!/usr/bin/env python3
"""Census of `except ...: pass` blocks wrapping a PERSISTENCE mutation — T-14.

A failed write inside one of these reports nothing: no error, no log, no
counter. This is the structure that hid a real `NameError` at `server.py:115`,
found by ruff and invisible to 723 test files.

Run it to see the list; `tests/test_t14_silent_mutation_ratchet.py` runs it to
make sure the count does not grow.

    python3 scripts/silent_mutation_census.py [N]      # show the first N sites

Scope note: this counts PERSISTENCE only. `close()`, `flush()` and attribute
assignment are deliberately excluded — a failed close is usually harmless, a
failed write never is. A broader census (including those) reports ~207.
"""
import ast, pathlib, sys

# PERSISTENCE only. `close`, `flush` and attribute assignment are too weak a
# signal: a failed close is usually harmless, a failed write never is.
MUTATORS = ("write_text", "write_bytes", "unlink", "rename", "replace",
            "rmtree", "commit", "executemany", "remove")

def is_bare_pass(handler):
    return len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass)

def mutates(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            if name in MUTATORS:
                return name
    return None

rows = []
for root in ("src", "scripts"):
    for f in sorted(pathlib.Path(root).rglob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for h in node.handlers:
                if not is_bare_pass(h):
                    continue
                m = mutates(node)
                if m:
                    rows.append((str(f), h.lineno, m))
print(f"PERSISTENCE sites with a bare `except: pass`: {len(rows)}")
for r in rows[:int(sys.argv[1]) if len(sys.argv) > 1 else 10]:
    print(f"  {r[0]}:{r[1]}  ({r[2]})")


def count() -> int:
    """The number of sites, for the ratchet test."""
    return len(rows)
