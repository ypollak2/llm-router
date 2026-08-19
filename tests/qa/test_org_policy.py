"""Org-grade YAML policy tests — secret resolution + plaintext rejection."""
from __future__ import annotations

from pathlib import Path

import pytest

from llm_router.org_policy import (
    OrgPolicy,
    PlaintextSecretInPolicy,
    SecretResolver,
)


# ────────────────────────────────────────────────────────────────────────
# Plaintext-secret rejection — the critical security guarantee
# ────────────────────────────────────────────────────────────────────────

def test_loader_rejects_inline_openai_key(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: bad\n"
        "providers:\n"
        "  openai:\n"
        '    api_key: "sk-proj-MUSTNOTBEHEREplz123456789"\n'
    )
    with pytest.raises(PlaintextSecretInPolicy, match="openai_key"):
        OrgPolicy.load(bad)


def test_loader_rejects_inline_anthropic_key(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        'name: bad\nproviders:\n  anthropic:\n'
        '    api_key: "sk-ant-NEVERINLINE12345678901234567"\n'
    )
    with pytest.raises(PlaintextSecretInPolicy, match="anthropic_key"):
        OrgPolicy.load(bad)


def test_loader_rejects_inline_aws_access_key(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        'name: bad\nproviders:\n  aws:\n'
        '    access_key: "AKIAIOSFODNN7EXAMPLE"\n'
    )
    with pytest.raises(PlaintextSecretInPolicy, match="aws_access_key"):
        OrgPolicy.load(bad)


def test_loader_rejects_inline_jwt(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        'name: bad\n'
        'authorization:\n'
        '  bearer: "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.'
        'SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"\n'
    )
    with pytest.raises(PlaintextSecretInPolicy, match="jwt"):
        OrgPolicy.load(bad)


def test_loader_rejects_inline_private_key_block(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        'name: bad\nkeys:\n  signing: |\n'
        '    -----BEGIN RSA PRIVATE KEY-----\n'
        '    MIIEpAIBAAKCAQEA...\n'
    )
    with pytest.raises(PlaintextSecretInPolicy, match="private_key_block"):
        OrgPolicy.load(bad)


def test_loader_accepts_env_indirection(tmp_path: Path):
    good = tmp_path / "good.yaml"
    good.write_text(
        'name: prod\n'
        'providers:\n'
        '  openai:\n'
        '    api_key: "${env:OPENAI_API_KEY}"\n'
    )
    # Must NOT raise
    policy = OrgPolicy.load(good)
    assert policy.name == "prod"


def test_loader_accepts_vault_indirection(tmp_path: Path):
    good = tmp_path / "good.yaml"
    good.write_text(
        'name: prod\n'
        'providers:\n'
        '  openai:\n'
        '    api_key: "${vault:secret/llm-providers#openai}"\n'
    )
    OrgPolicy.load(good)  # no raise


def test_skip_secret_check_bypass(tmp_path: Path):
    """Power-user escape for migration: skip the check (with explicit consent)."""
    risky = tmp_path / "risky.yaml"
    risky.write_text(
        'name: risky\napi_key: "sk-proj-ABCDEFGHIJ1234567890ABCDEF"\n'
    )
    # Plain load raises
    with pytest.raises(PlaintextSecretInPolicy):
        OrgPolicy.load(risky)
    # Explicit skip works
    OrgPolicy.load(risky, skip_secret_check=True)


# ────────────────────────────────────────────────────────────────────────
# Reference resolution
# ────────────────────────────────────────────────────────────────────────

