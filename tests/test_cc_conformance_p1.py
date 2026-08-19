"""CLAUDE-CODE-CONFORMANCE P1: contextForAgent → additionalContext for ALL
platforms. contextForAgent is undocumented in current Claude Code hooks docs;
additionalContext is the documented UserPromptSubmit context field. The normalizer
must rename it even for Claude Code sessions (it previously skipped Claude → the
two early-exit hint paths silently failed to inject context)."""
import importlib.util as u
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_auto_route():
    spec = u.spec_from_file_location("ar_p1", ROOT / "src" / "llm_router" / "hooks" / "auto-route.py")
    m = u.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_normalize_renames_for_claude_code():
    m = _load_auto_route()
    for model in ("claude-opus-4-8", "gpt-4o", "gemini-2.5-pro", ""):
        out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                      "contextForAgent": "hi there"}}
        norm = m._normalize_output_for_platform(out, {"model": model})
        hso = norm["hookSpecificOutput"]
        assert "contextForAgent" not in hso, f"contextForAgent leaked for model={model!r}"
        assert hso["additionalContext"] == "hi there", f"not renamed for model={model!r}"


def test_shipped_source_never_leaves_contextForAgent_unnormalized():
    """Every early-exit path that builds a contextForAgent hint must route it
    through _normalize_output_for_platform (which now renames it)."""
    src = (ROOT / "src" / "llm_router" / "hooks" / "auto-route.py").read_text()
    # The two hint paths both dump via the normalizer:
    assert src.count("_normalize_output_for_platform(") >= 2
    # And the normalizer no longer gates the rename on non-Claude platforms:
    assert "for ALL platforms" in src
