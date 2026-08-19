"""SECTION 3 — Policy switching audit.

Covers three areas from the task brief:
  1. ``apply_routing_policy`` (llm_router.user_routing_policy) actually reorders a
     mixed-provider chain differently for local-first / cost / quality vs.
     balanced (no-op).
  2. The TRUE precedence order between user-level (~/.llm-router/routing.yaml),
     repo-level (.llm_router.yml), and env-var overrides in RepoConfig
     (llm_router.repo_config) — verified with real files on disk, not assumed.
  3. Daily-cap / budget enforcement — what actually gates a request in
     route_and_call.

Original finding (see REPORT_A.md for detail): `RepoConfig.daily_caps` and
`RepoConfig.effective_enforce()` were NEVER read by `_build_and_filter_chain`
or `route_and_call` in router.py — their only consumer was
`llm_router.commands.config` (the `llm_router config` / `llm_router config lint` CLI
display command). The real per-task daily cap enforcement came from a
completely different file (`~/.llm-router/org-policy.yaml`, loaded via
`llm_router.policy.load_org_policy()` / `get_task_cap()`), and the "enforce" mode
string had no runtime effect at all.

Since fixed: `route_and_call`'s budget-enforcement block now reads
`RepoConfig.daily_caps` (routing.yaml, dollars) alongside org-policy.yaml's
`task_caps` (cents) for both the global and per-task caps, taking whichever
of the two is more restrictive when both are set. `effective_enforce()` now
has a real effect too: "soft" downgrades a would-be block into a logged
warning instead of raising `BudgetExceededError`; "hard" (the default)
keeps the original blocking behaviour.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router.repo_config import _dict_to_config, _merge, effective_config
from llm_router.router import route_and_call
from llm_router.types import BudgetExceededError, LLMResponse, RoutingProfile, TaskType
from llm_router.user_routing_policy import apply_routing_policy


# ── 1. Policy reordering actually changes the chain ─────────────────────────


def test_local_first_promotes_free_providers_in_canonical_order():
    chain = [
        "openai/gpt-4o",
        "ollama/llama3",
        "anthropic/claude-sonnet-4-6",
        "codex/gpt-4o-mini",
        "gemini_cli/gemini-2.5-pro",
    ]
    result = apply_routing_policy(chain, "local-first", task_type="query")
    assert result == [
        "ollama/llama3",
        "codex/gpt-4o-mini",
        "gemini_cli/gemini-2.5-pro",
        "openai/gpt-4o",
        "anthropic/claude-sonnet-4-6",
    ]
    # Must be a genuine reorder, not a no-op.
    assert result != chain
    assert apply_routing_policy(chain, "balanced", task_type="query") == chain


def test_cost_policy_sorts_free_models_ahead_of_paid_ones():
    chain = ["openai/gpt-4o", "ollama/llama3"]
    result = apply_routing_policy(chain, "cost", task_type="query")
    # Free/local providers are hard-coded to cost 0.0 in _policy_cost; any
    # real (non-negative) LiteLLM price for gpt-4o must sort it after ollama.
    assert result == ["ollama/llama3", "openai/gpt-4o"]
    assert result != chain


def test_quality_policy_sorts_by_task_specific_score_descending(monkeypatch):
    fake_scores = {
        "anthropic/claude-sonnet-4-6": {"code": 0.95},
        "openai/gpt-4o": {"code": 0.90},
    }
    monkeypatch.setattr(
        "llm_router.user_routing_policy._load_quality_scores", lambda: fake_scores
    )
    chain = ["openai/gpt-4o", "anthropic/claude-sonnet-4-6", "ollama/llama3"]
    result = apply_routing_policy(chain, "quality", task_type="code")
    # ollama has no score entry -> falls back to the FREE_PROVIDERS default of
    # 0.5, which sits between the two explicit scores here? No: 0.5 < 0.90, so
    # ollama ends up last.
    assert result == [
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-4o",
        "ollama/llama3",
    ]
    assert result != chain


def test_balanced_policy_is_a_true_no_op():
    chain = ["ollama/llama3", "openai/gpt-4o", "anthropic/claude-sonnet-4-6"]
    assert apply_routing_policy(chain, "balanced", task_type="query") == chain
    assert apply_routing_policy([], "local-first", task_type="query") == []


# ── 2. True precedence order: env > repo > user (verified, not assumed) ────


def test_effective_profile_precedence_env_beats_repo_beats_user(tmp_path, monkeypatch):
    # User-level config
    home = tmp_path / "home"
    (home / ".llm-router").mkdir(parents=True)
    (home / ".llm-router" / "routing.yaml").write_text("profile: budget\n")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    # Repo-level config
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".llm_router.yml").write_text("profile: premium\n")

    monkeypatch.delenv("LLM_ROUTER_PROFILE", raising=False)
    merged = effective_config(start=repo_dir)

    # Repo beats user when no env var is set.
    assert merged.profile == "premium"
    assert merged.effective_profile() == "premium"

    # Env beats repo (and user).
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "balanced")
    assert merged.effective_profile() == "balanced"

    # The stored `.profile` field itself is untouched by env — only the
    # `effective_profile()` accessor applies the env override.
    assert merged.profile == "premium"


def test_effective_enforce_precedence_env_beats_repo_beats_user(monkeypatch):
    user = _dict_to_config({"enforce": "shadow"}, "user")
    repo = _dict_to_config({"enforce": "enforce"}, "repo")
    merged = _merge(user, repo)

    assert merged.enforce == "enforce"  # repo beat user
    monkeypatch.delenv("LLM_ROUTER_ENFORCE", raising=False)
    assert merged.effective_enforce() == "enforce"

    monkeypatch.setenv("LLM_ROUTER_ENFORCE", "hard")
    assert merged.effective_enforce() == "hard"  # env beats repo


def test_repo_routing_pin_overrides_user_pin_for_same_task_no_env_layer():
    """Per-task model/provider pins have NO env-var override at all — only
    repo-vs-user precedence exists (`{**base.routing, **override.routing}`,
    repo wins per task-type key)."""
    user = _dict_to_config(
        {"routing": {"code": {"model": "ollama/user-pick"}}}, "user"
    )
    repo = _dict_to_config(
        {"routing": {"code": {"model": "openai/repo-pick"}}}, "repo"
    )
    merged = _merge(user, repo)
    assert merged.model_override("code") == "openai/repo-pick"


def test_block_lists_are_unioned_not_overridden_between_user_and_repo():
    """block_providers / block_models / allow_models combine (set union)
    across user + repo layers rather than repo replacing user — documented
    here because it's the opposite precedence shape from `profile`/`enforce`/
    `routing` pins, and is easy to assume incorrectly."""
    user = _dict_to_config({"block_providers": ["openai"]}, "user")
    repo = _dict_to_config({"block_providers": ["anthropic"]}, "repo")
    merged = _merge(user, repo)
    assert set(merged.block_providers) == {"openai", "anthropic"}


# ── 3. Daily caps / budget enforcement ──────────────────────────────────────


class _RouteConfig:
    llm_router_profile = RoutingProfile.BALANCED
    llm_router_monthly_budget = 0.0
    llm_router_daily_spend_limit = 0.0
    llm_router_escalate_above = 0.0
    llm_router_hard_stop_above = 0.0
    llm_router_claude_subscription = False
    llm_router_gemini_subscription = False
    llm_router_claw_code = False
    llm_router_routing_policy = "balanced"
    llm_router_agentic_model = ""
    codex_daily_limit = 1000
    compaction_mode = "off"
    compaction_threshold = 4000
    prompt_cache_enabled = False
    prompt_cache_min_tokens = 1024
    context_enabled = False
    caveman_mode = "off"
    available_providers = {"openai", "gemini"}


class _SimpleNamespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _response(model: str) -> LLMResponse:
    return LLMResponse(
        content=f"ok from {model}",
        model=model,
        input_tokens=7,
        output_tokens=3,
        cost_usd=0.001,
        latency_ms=15.0,
        provider=model.split("/", 1)[0],
    )


@pytest.fixture
def routed_runtime(monkeypatch, temp_db):
    """Full route_and_call harness — same shape as tests/audit/test_failure_fallback.py's
    fixture, duplicated locally so this file stays self-contained."""
    # Isolate policy/budget-cap enforcement from quality-gated escalation
    # (default-on), which would add a second in-chain attempt on low-scoring
    # answers and perturb the event counts these tests assert on.
    monkeypatch.setenv("LLM_ROUTER_ESCALATE_ON_QUALITY", "0")
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")
    route_log = MagicMock()
    mock_log = MagicMock()
    mock_log.bind.return_value = route_log
    tracker = MagicMock()
    tracker.is_healthy.return_value = True

    patches = [
        patch("llm_router.router.get_config", return_value=_RouteConfig()),
        patch("llm_router.router.get_tracker", return_value=tracker),
        patch("llm_router.router.log", mock_log),
        patch("llm_router.router._native_notify", lambda *_a, **_k: None),
        patch("llm_router.router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0),
        patch("llm_router.router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0),
        patch("llm_router.router.cost.log_usage", new_callable=AsyncMock),
        patch("llm_router.router.reserve_envelope", new_callable=AsyncMock, return_value=(None, True, None)),
        patch("llm_router.router.commit_envelope", new_callable=AsyncMock),
        patch("llm_router.router.release_envelope", new_callable=AsyncMock),
        patch("llm_router.semantic_cache.check", new_callable=AsyncMock, return_value=None),
        patch("llm_router.semantic_cache.store", new_callable=AsyncMock),
        patch(
            "llm_router.router._build_and_filter_chain",
            new_callable=AsyncMock,
            return_value=["openai/gpt-4o"],
        ),
        patch(
            "llm_router.router.providers.call_llm",
            new_callable=AsyncMock,
            side_effect=lambda model, messages, **kw: _response(model),
        ),
    ]
    for p in patches:
        p.start()
    try:
        yield _SimpleNamespace(route_log=route_log, tracker=tracker)
    finally:
        for p in reversed(patches):
            p.stop()


@pytest.mark.asyncio
async def test_routing_yaml_daily_caps_are_wired_and_downgrade(
    routed_runtime, monkeypatch
):
    """routing.yaml `daily_caps` ARE consulted by the live routing path
    (was CHZ-TQ-007). This test patches the real loader (`effective_config`) —
    the earlier `_BUG` version patched only the config *object* and never
    `effective_config`, so route_and_call never saw the cap and the test
    "passed" for the wrong reason, masking that caps were in fact wired.

    TQ-007 behavior: an exceeded routing.yaml daily cap DOWNGRADES to free-local;
    against the paid-only routed_runtime chain with enforce=hard there is no free
    fallback, so it hard-blocks with BudgetExceededError.
    """
    # effective_enforce() reads LLM_ROUTER_ENFORCE from env first; pin it so an
    # ambient value doesn't flip the no-free-fallback branch to smart.
    monkeypatch.setenv("LLM_ROUTER_ENFORCE", "hard")
    repo_cfg = _dict_to_config(
        {"daily_caps": {"code": 0.0001}, "enforce": "hard"}, "test"
    )
    assert repo_cfg.daily_cap_for("code") == 0.0001

    with patch(
        "llm_router.repo_config.effective_config", return_value=repo_cfg,
    ), patch(
        "llm_router.router.cost.get_daily_spend_by_task_type",
        new_callable=AsyncMock,
        return_value=9_999.0,  # already "way over" the cap
    ), patch("llm_router.policy.load_org_policy", return_value=None):
        with pytest.raises(BudgetExceededError, match="Task-type daily limit|Daily spend"):
            await route_and_call(TaskType.CODE, "hello", profile=RoutingProfile.BALANCED)


@pytest.mark.asyncio
async def test_org_policy_task_cap_is_the_mechanism_that_actually_enforces(
    routed_runtime, monkeypatch,
):
    """The REAL per-task daily cap mechanism: ~/.llm-router/org-policy.yaml's
    `task_caps`, loaded via llm_router.policy.load_org_policy() / get_task_cap().

    TQ-007: an exceeded cap DOWNGRADES to free-local providers rather than
    hard-blocking. The routed_runtime chain is paid-only (openai) — no free
    fallback — so with enforce=hard the cap hard-blocks (the enforcing case
    verified here). In smart mode it would instead fall through to Claude.
    """
    from llm_router.policy import OrgPolicy
    monkeypatch.setenv("LLM_ROUTER_ENFORCE", "hard")

    with patch(
        "llm_router.router.cost.get_daily_spend_by_task_type",
        new_callable=AsyncMock,
        return_value=5.0,
    ), patch(
        "llm_router.policy.load_org_policy",
        return_value=OrgPolicy(task_caps={"code": 0.01}, source="file"),
    ):
        with pytest.raises(BudgetExceededError, match="Task-type daily limit"):
            await route_and_call(TaskType.CODE, "hello", profile=RoutingProfile.BALANCED)


@pytest.mark.asyncio
async def test_task_caps_are_interpreted_as_cents_not_dollars(routed_runtime, monkeypatch):
    """Regression test for the cents/dollars unit bug: OrgPolicy.task_caps is
    documented (policy.py) and displayed (policy.py:569's `${v/100:.2f}`) as
    CENTS, but route_and_call used to compare the raw cents integer directly
    against a dollar-denominated spend — task_caps: {code: 5000} (meant as a
    $50/day cap) was enforced as a $5000/day cap, 100x too permissive.

    task_caps={"code": 5000} means a $50.00/day cap. $40 spent must NOT block
    (under cap); $60 spent must block (over cap, correctly converted). enforce=hard
    so the over-cap case blocks against the paid-only chain (TQ-007 downgrade has
    no free fallback here).
    """
    from llm_router.policy import OrgPolicy
    monkeypatch.setenv("LLM_ROUTER_ENFORCE", "hard")

    with patch(
        "llm_router.router.cost.get_daily_spend_by_task_type",
        new_callable=AsyncMock,
        return_value=40.0,
    ), patch(
        "llm_router.policy.load_org_policy",
        return_value=OrgPolicy(task_caps={"code": 5000}, source="file"),
    ):
        # $40 spent, $50 cap -> must NOT raise.
        response = await route_and_call(TaskType.CODE, "hello", profile=RoutingProfile.BALANCED)
        assert response is not None

    with patch(
        "llm_router.router.cost.get_daily_spend_by_task_type",
        new_callable=AsyncMock,
        return_value=60.0,
    ), patch(
        "llm_router.policy.load_org_policy",
        return_value=OrgPolicy(task_caps={"code": 5000}, source="file"),
    ):
        # $60 spent, $50 cap -> must raise, with the DOLLAR value in the message.
        with pytest.raises(BudgetExceededError, match=r"\$50\.00"):
            await route_and_call(TaskType.CODE, "hello", profile=RoutingProfile.BALANCED)


@pytest.mark.asyncio
async def test_enforce_mode_hard_blocks_soft_falls_through_to_claude(
    routed_runtime, monkeypatch
):
    """LLM_ROUTER_ENFORCE controls the cap no-free branch (RED2-2-01 corrected).

    With a task cap exceeded and no free-local provider:
    - "hard" blocks with BudgetExceededError (paid-only chain).
    - "soft"/"smart" fall through to CLAUDE if the chain has a Claude model —
      NOT to a non-Claude paid provider (that was the RED2-2-01 silent-paid-call
      bug: soft used to return openai/gpt-4o). With a paid-only chain that has no
      Claude, soft also blocks — there is no cap-respecting way to proceed.
    """
    from llm_router.policy import OrgPolicy

    with patch(
        "llm_router.policy.load_org_policy",
        return_value=OrgPolicy(task_caps={"code": 1000}, source="file"),  # $10 cap
    ), patch(
        "llm_router.router.cost.get_daily_spend_by_task_type",
        new_callable=AsyncMock,
        return_value=20.0,
    ):
        # hard: paid-only chain (routed_runtime → openai) → block.
        monkeypatch.setenv("LLM_ROUTER_ENFORCE", "hard")
        with pytest.raises(BudgetExceededError, match="Task-type daily limit"):
            await route_and_call(TaskType.CODE, "hello", profile=RoutingProfile.BALANCED)

        # soft, paid-only NON-Claude chain → block (no silent paid call).
        monkeypatch.setenv("LLM_ROUTER_ENFORCE", "soft")
        with pytest.raises(BudgetExceededError, match="Task-type daily limit"):
            await route_and_call(TaskType.CODE, "hello", profile=RoutingProfile.BALANCED)

        # soft, chain WITH Claude → fall through to Claude (anthropic), not openai.
        with patch(
            "llm_router.router._build_and_filter_chain",
            new_callable=AsyncMock,
            return_value=["openai/gpt-4o", "anthropic/claude-sonnet-4-6"],
        ):
            resp = await route_and_call(
                TaskType.CODE, "hello", profile=RoutingProfile.BALANCED
            )
            assert resp.provider == "anthropic", (
                f"soft cap-fallthrough must go to Claude, not a paid API: {resp.model}"
            )
