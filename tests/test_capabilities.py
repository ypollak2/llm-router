# Ported from Chuzom's test_capabilities.py; imports/env vars renamed to
# llm_router.capabilities / llm_router.hooks.chain_builder /
# LLM_ROUTER_CAPABILITY_ROUTING. The `_git_repo` fixture's synthetic module
# path is renamed from `src/chuzom/widget.py` to `src/llm_router/widget.py`
# (brand-neutral; behavior is identical -- detection only cares about the
# `src/` prefix and the symbol name, not the package name).
#
# NOTE ON SCOPE (two omissions from chuzom's original):
#
# 1. `test_scenario5_context_survives_frozen_context_and_pack` is NOT ported:
#    it exercises `chuzom.agentic.adapters.pack_prompt` and
#    `chuzom.agentic.ledger.{AcceptanceResult, Milestone, TaskLedger}`, a
#    delegation-execution engine llm-router does not have (see
#    `bounded_operational.py`'s own documented gap for the same reason).
#    `collect_relevant_context` itself IS ported and tested below
#    (`test_scenario5_symbol_resolves_to_file`,
#    `test_serialize_relevant_context_is_bounded`).
#
# 2. `test_needs_claude_tools_flag_on_uses_rich_vector` is NOT ported verbatim.
#    In chuzom, `hooks.chain_builder.needs_claude_tools()` DOES switch to the
#    rich capability vector when the flag is on -- i.e. the flag changes
#    chuzom's live routing for that function. llm-router's WS4 scope is
#    explicitly shadow-mode-only here: `needs_claude_tools()` always returns
#    `legacy_match`, regardless of the flag (see the "THIRD DEVIATION" note in
#    `capabilities.py`'s module header). It is replaced below by
#    `test_needs_claude_tools_flag_on_still_uses_legacy_shadow_mode_only`,
#    which asserts the OPPOSITE of chuzom's test by design, plus a broader
#    `test_shadow_mode_invariance_corpus` covering more prompts.
"""CF-2: the shared capability predicate, relevant-context collection, and safety.

Covers the evaluation matrix (including the documented false-positive/negative
boundaries), path/symlink/secret safety, Scenario 5 (symbol -> file via repo
search), and WS4's shadow-mode invariance: `LLM_ROUTER_CAPABILITY_ROUTING` must
never change what `needs_claude_tools()` returns or what the live route picks --
only what gets recorded into `routing_decisions.capabilities_json`.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from llm_router.capabilities import (
    EXCLUDED_PATTERNS,
    CapabilityRequirement,
    capability_routing_enabled,
    collect_relevant_context,
    detect_capabilities,
    is_safe_path,
    serialize_capability_decision,
    serialize_relevant_context,
)


# ── evaluation matrix ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("prompt, task_type, expect_needs_tools, expect_bits", [
    ("What is the GIL?", "query", False, {}),
    ("What does `classify_signals` do?", "query", True, {"repo_search", "read_files"}),
    ("Add a blank line to README.md", "code", True, {"write_files", "objective_verification"}),
    ("Run the tests and show failures", "code", True, {"run_commands", "objective_verification"}),
    ("Rename RouteRecord everywhere across the codebase", "code", True,
     {"repo_search", "multi_step_execution"}),
    ("Show me src/llm_router/routing_quality.py", "query", True, {"read_files"}),
    ("What does `record_delegation` do?", "query", True, {"repo_search", "read_files"}),
])
def test_eval_matrix_positive_rows(prompt, task_type, expect_needs_tools, expect_bits):
    d = detect_capabilities(prompt, task_type)
    assert d.required.needs_tools is expect_needs_tools
    for bit in expect_bits:
        assert getattr(d.required, bit) is True, f"{bit} should be set for: {prompt!r}"


def test_pure_qa_has_no_capabilities():
    d = detect_capabilities("Explain the difference between TCP and UDP", "query")
    assert d.required == CapabilityRequirement()  # all False
    assert d.required.needs_tools is False


def test_documented_false_positive_is_a_known_boundary():
    """'src/' in prose trips the heuristic. Documented brittleness, not fixed."""
    d = detect_capabilities("My src/images folder has vacation photos", "query")
    # honest boundary: this SHOULD be False but the regex fires -- assert the known state
    assert d.required.read_files is True  # known false positive


def test_documented_false_negative_is_a_known_boundary():
    """'patch the auth handler' has no path -> regex misses write intent."""
    d = detect_capabilities("Patch the authentication handler", "code")
    # honest boundary: this SHOULD set write_files but the regex misses it
    assert d.required.write_files is False  # known false negative


def test_ambiguous_prompt_low_confidence_conservative():
    d = detect_capabilities("Update it", "query")
    assert d.confidence < 0.6  # ambiguous -> low confidence -> conservative routing


# ── shared-predicate wiring (default shadow mode) ─────────────────────────────

def test_needs_claude_tools_default_is_legacy(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_CAPABILITY_ROUTING", raising=False)
    from llm_router.hooks.chain_builder import needs_claude_tools
    # a symbol-only prompt: rich detector says needs_tools, but DEFAULT stays legacy(False)
    assert needs_claude_tools("What does `foo` do?", "query") is False
    # a legacy pattern still fires by default
    assert needs_claude_tools("look in the codebase", "query") is True


def test_needs_claude_tools_flag_on_still_uses_legacy_shadow_mode_only(monkeypatch):
    """WS4 deviation from chuzom (see capabilities.py's module header): unlike
    chuzom, llm-router's `needs_claude_tools()` NEVER flips to the rich vector,
    even with the flag on -- live routing stays byte-identical. The flag only
    ever affects what gets shadow-recorded (`router.py` -> capabilities_json).
    """
    monkeypatch.setenv("LLM_ROUTER_CAPABILITY_ROUTING", "1")
    from llm_router.hooks.chain_builder import needs_claude_tools

    # Rich vector WOULD say True (repo_search via the backtick symbol) --
    # confirm that premise holds before asserting the live function ignores it.
    assert detect_capabilities("What does `foo` do?", "query").required.needs_tools is True
    assert needs_claude_tools("What does `foo` do?", "query") is False


def test_shadow_mode_invariance_corpus(monkeypatch):
    """Golden corpus: `needs_claude_tools()` must equal `legacy_match` for every
    prompt below, with the flag both unset and set -- proving the live route is
    byte-identical regardless of LLM_ROUTER_CAPABILITY_ROUTING."""
    from llm_router.hooks.chain_builder import needs_claude_tools

    corpus = [
        ("What is the GIL?", "query"),
        ("What does `classify_signals` do?", "query"),
        ("Add a blank line to README.md", "code"),
        ("Run the tests and show failures", "code"),
        ("Rename RouteRecord everywhere across the codebase", "code"),
        ("look in the codebase", "query"),
        ("Explain the difference between TCP and UDP", "query"),
        ("Update it", "query"),
    ]

    for flag in (None, "0", "1"):
        if flag is None:
            monkeypatch.delenv("LLM_ROUTER_CAPABILITY_ROUTING", raising=False)
        else:
            monkeypatch.setenv("LLM_ROUTER_CAPABILITY_ROUTING", flag)
        for prompt, task_type in corpus:
            expected = detect_capabilities(prompt, task_type).legacy_match
            assert needs_claude_tools(prompt, task_type) is expected, (
                f"flag={flag!r} prompt={prompt!r}: live route diverged from legacy_match"
            )


@pytest.mark.parametrize("value, expected", [
    ("1", True), ("true", True), ("True", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("nonsense", False),
])
def test_capability_routing_enabled_parses_env_flag(monkeypatch, value, expected):
    monkeypatch.setenv("LLM_ROUTER_CAPABILITY_ROUTING", value)
    assert capability_routing_enabled() is expected


def test_capability_routing_enabled_default_off(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_CAPABILITY_ROUTING", raising=False)
    assert capability_routing_enabled() is False


# ── serialize_capability_decision (WS4 shadow-recording payload) ──────────────

def test_serialize_capability_decision_roundtrips_expected_shape():
    d = detect_capabilities("Run the tests and show failures", "code")
    s = serialize_capability_decision(d)
    payload = json.loads(s)
    assert payload["required"]["run_commands"] is True
    assert payload["required"]["objective_verification"] is True
    assert payload["required"]["needs_tools"] is True
    assert payload["legacy_match"] == d.legacy_match
    assert isinstance(payload["evidence"], list)
    assert 0.0 <= payload["confidence"] <= 1.0


def test_serialize_capability_decision_fails_open_on_bad_input():
    class _Broken:
        required = None  # attribute access on .read_files etc. will raise

    assert serialize_capability_decision(_Broken()) == "{}"  # type: ignore[arg-type]


def test_serialize_capability_decision_never_leaks_brand():
    d = detect_capabilities("look in the codebase and fix src/main.py", "code")
    s = serialize_capability_decision(d)
    assert "chuzom" not in s.lower()


# ── safety: containment, traversal, symlink, secrets ──────────────────────────

def test_is_safe_path_rejects_traversal(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    assert is_safe_path(root / "src" / "a.py", root) is True
    assert is_safe_path(root / ".." / "etc" / "passwd", root) is False


def test_is_safe_path_rejects_symlink_escape(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("secret")
    link = root / "src" / "escape.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    # resolve() follows the link to outside the repo -> rejected
    assert is_safe_path(link, root) is False


def test_is_safe_path_rejects_secrets(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for secret in (".env", "id_rsa", "prod.pem", "my_token.txt", "aws_credentials"):
        (root / secret).write_text("x")
        assert is_safe_path(root / secret, root) is False, secret


def test_excluded_patterns_cover_common_secrets():
    for needle in (".env", "*.pem", "*.key", "id_rsa", "*token*", "*secret*"):
        assert needle in EXCLUDED_PATTERNS


# ── Scenario 5: symbol -> file via repo search ─────────────────────────────────

def _git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "llm_router").mkdir(parents=True)
    target = root / "src" / "llm_router" / "widget.py"
    target.write_text("def record_delegation():\n    return 42\n")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / ".env").write_text("SECRET=should_never_appear\n")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c",
                 "user.name=t", "commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(root), *args], check=True,
                       capture_output=True)
    return root


def test_scenario5_symbol_resolves_to_file(tmp_path):
    root = _git_repo(tmp_path)
    rc = collect_relevant_context("What does `record_delegation` do?", root)
    assert rc is not None
    paths = [f.path for f in rc.files]
    assert any(p.endswith("widget.py") for p in paths), paths
    # the secret file must NEVER be included
    assert not any(".env" in p for p in paths)
    assert "pyproject.toml" in rc.config_files
    assert len(rc.files) <= 12  # bound respected


def test_serialize_relevant_context_is_bounded(tmp_path):
    root = _git_repo(tmp_path)
    rc = collect_relevant_context("look at `record_delegation` in src/llm_router/widget.py", root)
    assert rc is not None
    s = serialize_relevant_context(rc, max_chars=200)
    assert len(s) <= 200


def test_serialize_relevant_context_never_leaks_brand(tmp_path):
    root = _git_repo(tmp_path)
    rc = collect_relevant_context("What does `record_delegation` do?", root)
    assert rc is not None
    s = serialize_relevant_context(rc)
    assert "chuzom" not in s.lower()


def test_collect_returns_none_outside_repo(tmp_path):
    # a non-repo cwd with no code refs -> no relevant context, no crash
    assert collect_relevant_context("What is the capital of France?", None) is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert collect_relevant_context("hello", empty) is None
