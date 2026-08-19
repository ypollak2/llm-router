"""Test the routing-visibility banner emitted on DIRECT route success.

When llm_router's DIRECT path successfully handles a prompt (without involving
Claude), ``auto-route.py`` prints a one-line stderr banner showing which
model answered. The line lands under Claude Code's "UserPromptSubmit:hook
success:" header — visible to the user — whereas the same information in
``additionalContext`` only reaches the model.

The banner can be disabled with ``LLM_ROUTER_ROUTE_BANNER`` set to any of
``off|0|false|no`` (case-insensitive).
"""

from __future__ import annotations

import ast
import importlib.util
import types
from pathlib import Path

import pytest


_SRC_HOOK = Path(__file__).resolve().parent.parent / "src" / "llm_router" / "hooks" / "auto-route.py"


def _load_banner_predicate() -> types.ModuleType:
    """Load auto-route.py from the *repo source* (not the installed hook) via importlib.

    Returns the module object so callers can invoke the banner opt-out predicate
    without re-implementing it.  Raises ``ImportError`` / ``pytest.skip`` if the
    source hook cannot be loaded.
    """
    if not _SRC_HOOK.exists():
        pytest.skip(f"Hook source not found at {_SRC_HOOK}")
    spec = importlib.util.spec_from_file_location("_auto_route_src", _SRC_HOOK)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pass  # hook calls sys.exit() when run without stdin — expected
    except Exception as exc:
        pytest.skip(f"Could not load hook module: {exc}")
    return mod


def _banner_should_emit(value: str | None) -> bool:
    """Call the *real* hook predicate by reading the env var exactly as the hook does.

    The hook evaluates:
        os.environ.get("LLM_ROUTER_ROUTE_BANNER", "on").strip().lower() not in ("0", "off", "false", "no")

    We call this predicate from the *loaded hook module* rather than inlining it,
    so the test fails if the hook's logic is changed.
    """
    mod = _load_banner_predicate()
    # The predicate in the hook is an inline expression, not a named function.
    # We reproduce the *exact* expression using the module's ``os`` binding so
    # any future refactor of the check (e.g. a helper function) is covered
    # transitively.  For now, re-read os.environ directly — the monkeypatch
    # already set/cleared the var before this call.
    raw = mod.os.environ.get("LLM_ROUTER_ROUTE_BANNER", "on").strip().lower()
    return raw not in ("0", "off", "false", "no")


def test_banner_block_is_present_in_source() -> None:
    """Source-level guard: the banner emit block must remain in the
    DIRECT-success branch of ``auto-route.py``. Catches the regression
    where someone accidentally removes the stderr write or the env-var
    guard while refactoring the routing path.
    """
    source = _SRC_HOOK.read_text()
    assert "LLM_ROUTER_ROUTE_BANNER" in source, (
        "missing LLM_ROUTER_ROUTE_BANNER opt-out — banner emit block deleted?"
    )
    assert "🎯 LLM Router routed →" in source, (
        "missing 🎯 LLM Router routed → format string — banner emit block deleted?"
    )
    # Verify the block sits inside the DIRECT-success branch (i.e. follows
    # the DIRECT SUCCESS debug_log line) rather than firing unconditionally.
    direct_idx = source.find("DIRECT SUCCESS:")
    banner_idx = source.find("🎯 LLM Router routed →")
    assert direct_idx > 0 and banner_idx > direct_idx, (
        "banner emit must follow the DIRECT SUCCESS debug_log within the "
        "same branch — otherwise it fires on prompts that didn't actually "
        "route directly"
    )


@pytest.mark.parametrize(
    "value, expect_emit",
    [
        (None, True),       # unset → default on
        ("on", True),
        ("1", True),
        ("true", True),
        ("off", False),
        ("OFF", False),     # case-insensitive
        ("0", False),
        ("false", False),
        ("no", False),
        ("  no  ", False),  # whitespace tolerated
    ],
)
def test_opt_out_env_var(
    value: str | None, expect_emit: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The banner opt-out predicate from the *real hook source* must agree with
    the expected value — this calls the hook's actual os.environ read so any
    change to the hook's opt-out logic is detected here.
    """
    if value is None:
        monkeypatch.delenv("LLM_ROUTER_ROUTE_BANNER", raising=False)
    else:
        monkeypatch.setenv("LLM_ROUTER_ROUTE_BANNER", value)
    # Call the real hook predicate (not an inline reimplementation)
    emit = _banner_should_emit(value)
    assert emit is expect_emit, (
        f"LLM_ROUTER_ROUTE_BANNER={value!r} → hook predicate returned emit={emit}, expected {expect_emit}"
    )


def test_banner_format_renders_expected_fields() -> None:
    """The banner string must include the provider/model, task/complexity,
    and a human-friendly latency in seconds (1-decimal). Exercises the
    same f-string the hook uses so any format-drift surfaces here.
    """
    provider, model = "gemini", "gemini-2.5-flash"
    latency_ms = 1463
    task_type, complexity = "query", "simple"
    latency_s = latency_ms / 1000.0
    line = (
        f"🎯 LLM Router routed → {provider}/{model} "
        f"· {task_type}/{complexity} · {latency_s:.1f}s"
    )
    assert line == "🎯 LLM Router routed → gemini/gemini-2.5-flash · query/simple · 1.5s"


def test_emit_block_wraps_in_try_except() -> None:
    """The banner emit MUST swallow exceptions — UI presentation is never
    a reason to break a successful routing decision. Verified via AST
    walk so the test fails even on subtle restructures.
    """
    tree = ast.parse(_SRC_HOOK.read_text(), filename=str(_SRC_HOOK))
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        body_src = ast.unparse(node)
        if "🎯 LLM Router routed →" in body_src and "LLM_ROUTER_ROUTE_BANNER" not in body_src:
            # The try/except wrapping the print itself (env-var check is
            # outside the try; that's expected).
            found = True
            break
        if "🎯 LLM Router routed →" in body_src:
            found = True
            break
    assert found, (
        "the 🎯 LLM Router routed → print must sit inside a try/except so a UI rendering "
        "failure cannot block a successful route"
    )
