"""No code path may default to a model name nobody checked is installed.

Codex relevance audit, 2026-09-15. Eight reachable paths carried a hardcoded
Ollama model that is not installed on this machine, so each 404'd and degraded in
silence rather than failing:

    hooks/session-start.py      warmed qwen3.5:latest        every session, warming nothing
    hooks/playwright-compress   compressed with qwen2.5:7b   returning output uncompressed
    library/sealer.py           qwen2.5-coder:7b
    agentic/react.py            qwen2.5:7b, under a comment claiming it WAS installed

The last one is the tell: a previous fix replaced one absent model with another
absent model and wrote "a model that is actually installed" above it. Substituting
a fixed name cannot hold, because the inventory changes — qwen3.5 was deleted from
this machine mid-session and eight call sites kept naming it.

The rule is therefore about the SHAPE of the default, not about which model is
currently present.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src/llm_router"

# Reachable code that picks a local model for itself. Not docs, not historical
# measurements, not model metadata — those may legitimately name any model.
GUESS_FREE = [
    "hooks/session-start.py",
    "hooks/playwright-compress.py",
    "library/sealer.py",
    "agentic/react.py",
]

_OLLAMA_NAME = re.compile(r"^(qwen|llama|gemma|mistral|phi|deepseek|codellama)[\w.-]*:[\w.-]+$")


def _string_defaults(path: Path) -> list[tuple[int, str]]:
    """Model-shaped string literals used as a DEFAULT, not in a comment."""
    out = []
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        # os.environ.get("X", "qwen2.5:7b")
        if isinstance(node, ast.Call) and len(node.args) == 2:
            second = node.args[1]
            if isinstance(second, ast.Constant) and isinstance(second.value, str):
                if _OLLAMA_NAME.match(second.value):
                    out.append((node.lineno, second.value))
        # model: str = "qwen2.5:7b"
        if isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str) and _OLLAMA_NAME.match(node.value.value):
                out.append((node.lineno, node.value.value))
    return out


@pytest.mark.parametrize("rel", GUESS_FREE)
def test_no_hardcoded_model_default(rel):
    found = _string_defaults(SRC / rel)
    assert not found, (
        f"{rel} defaults to {found}. A fixed name rots the moment the local "
        "inventory changes, and the failure is a silent 404 rather than an error. "
        "Use model_discovery.first_installed()."
    )


@pytest.mark.parametrize("rel", GUESS_FREE)
def test_each_one_asks_discovery_instead(rel):
    assert "first_installed" in (SRC / rel).read_text(), (
        f"{rel} no longer hardcodes a model but does not consult discovery either"
    )


class TestFirstInstalled:

    def test_it_never_invents_a_model(self, monkeypatch):
        from llm_router import model_discovery as md
        monkeypatch.setattr(md, "available_ollama_models", lambda: ["real:latest"])
        assert md.first_installed(("absent:1b",)) == "real:latest"

    def test_a_preference_is_honoured_when_present(self, monkeypatch):
        from llm_router import model_discovery as md
        monkeypatch.setattr(md, "available_ollama_models",
                            lambda: ["other:latest", "wanted:7b"])
        assert md.first_installed(("wanted:7b",)) == "wanted:7b"

    def test_a_preference_matches_on_family(self, monkeypatch):
        from llm_router import model_discovery as md
        monkeypatch.setattr(md, "available_ollama_models", lambda: ["qwen3-coder:30b"])
        assert md.first_installed(("qwen3-coder:7b",)) == "qwen3-coder:30b"

    def test_nothing_installed_returns_none_not_a_guess(self, monkeypatch):
        from llm_router import model_discovery as md
        monkeypatch.setattr(md, "available_ollama_models", lambda: [])
        assert md.first_installed() is None

    def test_a_broken_discovery_returns_none(self, monkeypatch):
        from llm_router import model_discovery as md
        monkeypatch.setattr(md, "available_ollama_models",
                            lambda: (_ for _ in ()).throw(OSError("no ollama")))
        assert md.first_installed() is None


def test_cache_py_stays_deleted():
    assert not (SRC / "cache.py").exists(), (
        "cache.py is byte-identical to cache/classification.py and unreachable — "
        "Python resolves the package. Editing it changes nothing, which is why it "
        "was deleted rather than kept as a copy."
    )


@pytest.mark.parametrize("rel,retraction", [
    ("chain_builder.py", "NOT the live chain builder"),
    ("context_signal.py", "NOT the one in production"),
])
def test_modules_that_are_not_authoritative_say_so(rel, retraction):
    """Assert the RETRACTION is present, not that the old phrase is absent.

    Both docstrings quote their former claim while correcting it — that is what
    makes the correction legible to the next reader. A test grepping for the old
    phrase would fire on the sentence that fixes it, which is the same mistake as
    the shell=True check earlier in this session.
    """
    head = (SRC / rel).read_text()[:2000]
    assert retraction in head, (
        f"{rel} does not tell the reader it is not the live path. Verified: every "
        "reference to it is a test. A reader who believes the old claim tunes it "
        "and sees nothing change."
    )
