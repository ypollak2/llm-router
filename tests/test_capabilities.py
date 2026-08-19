"""CF-2: the shared capability predicate, relevant-context collection, and safety.

Covers the §7.6 evaluation matrix (including the documented false-positive/negative
boundaries), path/symlink/secret safety, Scenario 5 (symbol → file via repo search),
and the frozen_context / pack_prompt propagation that must survive escalation.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from llm_router.capabilities import (
    EXCLUDED_PATTERNS,
    CapabilityRequirement,
    collect_relevant_context,
    detect_capabilities,
    is_safe_path,
    serialize_relevant_context,
)


# ── §7.6 evaluation matrix ────────────────────────────────────────────────────

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
    """Row 12: 'src/' in prose trips the heuristic. Documented brittleness, not fixed."""
    d = detect_capabilities("My src/images folder has vacation photos", "query")
    # honest boundary: this SHOULD be False but the regex fires — assert the known state
    assert d.required.read_files is True  # known false positive


def test_documented_false_negative_is_a_known_boundary():
    """Row 13: 'patch the auth handler' has no path → regex misses write intent."""
    d = detect_capabilities("Patch the authentication handler", "code")
    # honest boundary: this SHOULD set write_files but the regex misses it
    assert d.required.write_files is False  # known false negative


def test_ambiguous_prompt_low_confidence_conservative():
    d = detect_capabilities("Update it", "query")
    assert d.confidence < 0.6  # ambiguous → low confidence → conservative routing


# ── shared-predicate wiring (default shadow mode) ─────────────────────────────

def test_needs_claude_tools_default_is_legacy(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_CAPABILITY_ROUTING", raising=False)
    from llm_router.hooks.chain_builder import needs_claude_tools
    # a symbol-only prompt: rich detector says needs_tools, but DEFAULT stays legacy(False)
    assert needs_claude_tools("What does `foo` do?", "query") is False
    # a legacy pattern still fires by default
    assert needs_claude_tools("look in the codebase", "query") is True


def test_needs_claude_tools_flag_on_uses_rich_vector(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CAPABILITY_ROUTING", "1")
    from llm_router.hooks.chain_builder import needs_claude_tools
    assert needs_claude_tools("What does `foo` do?", "query") is True  # rich: repo_search


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
    # resolve() follows the link to outside the repo → rejected
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


# ── Scenario 5: symbol → file via repo search; context survives escalation ────

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


def test_scenario5_context_survives_frozen_context_and_pack(tmp_path):
    from llm_router.agentic.adapters import pack_prompt
    from llm_router.agentic.ledger import AcceptanceResult, Milestone, TaskLedger

    root = _git_repo(tmp_path)
    rc = collect_relevant_context("What does `record_delegation` do?", root)
    milestone = Milestone(id="m1", description="do it",
                          acceptance=lambda a: AcceptanceResult(ok=True))
    ledger = TaskLedger(goal="g", milestones=[milestone], relevant_context=rc)
    frozen = ledger.frozen_context()
    ids = [c.get("id") for c in frozen]
    assert "RELEVANT_CONTEXT" in ids
    # survives packing and renders as context, not a completed milestone
    packed = pack_prompt(ledger.milestones[0], frozen)
    assert "widget.py" in packed
    assert "RELEVANT CONTEXT" in packed
    assert "SECRET=should_never_appear" not in packed


def test_serialize_relevant_context_is_bounded(tmp_path):
    root = _git_repo(tmp_path)
    rc = collect_relevant_context("look at `record_delegation` in src/llm_router/widget.py", root)
    assert rc is not None
    s = serialize_relevant_context(rc, max_chars=200)
    assert len(s) <= 200


def test_collect_returns_none_outside_repo(tmp_path):
    # a non-repo cwd with no code refs → no relevant context, no crash
    assert collect_relevant_context("What is the capital of France?", None) is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert collect_relevant_context("hello", empty) is None
