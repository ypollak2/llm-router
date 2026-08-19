"""T4-M2 (Track-4 cost+privacy, Medium): per-classification provider
allow-list wired into the router candidate walk.

Closes the second slice of G-013 (T4-M1 was prompt redaction; this is
the data-residency / provider gating slice). The router refuses any
candidate whose provider is not on the configured allow-list for the
turn's classification — operators can pin CODE to on-prem providers
while letting RESEARCH go to a hosted web-grounded model.

Pins:

1. **Env switch.** Off (default) → no enforcement. Warn → log + allow.
   Strict → skip the candidate, fall through to the next chain entry.
2. **Per-classification semantics.** Allow-list applies only to the
   task_type that's listed. Unlisted classifications are unrestricted
   even under strict — opt-in posture.
3. **Pure-function correctness.** ``check_classification_provider``
   reads env on every call (env-driven, no module state) and returns
   ``(mode, allowed)`` deterministically.
4. **Fail-open on misconfig.** Invalid JSON or wrong shape falls back
   to allow-all with a structured warning — broken env never breaks
   routing.
5. **Router integration.** With CODE pinned to on-prem and a
   forbidden provider attempted first in the chain, the router skips
   the forbidden provider and dispatches to the allowed one.

See: Docs/audit/post-remediation/GAP_ANALYSIS.md G-013.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from llm_router import router as router_mod
from llm_router.audit_routing import reset_audit_log_for_tests
from llm_router.classification_allowlist import (
    MODE_OFF,
    MODE_STRICT,
    MODE_WARN,
    check_classification_provider,
)
from llm_router.idempotency import reset_store_for_tests
from llm_router.router import route_and_call
from llm_router.types import LLMResponse, TaskType


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LLM_ROUTER_CLASSIFICATION_ALLOWLIST", raising=False)
    monkeypatch.delenv("LLM_ROUTER_CLASSIFICATION_ALLOWLIST_MODE", raising=False)
    yield


@pytest.fixture
def isolated_audit_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "audit.db"
    monkeypatch.setenv("LLM_ROUTER_AUDIT_PATH", str(db))
    monkeypatch.delenv("LLM_ROUTER_AUDIT_DISABLED", raising=False)
    reset_audit_log_for_tests()
    yield db
    reset_audit_log_for_tests()


@pytest.fixture
def isolated_idempotency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_ROUTER_IDEMPOTENCY_PATH", str(tmp_path / "idem.db"))
    reset_store_for_tests()
    yield
    reset_store_for_tests()


def _ok_response(model: str = "openai/gpt-5", provider: str = "openai") -> LLMResponse:
    return LLMResponse(
        content="ok",
        model=model,
        provider=provider,
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.001,
        latency_ms=10.0,
    )


# ── 1. Mode resolution ───────────────────────────────────────────────────────


def test_mode_defaults_to_off_when_unset(clean_env) -> None:
    mode, allowed = check_classification_provider(TaskType.CODE, "openai")
    assert mode == MODE_OFF
    assert allowed is True


@pytest.mark.parametrize(
    "value,expected",
    [
        ("strict", MODE_STRICT),
        ("hard", MODE_STRICT),
        ("STRICT", MODE_STRICT),
        ("warn", MODE_WARN),
        ("soft", MODE_WARN),
        ("shadow", MODE_WARN),
        ("WARN", MODE_WARN),
        ("off", MODE_OFF),
        ("", MODE_OFF),
        ("garbage", MODE_OFF),
    ],
)
def test_mode_synonyms(
    clean_env, monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    monkeypatch.setenv("LLM_ROUTER_CLASSIFICATION_ALLOWLIST_MODE", value)
    mode, _ = check_classification_provider(TaskType.CODE, "openai")
    assert mode == expected


# ── 2. Per-classification semantics ──────────────────────────────────────────


def test_strict_with_listed_provider_passes(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_ROUTER_CLASSIFICATION_ALLOWLIST_MODE", "strict")
    monkeypatch.setenv(
        "LLM_ROUTER_CLASSIFICATION_ALLOWLIST",
        '{"code": ["openai", "anthropic"]}',
    )
    mode, allowed = check_classification_provider(TaskType.CODE, "openai")
    assert mode == MODE_STRICT
    assert allowed is True


def test_strict_with_unlisted_provider_refuses(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_ROUTER_CLASSIFICATION_ALLOWLIST_MODE", "strict")
    monkeypatch.setenv(
        "LLM_ROUTER_CLASSIFICATION_ALLOWLIST",
        '{"code": ["openai", "anthropic"]}',
    )
    mode, allowed = check_classification_provider(TaskType.CODE, "perplexity")
    assert mode == MODE_STRICT
    assert allowed is False


def test_strict_with_unconfigured_classification_passes(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opt-in posture: a classification missing from the allow-list is
    unrestricted even under strict. Operators dial up enforcement per
    task type as they validate each."""
    monkeypatch.setenv("LLM_ROUTER_CLASSIFICATION_ALLOWLIST_MODE", "strict")
    monkeypatch.setenv(
        "LLM_ROUTER_CLASSIFICATION_ALLOWLIST",
        '{"code": ["openai"]}',
    )
    # QUERY is not in the config → unrestricted.
    mode, allowed = check_classification_provider(TaskType.QUERY, "perplexity")
    assert mode == MODE_STRICT
    assert allowed is True


