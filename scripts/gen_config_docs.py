#!/usr/bin/env python3
"""Generate the settings table in docs/configuration.md from env_registry.PUBLIC.

    python scripts/gen_config_docs.py           # rewrite the generated block
    python scripts/gen_config_docs.py --check   # exit 1 if the file is stale (CI)

The docs are the registry: edit ``PUBLIC`` in src/llm_router/env_registry.py, never
the table. Only the text between the BEGIN/END markers is owned by this script.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llm_router.env_registry import PUBLIC  # noqa: E402

DOC = ROOT / "docs" / "configuration.md"
BEGIN = "<!-- BEGIN GENERATED -->"
END = "<!-- END GENERATED -->"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def render() -> str:
    lines = [
        "| Setting | Type | Default | What it does |",
        "|---|---|---|---|",
    ]
    for s in PUBLIC.values():
        if s.members:
            name = "Provider API keys: " + ", ".join(f"`{m}`" for m in s.members)
        else:
            name = f"`{s.name}`"
            if s.aliases:
                name += " (alias " + ", ".join(f"`{a}`" for a in s.aliases) + ")"
        lines.append(
            f"| {name} | {_cell(s.type)} | {_cell(s.default)} | {_cell(s.description)} |"
        )
    return "\n".join(lines)


def splice(current: str) -> str:
    head, sep, rest = current.partition(BEGIN)
    _, sep2, tail = rest.partition(END)
    if not sep or not sep2:
        raise SystemExit(f"{DOC}: missing {BEGIN} / {END} markers")
    return f"{head}{BEGIN}\n{render()}\n{END}{tail}"


def main(argv: list[str]) -> int:
    current = DOC.read_text(encoding="utf-8")
    new = splice(current)
    if "--check" in argv:
        if new != current:
            print(f"{DOC.relative_to(ROOT)} is stale; run scripts/gen_config_docs.py", file=sys.stderr)
            return 1
        return 0
    DOC.write_text(new, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