def test_resolve_env_reference(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MY_TEST_KEY", "the-actual-secret-value")
    good = tmp_path / "p.yaml"
    good.write_text(
        'name: t\n'
        'providers:\n'
        '  custom:\n'
        '    api_key: "${env:MY_TEST_KEY}"\n'
    )
    policy = OrgPolicy.load(good)
    resolved = policy.resolve("providers.custom.api_key")
    assert resolved == "the-actual-secret-value"


def test_resolve_file_reference(tmp_path: Path):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("from-the-file-system\n")
    good = tmp_path / "p.yaml"
    good.write_text(
        f'name: t\n'
        f'providers:\n'
        f'  custom:\n'
        f'    api_key: "${{file:{secret_file}}}"\n'
    )
    policy = OrgPolicy.load(good)
    resolved = policy.resolve("providers.custom.api_key")
    assert resolved == "from-the-file-system"


def test_resolve_env_reference_missing_raises(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("NEVER_DEFINED_KEY", raising=False)
    good = tmp_path / "p.yaml"
    good.write_text(
        'name: t\nkey: "${env:NEVER_DEFINED_KEY}"\n'
    )
    policy = OrgPolicy.load(good)
    with pytest.raises(KeyError, match="NEVER_DEFINED_KEY"):
        policy.resolve("key")


def test_resolve_unknown_scheme_raises():
    resolver = SecretResolver()
    with pytest.raises(ValueError, match="Unknown secret scheme"):
        resolver.resolve("${unknown-scheme:foo}")


def test_resolve_in_value_walks_nested_structure(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("K1", "v1")
    monkeypatch.setenv("K2", "v2")
    resolver = SecretResolver()
    nested = {
        "providers": {
            "a": {"key": "${env:K1}", "url": "https://api.example.com"},
            "b": {"key": "${env:K2}"},
        },
        "tier_budgets": [1.0, 2.0],
    }
    out = resolver.resolve_in_value(nested)
    assert out["providers"]["a"]["key"] == "v1"
    assert out["providers"]["a"]["url"] == "https://api.example.com"
    assert out["providers"]["b"]["key"] == "v2"
    assert out["tier_budgets"] == [1.0, 2.0]


def test_resolve_inline_substring(tmp_path: Path, monkeypatch):
    """Inline refs like 'prefix-${env:X}-suffix' should also work."""
    monkeypatch.setenv("MY_TOKEN", "abc123")
    resolver = SecretResolver()
    result = resolver.resolve_in_value("Bearer ${env:MY_TOKEN}-extra")
    assert result == "Bearer abc123-extra"


def test_custom_scheme_registration(tmp_path: Path):
    resolver = SecretResolver()
    resolver.register_scheme("mock", lambda x: f"mocked-{x}")
    assert resolver.resolve("${mock:foo}") == "mocked-foo"


# ────────────────────────────────────────────────────────────────────────
# Policy navigation
# ────────────────────────────────────────────────────────────────────────

def test_policy_get_returns_literal_with_unresolved_ref(tmp_path: Path):
    """get() returns the YAML literal — useful for inspection without
    triggering external calls."""
    good = tmp_path / "p.yaml"
    good.write_text(
        'name: t\nrouting:\n  enforce: smart\nproviders:\n  a:\n    key: "${env:NOPE}"\n'
    )
    policy = OrgPolicy.load(good)
    assert policy.get("routing.enforce") == "smart"
    # get() doesn't resolve — returns the literal ${env:NOPE}
    assert policy.get("providers.a.key") == "${env:NOPE}"


def test_policy_get_missing_returns_default(tmp_path: Path):
    good = tmp_path / "p.yaml"
    good.write_text("name: t\n")
    policy = OrgPolicy.load(good)
    assert policy.get("nonexistent.path", default="fallback") == "fallback"


def test_policy_resolve_missing_raises(tmp_path: Path):
    good = tmp_path / "p.yaml"
    good.write_text("name: t\n")
    policy = OrgPolicy.load(good)
    with pytest.raises(KeyError):
        policy.resolve("nonexistent.path")


def test_policy_resolve_all_materializes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("K", "resolved")
    good = tmp_path / "p.yaml"
    good.write_text(
        'name: t\nproviders:\n  a:\n    key: "${env:K}"\n'
    )
    policy = OrgPolicy.load(good)
    full = policy.resolve_all()
    assert full["providers"]["a"]["key"] == "resolved"
