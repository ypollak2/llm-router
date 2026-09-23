"""S7 — markup must be parsed, not printed.

Phase 50, on a clean install of the published 15.0.0:

    $ llm-router status
    ╭──────────────────────────────────────────────────────────╮
    │ [bold #7aa2f7]⚡ LLM_ROUTER Status  ·  Health: Optimal[/] │
    ╰──────────────────────────────────────────────────────────╯

`rich.text.Text(...)` does NOT interpret markup — it renders the characters
literally. `Text.from_markup(...)` parses it. Four call sites in
`status_premium.py` built a markup string and passed it to the constructor
that does not parse markup, on the first command the README sends a new user
to.

The header was the visible one. Fixing it exposed 30 more tokens from three
other sites, which is why this test asserts on the WHOLE rendered output
rather than on the line that was reported.
"""

from __future__ import annotations

# `importing_a_submodule` (tests/conftest.py) undoes the parent-package binding
# that `import llm_router.ui.status_premium` leaves behind. Without it the
# suite's own T-01 guard fails this file at teardown — correctly: a fileless
# stub on `llm_router.ui` would answer for the real module in every test that
# follows. The guard caught this exact class twice during remediation II.

import ast
import io
import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "llm_router"

#: `[bold #7aa2f7]`, `[/]`, `[#9ece6a]`, `[dim]` — rich markup that survived to
#: the terminal. Deliberately narrow: a bare `[1]` or `[ok]` in prose is not
#: markup, and flagging those would make the test noise.
_MARKUP = re.compile(r"\[/(?:[a-z]+)?\]|\[(?:bold|dim|italic|underline|reverse)\b[^\]]*\]|\[#[0-9a-fA-F]{6}\]")


def _render(obj) -> str:
    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, width=120, force_terminal=False).print(obj)
    return buf.getvalue()


def test_the_detector_fires_on_known_markup():
    """Anti-vacuity: a regex that matches nothing passes every clean output."""
    assert _MARKUP.findall("[bold #7aa2f7]x[/]") == ["[bold #7aa2f7]", "[/]"]
    assert _MARKUP.findall("[#9ece6a]y[/]") == ["[#9ece6a]", "[/]"]
    assert not _MARKUP.findall("see footnote [1] and the [ok] column")


def test_the_premium_status_renders_no_literal_markup(importing_a_submodule):
    """The reported defect, and everything it was hiding."""
    from llm_router.ui.status_premium import PremiumStatusCommand

    out = _render(PremiumStatusCommand().render_full_status())
    leaks = _MARKUP.findall(out)
    assert not leaks, (
        f"{len(leaks)} literal markup token(s) reached the terminal: "
        f"{sorted(set(leaks))[:6]}\n"
        "A string containing markup must be built with `Text.from_markup(...)`; "
        "`Text(...)` renders it verbatim."
    )


def test_no_text_constructor_receives_a_markup_string():
    """AST, so the fix cannot be reverted behind a passing render.

    The render test above depends on the code path being reached with data.
    A branch that only runs when a database exists could regress unnoticed, so
    the call sites are checked directly.
    """
    path = SRC / "ui" / "status_premium.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    # Variables assigned a markup-bearing f-string in this module.
    markup_vars: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.JoinedStr):
            rendered = ast.unparse(node.value)
            if "[/" in rendered or re.search(r"\[\{?\w*PALETTE", rendered):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        markup_vars.add(t.id)

    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and ast.unparse(node.func) == "Text"):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        text = ast.unparse(arg)
        carries_markup = (
            (isinstance(arg, ast.Name) and arg.id in markup_vars)
            or "[/" in text
            or bool(re.search(r"\[\{?\w*PALETTE", text))
        )
        if carries_markup:
            offenders.append(f"line {node.lineno}: Text({text[:70]})")

    assert not offenders, (
        "markup string(s) passed to `Text(...)`, which does not parse markup:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse `Text.from_markup(...)`."
    )


def test_the_module_actually_uses_from_markup():
    """The check on the check.

    If someone 'fixes' this by stripping markup from the strings instead, the
    two tests above pass and the colours are gone. This asserts the intended
    mechanism is present.
    """
    src = (SRC / "ui" / "status_premium.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = {
        ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    assert "Text.from_markup" in calls, (
        "status_premium.py no longer calls Text.from_markup anywhere; either "
        "the markup was stripped (losing the styling) or the fix was reverted"
    )


@pytest.mark.parametrize("renderer", [
    "render_header",
    "render_subscription_quotas",
    "render_routing_savings",
])
def test_each_section_renders_cleanly(renderer, importing_a_submodule):
    """Per-section, so a failure names the one that regressed."""
    from llm_router.ui.status_premium import PremiumStatusCommand

    cmd = PremiumStatusCommand()
    fn = getattr(cmd, renderer, None)
    if fn is None:
        pytest.skip(f"{renderer} no longer exists")
    result = fn()
    # `render_header` returns a markup STRING by design; it is the caller's job
    # to parse it. The others return renderables.
    if isinstance(result, str):
        from rich.text import Text

        result = Text.from_markup(result)
    leaks = _MARKUP.findall(_render(result))
    assert not leaks, f"{renderer} leaks markup: {sorted(set(leaks))[:5]}"