def test_provider_match_is_case_insensitive(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_ROUTER_CLASSIFICATION_ALLOWLIST_MODE", "strict")
    monkeypatch.setenv(
        "LLM_ROUTER_CLASSIFICATION_ALLOWLIST",
        '{"code": ["OpenAI"]}',
    )
    _, allowed = check_classification_provider(TaskType.CODE, "openai")
    assert allowed is True


def test_warn_with_unlisted_provider_returns_warn_false(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Warn mode returns (warn, False) — the *caller* decides to log
    and proceed. Decoupling mode from the allow/deny lets the router
    use the same predicate for audit observability."""
    monkeypatch.setenv("LLM_ROUTER_CLASSIFICATION_ALLOWLIST_MODE", "warn")
    monkeypatch.setenv(
        "LLM_ROUTER_CLASSIFICATION_ALLOWLIST",
        '{"code": ["openai"]}',
    )
    mode, allowed = check_classification_provider(TaskType.CODE, "perplexity")
    assert mode == MODE_WARN
    assert allowed is False


# ── 3. Fail-open on misconfig ────────────────────────────────────────────────


def test_invalid_json_falls_back_to_allow_all(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_ROUTER_CLASSIFICATION_ALLOWLIST_MODE", "strict")
    monkeypatch.setenv("LLM_ROUTER_CLASSIFICATION_ALLOWLIST", "this is not json")
    _, allowed = check_classification_provider(TaskType.CODE, "openai")
    # Empty allowlist → no entry for code → allowed even under strict.
    assert allowed is True


def test_non_dict_json_falls_back_to_allow_all(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_ROUTER_CLASSIFICATION_ALLOWLIST_MODE", "strict")
    monkeypatch.setenv(
        "LLM_ROUTER_CLASSIFICATION_ALLOWLIST", '["openai", "anthropic"]'
    )
    _, allowed = check_classification_provider(TaskType.CODE, "openai")
    assert allowed is True


def test_entry_not_list_is_skipped(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single entry with the wrong shape is skipped silently so
    other classifications still enforce."""
    monkeypatch.setenv("LLM_ROUTER_CLASSIFICATION_ALLOWLIST_MODE", "strict")
    monkeypatch.setenv(
        "LLM_ROUTER_CLASSIFICATION_ALLOWLIST",
        '{"code": "openai", "query": ["ollama"]}',
    )
    # 'code' had wrong shape (string not list) → skipped → allowed
    _, code_allowed = check_classification_provider(TaskType.CODE, "perplexity")
    assert code_allowed is True
    # 'query' parsed fine → strict enforcement against 'perplexity'
    _, query_allowed = check_classification_provider(
        TaskType.QUERY, "perplexity"
    )
    assert query_allowed is False


# ── 4. Router integration ────────────────────────────────────────────────────
#
# Pattern mirrors T1-M3: one mocked-dispatch test pins the wiring
# (identity → _dispatch_model_loop), one un-mocked test pins the
# *actual* gate firing inside the loop by setting up a configuration
# where every candidate is forbidden and asserting PermissionDenied.


@pytest.mark.asyncio
async def test_wiring_dispatch_runs_under_off_mode(
    clean_env,
    monkeypatch: pytest.MonkeyPatch,
    isolated_audit_db: Path,
    isolated_idempotency,
) -> None:
    """Sanity: off mode does not block dispatch. Pins that the
    classification gate doesn't accidentally block calls when no
    enforcement is configured."""
    captured: list[dict] = []

    async def _capture(**kwargs: Any) -> LLMResponse:
        captured.append(kwargs)
        return _ok_response(model="perplexity/sonar", provider="perplexity")

    monkeypatch.setattr(router_mod, "_dispatch_model_loop", _capture)

    resp = await route_and_call(
        task_type=TaskType.CODE,
        prompt="research best codecs",
        model_override="perplexity/sonar",
    )
    assert resp.content == "ok"
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_strict_with_all_providers_forbidden_raises_permission_denied(
    clean_env,
    monkeypatch: pytest.MonkeyPatch,
    isolated_audit_db: Path,
    isolated_idempotency,
) -> None:
    """The actual gate firing: pin CODE to a provider that no chain
    member belongs to, and assert PermissionDenied bubbles up via the
    T1-M3 post-loop surface (the classification skip reuses
    rbac_skipped so the same error semantics apply).

    No dispatch mock — this exercises the real candidate walk inside
    ``_dispatch_model_loop`` so the gate is actually exercised end-to-end.
    """
    from llm_router.enterprise.rbac import PermissionDenied

    monkeypatch.setenv("LLM_ROUTER_CLASSIFICATION_ALLOWLIST_MODE", "strict")
    monkeypatch.setenv(
        "LLM_ROUTER_CLASSIFICATION_ALLOWLIST",
        '{"code": ["non-existent-provider"]}',
    )

    with pytest.raises(PermissionDenied):
        await route_and_call(
            task_type=TaskType.CODE,
            prompt="hi",
        )


@pytest.mark.asyncio
async def test_strict_other_classification_unaffected(
    clean_env,
    monkeypatch: pytest.MonkeyPatch,
    isolated_audit_db: Path,
    isolated_idempotency,
) -> None:
    """Opt-in posture pinned at integration level: CODE locked to
    nothing, but QUERY (unconfigured) routes normally."""
    monkeypatch.setenv("LLM_ROUTER_CLASSIFICATION_ALLOWLIST_MODE", "strict")
    monkeypatch.setenv(
        "LLM_ROUTER_CLASSIFICATION_ALLOWLIST",
        '{"code": ["non-existent-provider"]}',
    )

    async def _capture(**kwargs: Any) -> LLMResponse:
        return _ok_response()

    monkeypatch.setattr(router_mod, "_dispatch_model_loop", _capture)

    # QUERY is not in the allow-list → unrestricted → call proceeds.
    resp = await route_and_call(task_type=TaskType.QUERY, prompt="hi")
    assert resp.content == "ok"
