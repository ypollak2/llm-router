"""Resolver behaviour over synthetic setups. Every case states the setup and the
exact outcome; the reasons are asserted, not just the verdict."""

from __future__ import annotations

import json
import random

import pytest

from llm_router.resolver import inventory as inv_mod
from llm_router.resolver import profile as profile_mod
from llm_router.resolver.profile import ProbeResult
from llm_router.resolver.resolve import (
    PRESSURE_DEMOTE,
    STATUS_NO_ELIGIBLE,
    STATUS_ROUTED,
    Needs,
    Setup,
    hard_failures,
    resolve,
)
from llm_router.resolver.types import (
    CAP_NO,
    CAP_YES,
    SRC_DECLARED,
    SRC_MEASURED,
    Cap,
    Inventory,
    ModelEntry,
    Quota,
)

from .fakes import NOW, make_probes


def E(model_id, *, route="local", ceiling="EASY", basis="measured", tools=True, vision=False,
      thinking=False, ctx=32768, pressure=None, qstate=None, until=None, verified=True,
      authorized=True, present=True, privacy=None, price=None, recall=None, cap_source=SRC_DECLARED):
    qs = qstate or ("n/a" if route == "local" else ("metered" if route == "api" else "ok"))
    caps = {n: Cap(CAP_YES if on else CAP_NO, cap_source)
            for n, on in (("tools", tools), ("vision", vision), ("thinking", thinking))}
    return ModelEntry(
        id=model_id, provider=model_id.split("/")[0], route_kind=route,
        privacy=privacy or ("local" if route == "local" else "cloud"),
        path_verified=verified, path_detail="" if verified else "key present only",
        authorized=authorized, auth_detail="" if authorized else "not logged in",
        quota=Quota(qs, pressure, until, ""), capabilities=caps, context_window=ctx,
        tier_ceiling=ceiling, tier_basis=basis, present=present,
        price_in_per_mtok=price[0] if price else None, price_out_per_mtok=price[1] if price else None,
        measured_recall_tokens=recall)


def setup(*models, configured="anthropic/claude-opus-4-8", age=0.0):
    return Setup(Inventory(generated_at=NOW - age, models=list(models)), configured)


def go(tier, needs, s, **kw):
    return resolve(tier, Needs.parse(needs) if isinstance(needs, str) else needs, s, now=NOW, **kw)


def rejected(res):
    return {r.model: r.reasons for r in res.rejected}


# ------------------------------------------------------------ 1-2: local-only

def test_local_only_small_model_serves_easy_and_refuses_medium_without_downgrading():
    s = setup(E("ollama/qwen3:8b", ceiling="EASY"))
    easy = go("EASY", "tools", s)
    assert (easy.status, easy.model, easy.route) == (STATUS_ROUTED, "ollama/qwen3:8b", "local")

    med = go("MEDIUM", "tools", s)
    assert med.status == STATUS_NO_ELIGIBLE and med.model is None
    assert med.keep_configured == "anthropic/claude-opus-4-8"
    assert "no eligible model for MEDIUM" in med.reason
    assert any("no model qualifies for MEDIUM" in w for w in med.warnings)
    below = [r for r in med.fallbacks if r.below_tier]
    assert [r.model for r in below] == ["ollama/qwen3:8b"]        # visible, flagged, NOT selected
    assert med.fallbacks[-1].kind == "configured"


def test_local_only_large_model_serves_medium_but_not_frontier():
    s = setup(E("ollama/qwen3-coder:30b", ceiling="MEDIUM"), E("ollama/qwen3:8b", ceiling="EASY"))
    assert go("MEDIUM", "tools", s).model == "ollama/qwen3-coder:30b"
    assert go("EASY", "tools", s).model == "ollama/qwen3:8b"       # tightest fit, not the biggest
    fr = go("FRONTIER", "", s)
    assert fr.status == STATUS_NO_ELIGIBLE
    assert [r.model for r in fr.fallbacks if r.below_tier] == ["ollama/qwen3-coder:30b", "ollama/qwen3:8b"]


# ---------------------------------------------------------- 3-4: subscriptions

def test_claude_only_subscription_matches_the_tier_and_flags_the_prior():
    s = setup(E("claude_subscription/haiku", route="subscription", ceiling="EASY", basis="prior", vision=True, pressure=0.2),
              E("claude_subscription/sonnet", route="subscription", ceiling="MEDIUM", basis="prior", vision=True, pressure=0.2),
              E("claude_subscription/opus", route="subscription", ceiling="FRONTIER", basis="prior", vision=True, pressure=0.2))
    assert go("EASY", "vision", s).model == "claude_subscription/haiku"
    assert go("MEDIUM", "vision", s).model == "claude_subscription/sonnet"
    fr = go("FRONTIER", "vision", s)
    assert fr.model == "claude_subscription/opus"
    assert any("curated registry, not a measurement" in w for w in fr.warnings)
    assert any("vision is declared, not measured" in w for w in fr.warnings)
    assert [r.model for r in fr.fallbacks[:-1]] == ["claude_subscription/sonnet", "claude_subscription/haiku"]
    assert all(r.below_tier for r in fr.fallbacks[:-1])


def test_codex_only_setup_routes_frontier_and_keeps_configured_as_last_rung():
    s = setup(E("codex/gpt-5.5", route="subscription", ceiling="FRONTIER", basis="prior", pressure=0.1),
              E("codex/gpt-5.4", route="subscription", ceiling="MEDIUM", basis="prior", pressure=0.1))
    assert go("FRONTIER", "tools", s).model == "codex/gpt-5.5"
    med = go("MEDIUM", "tools", s)
    assert med.model == "codex/gpt-5.4"
    assert med.fallbacks[0].model == "codex/gpt-5.5" and not med.fallbacks[0].below_tier
    assert med.fallbacks[-1].kind == "configured"


# ----------------------------------------------------------------- 5: API-only

def test_api_key_only_is_not_routable_until_the_path_is_verified():
    uncal = setup(E("openai/gpt-5.5", route="api", ceiling="FRONTIER", basis="prior", verified=False))
    res = go("FRONTIER", "tools", uncal)
    assert res.status == STATUS_NO_ELIGIBLE and res.keep_configured
    assert "execution path not verified" in " ".join(rejected(res)["openai/gpt-5.5"])

    cal = setup(E("openai/gpt-5.5", route="api", ceiling="FRONTIER", basis="prior", verified=True,
                  price=(5.0, 20.0)))
    ok = go("FRONTIER", "tools", cal)
    assert ok.model == "openai/gpt-5.5" and ok.route == "api"
    assert any("metered API route" in w for w in ok.warnings)


# -------------------------------------------------------------------- 6: mixed

def test_mixed_setup_prefers_cheap_route_for_easy_and_lower_pressure_for_frontier():
    s = setup(E("ollama/qwen3:8b", ceiling="EASY"),
              E("claude_subscription/opus", route="subscription", ceiling="FRONTIER", basis="prior", pressure=0.3),
              E("codex/gpt-5.5", route="subscription", ceiling="FRONTIER", basis="prior", pressure=0.5),
              E("openai/gpt-5.5", route="api", ceiling="FRONTIER", basis="prior", price=(5.0, 20.0)))
    assert go("EASY", "", s).model == "ollama/qwen3:8b"            # local is free
    fr = go("FRONTIER", "", s)
    assert fr.model == "claude_subscription/opus"                  # subscription before metered; lower pressure first
    assert [r.model for r in fr.fallbacks[:2]] == ["codex/gpt-5.5", "openai/gpt-5.5"]
    assert fr.fallbacks[-2].model == "ollama/qwen3:8b" and fr.fallbacks[-2].below_tier
    assert fr.fallbacks[-1].kind == "configured"


def test_a_measured_model_beats_an_equal_prior_model():
    s = setup(E("claude_subscription/sonnet", route="subscription", ceiling="MEDIUM", basis="prior", pressure=0.1),
              E("ollama/big:70b", ceiling="MEDIUM", basis="measured"))
    assert go("MEDIUM", "", s).model == "ollama/big:70b"


# ------------------------------------------------------------- 7: benched Codex

def test_benched_codex_is_rejected_with_its_reason_and_the_next_route_is_used():
    until = NOW + 3 * 3600 + 20 * 60
    s = setup(E("codex/gpt-5.5", route="subscription", ceiling="FRONTIER", basis="prior", qstate="benched", until=until),
              E("claude_subscription/opus", route="subscription", ceiling="FRONTIER", basis="prior", pressure=0.2))
    res = go("FRONTIER", "", s)
    assert res.model == "claude_subscription/opus"
    assert "benched after a reported usage limit for another 3h20m" in rejected(res)["codex/gpt-5.5"][0]
    assert "codex/gpt-5.5" not in [r.model for r in res.fallbacks]  # never offered as a fallback either


def test_only_codex_and_it_is_benched_means_no_eligible_and_keep_configured():
    s = setup(E("codex/gpt-5.5", route="subscription", ceiling="FRONTIER", basis="prior", qstate="benched", until=NOW + 60))
    res = go("FRONTIER", "", s)
    assert res.status == STATUS_NO_ELIGIBLE and res.keep_configured == "anthropic/claude-opus-4-8"
    assert [r.kind for r in res.fallbacks] == ["configured"]


# --------------------------------------------------------- 8: quota pressure 92%

def test_pressure_at_92_percent_demotes_but_does_not_forbid():
    claude = E("claude_subscription/opus", route="subscription", ceiling="FRONTIER", basis="prior", pressure=0.92)
    codex = E("codex/gpt-5.5", route="subscription", ceiling="FRONTIER", basis="prior", pressure=0.40)
    res = go("FRONTIER", "", setup(claude, codex))
    assert res.model == "codex/gpt-5.5"
    assert "claude_subscription/opus deprioritised at 92% quota pressure" in res.reason
    assert res.fallbacks[0].model == "claude_subscription/opus"     # still on the ladder

    only = go("FRONTIER", "", setup(claude))
    assert only.model == "claude_subscription/opus"                 # the only qualified route is kept...
    assert any("quota pressure 92%" in w for w in only.warnings)    # ...with a warning

    exhausted = go("FRONTIER", "", setup(
        E("claude_subscription/opus", route="subscription", ceiling="FRONTIER", basis="prior", pressure=0.99)))
    assert exhausted.status == STATUS_NO_ELIGIBLE
    assert rejected(exhausted)["claude_subscription/opus"] == ("quota exhausted (99% used)",)


def test_pressure_threshold_is_the_documented_constant():
    assert PRESSURE_DEMOTE == 0.85


def test_pressure_outranks_cost_among_equal_tier_fits():
    a = E("claude_subscription/sonnet", route="subscription", ceiling="MEDIUM", basis="prior", pressure=0.9)
    b = E("openai/gpt-5.4", route="api", ceiling="MEDIUM", basis="prior", price=(2.0, 8.0))
    assert go("MEDIUM", "", setup(a, b)).model == "openai/gpt-5.4"


# ------------------------------------------------------------- 9: stale inventory

def test_model_removed_since_last_inventory_is_never_chosen_and_is_called_out():
    old = inv_mod.collect_inventory(make_probes(ollama={
        "keep:7b": {"caps": ["completion", "tools"]}, "gone:30b": {"caps": ["completion", "tools"]}}), profiles={})
    live = inv_mod.collect_inventory(make_probes(ollama={"keep:7b": {"caps": ["completion", "tools"]}}),
                                     profiles={}, previous=old)
    s = Setup(live, configured_model="ollama/gone:30b")
    res = resolve("EASY", Needs(), s, now=NOW, allow_unmeasured=True)
    assert res.model == "ollama/keep:7b"
    assert "removed since the last inventory" in rejected(res)["ollama/gone:30b"][0]
    assert any("configured model ollama/gone:30b was removed" in w for w in res.warnings)


def test_old_inventory_snapshot_warns_with_its_age():
    s = setup(E("ollama/a:1b"), age=30 * 3600)
    res = go("EASY", "", s)
    assert res.model == "ollama/a:1b"
    assert any("inventory is 30.0h old" in w for w in res.warnings)
    assert not any("inventory is" in w for w in go("EASY", "", setup(E("ollama/a:1b"), age=60)).warnings)


# -------------------------------------------------------------- 10: vision

def test_vision_needed_but_no_vision_model_is_an_explicit_no():
    s = setup(E("ollama/qwen3:8b", vision=False), E("claude_subscription/haiku", route="subscription",
                                                    ceiling="EASY", basis="prior", vision=False, pressure=0.1))
    res = go("EASY", "vision", s)
    assert res.status == STATUS_NO_ELIGIBLE
    assert all("needs vision: declared as unsupported" in r[0] for r in rejected(res).values())
    assert res.keep_configured


def test_unknown_capability_is_not_treated_as_supported():
    m = E("ollama/old:1b")
    m = ModelEntry(**{**m.__dict__, "capabilities": {}})
    assert "capability unknown" in hard_failures(m, Needs(vision=True), NOW)[0]


def test_measured_no_overrides_declared_yes():
    m = E("ollama/liar:7b", tools=True)
    caps = dict(m.capabilities)
    caps["tools"] = Cap(CAP_NO, SRC_MEASURED, "no lookup tool call")
    m = ModelEntry(**{**m.__dict__, "capabilities": caps})
    assert hard_failures(m, Needs(tools=True), NOW) == ["needs tools: measured as unsupported"]


# --------------------------------------------------------- 11: privacy local-only

def test_local_only_with_only_cloud_models_refuses_and_cannot_keep_a_cloud_default():
    s = setup(E("claude_subscription/opus", route="subscription", ceiling="FRONTIER", basis="prior", pressure=0.1),
              E("codex/gpt-5.5", route="subscription", ceiling="FRONTIER", basis="prior", pressure=0.1))
    res = go("EASY", "local-only", s)
    assert res.status == STATUS_NO_ELIGIBLE and res.model is None
    assert all("privacy=local-only" in r[0] for r in rejected(res).values())
    assert res.keep_configured is None
    assert [r.kind for r in res.fallbacks] == ["ask_user"]
    assert any("cannot be kept under privacy=local-only" in w for w in res.warnings)


def test_local_only_picks_the_local_model_even_when_cloud_is_a_better_tier_fit():
    s = setup(E("ollama/qwen3:8b", ceiling="EASY"),
              E("claude_subscription/haiku", route="subscription", ceiling="EASY", basis="prior", pressure=0.0))
    assert go("EASY", "local-only", s).model == "ollama/qwen3:8b"


def test_remote_ollama_counts_as_cloud_for_privacy():
    s = setup(E("ollama/remote:8b", privacy="cloud"))
    assert go("EASY", "local-only", s).status == STATUS_NO_ELIGIBLE


def test_a_local_configured_model_can_be_kept_under_local_only():
    s = setup(E("ollama/qwen3:8b", ceiling="EASY"), configured="ollama/qwen3:8b")
    res = go("MEDIUM", "local-only", s)
    assert res.status == STATUS_NO_ELIGIBLE and res.keep_configured == "ollama/qwen3:8b"


# -------------------------------------------------------- 12: nothing for FRONTIER

def test_no_model_for_frontier_names_the_best_available_and_does_not_pick_it():
    s = setup(E("claude_subscription/sonnet", route="subscription", ceiling="MEDIUM", basis="prior", pressure=0.1),
              E("ollama/q:30b", ceiling="MEDIUM"))
    res = go("FRONTIER", "tools", s)
    assert res.status == STATUS_NO_ELIGIBLE and res.model is None and res.route is None
    assert res.fallbacks[0].below_tier and res.fallbacks[0].note == "MEDIUM < requested FRONTIER"
    assert "0 qualified" not in res.reason and "no eligible model for FRONTIER" in res.reason


# ------------------------------------------------------- tier-qualification rules

def test_unmeasured_model_is_not_qualified_unless_explicitly_allowed():
    s = setup(E("ollama/new:7b", ceiling=None, basis="unmeasured"))
    res = go("EASY", "", s)
    assert res.status == STATUS_NO_ELIGIBLE
    assert "not measured, so no tier is qualified" in rejected(res)["ollama/new:7b"][0]
    assert go("EASY", "", s, allow_unmeasured=True).model == "ollama/new:7b"
    assert go("MEDIUM", "", s, allow_unmeasured=True).status == STATUS_NO_ELIGIBLE   # assumed EASY only


def test_a_model_that_failed_calibration_is_out_for_every_tier():
    s = setup(E("ollama/bad:4b", ceiling=None, basis="measured"))
    res = go("EASY", "", s, allow_unmeasured=True)
    assert res.status == STATUS_NO_ELIGIBLE
    assert rejected(res)["ollama/bad:4b"] == ("failed the EASY probes",)


def test_require_measured_refuses_prior_only_tiers():
    s = setup(E("claude_subscription/opus", route="subscription", ceiling="FRONTIER", basis="prior", pressure=0.1))
    assert go("FRONTIER", "", s).model == "claude_subscription/opus"
    res = go("FRONTIER", "", s, require_measured=True)
    assert res.status == STATUS_NO_ELIGIBLE
    assert "rests on a prior" in rejected(res)["claude_subscription/opus"][0]


# ------------------------------------------------------------------- context

def test_context_need_uses_the_trusted_window_not_the_declared_one():
    big_declared = E("ollama/a:30b", ctx=262144, recall=8000)
    small = E("ollama/b:7b", ctx=32768)
    ok = E("claude_subscription/sonnet", route="subscription", ceiling="MEDIUM", basis="prior", ctx=1_000_000, pressure=0.1)
    res = go("EASY", "ctx=100000", setup(big_declared, small, ok))
    assert res.model == "claude_subscription/sonnet"
    assert "only 8000 usable" in rejected(res)["ollama/a:30b"][0]            # recall measured at 8k only
    assert "only 32768 usable" in rejected(res)["ollama/b:7b"][0]
    unknown = ModelEntry(**{**E("ollama/u:1b").__dict__, "context_window": None})
    assert "window unknown" in hard_failures(unknown, Needs(context_tokens=1000), NOW)[0]


def test_long_context_is_128k_tokens():
    assert Needs.parse("long-context").context_tokens == 128_000


# ------------------------------------------------------------------ Needs parsing

@pytest.mark.parametrize("spec,expect", [
    ("", Needs()),
    ("tools,vision", Needs(tools=True, vision=True)),
    (" Tools , THINKING ", Needs(tools=True, thinking=True)),
    ("ctx=64000,local", Needs(context_tokens=64000, local_only=True)),
    ("privacy=local-only", Needs(local_only=True)),
    ("long_context", Needs(context_tokens=128_000)),
])
def test_needs_parse(spec, expect):
    assert Needs.parse(spec) == expect


@pytest.mark.parametrize("bad", ["teleport", "ctx=abc", "ctx=0", "ctx=-5"])
def test_needs_parse_rejects_garbage(bad):
    with pytest.raises(ValueError):
        Needs.parse(bad)


def test_unknown_tier_is_an_error():
    with pytest.raises(ValueError, match="unknown tier"):
        resolve("GIGANTIC", Needs(), setup(E("ollama/a:1b")), now=NOW)


# ------------------------------------------------------- invariants (randomised)

def _random_model(rng: random.Random, n: int) -> ModelEntry:
    route = rng.choice(["local", "subscription", "api"])
    return E(f"{route}/m{n}", route=route,
             ceiling=rng.choice([None, "EASY", "MEDIUM", "FRONTIER"]),
             basis=rng.choice(["measured", "prior", "unmeasured"]),
             tools=rng.random() < 0.7, vision=rng.random() < 0.4, thinking=rng.random() < 0.4,
             ctx=rng.choice([4096, 32768, 200_000, 1_000_000]),
             pressure=rng.choice([None, 0.0, 0.5, 0.9, 0.995]),
             verified=rng.random() < 0.8, authorized=rng.random() < 0.9, present=rng.random() < 0.9,
             qstate=rng.choice([None, None, "benched", "unknown"]),
             until=NOW + 600, privacy=rng.choice([None, None, "cloud"]))


def test_invariants_hold_over_2000_random_setups():
    """Whatever the setup: a routed model passes every hard check, reaches the
    requested tier, honours local-only, and the ladder always ends in the
    configured model or an explicit ask. Counting what was exercised so an empty
    run cannot pass."""
    rng = random.Random(20261004)
    routed = refused = 0
    for _ in range(2000):
        models = [_random_model(rng, i) for i in range(rng.randint(0, 6))]
        needs = Needs(tools=rng.random() < 0.4, vision=rng.random() < 0.2, thinking=rng.random() < 0.2,
                      context_tokens=rng.choice([None, None, 50_000]), local_only=rng.random() < 0.25)
        tier = rng.choice(["EASY", "MEDIUM", "FRONTIER"])
        conf = rng.choice([None, "anthropic/claude-opus-4-8", "ollama/m0"])
        res = resolve(tier, needs, setup(*models, configured=conf), now=NOW,
                      allow_unmeasured=rng.random() < 0.3, require_measured=rng.random() < 0.2)
        last = res.fallbacks[-1]
        assert last.kind in ("configured", "ask_user")
        if res.status == STATUS_ROUTED:
            routed += 1
            m = next(x for x in models if x.id == res.model)
            assert hard_failures(m, needs, NOW) == []
            assert m.tier_ceiling is not None or m.tier_basis == "unmeasured"
            if needs.local_only:
                assert m.privacy == "local"
            assert res.route == m.route_kind
            assert not any(r.model == res.model for r in res.fallbacks)
        else:
            refused += 1
            assert res.model is None and res.route is None
            assert res.reason.startswith("no eligible model for")
    assert routed > 200 and refused > 200, (routed, refused)


def test_ladder_never_lists_a_hard_ineligible_model():
    rng = random.Random(7)
    for _ in range(500):
        models = [_random_model(rng, i) for i in range(5)]
        needs = Needs(tools=rng.random() < 0.5, local_only=rng.random() < 0.3)
        res = resolve("MEDIUM", needs, setup(*models), now=NOW, allow_unmeasured=True)
        by_id = {m.id: m for m in models}
        for r in res.fallbacks:
            if r.kind == "model":
                assert hard_failures(by_id[r.model], needs, NOW) == []


# --------------------------------------------- end to end through inventory + profile

def test_end_to_end_local_calibrated_plus_claude_subscription():
    probes = make_probes(
        ollama={"qwen3-coder:30b": {"caps": ["completion", "tools"], "ctx": 262144}},
        claude="/bin/claude", usage=("ok", 0.92),
        codex=("/bin/codex", "Logged in using ChatGPT"), codex_pressure=0.2)
    prof = profile_mod.build_profile("ollama/qwen3-coder:30b", {
        "json": ProbeResult(True), "edit": ProbeResult(True), "tool_call": ProbeResult(True),
        "long_context": ProbeResult(True, size_tokens=8000)}, reachable=True, now=NOW)
    inv = inv_mod.collect_inventory(probes, profiles={"ollama/qwen3-coder:30b": prof})
    s = Setup(inv, "anthropic/claude-opus-4-8")
    assert resolve("MEDIUM", Needs(tools=True), s, now=NOW).model == "ollama/qwen3-coder:30b"
    fr = resolve("FRONTIER", Needs(tools=True), s, now=NOW)
    assert fr.model == "codex/gpt-5.5"                         # Claude is at 92%, Codex at 20%
    assert any(r.model == "claude_subscription/opus" for r in fr.fallbacks)
    local_only = resolve("FRONTIER", Needs(local_only=True), s, now=NOW)
    assert local_only.status == STATUS_NO_ELIGIBLE


# ------------------------------------------------------------------------- CLI

def _cli(monkeypatch, tmp_path, **kw):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    probes = make_probes(**kw)
    monkeypatch.setattr(inv_mod, "Probes", lambda: probes)


def test_cli_routed_and_no_eligible_exit_codes(monkeypatch, tmp_path, capsys):
    from llm_router.commands.resolve import cmd_resolve

    _cli(monkeypatch, tmp_path, claude="/bin/claude", usage=("ok", 0.1))
    assert cmd_resolve(["--tier", "FRONTIER", "--needs", "tools,vision", "--configured", "my-model"]) == 0
    out = capsys.readouterr().out
    assert "=> claude_subscription/opus" in out and "ladder:" in out and "[BELOW TIER]" in out

    _cli(monkeypatch, tmp_path)           # nothing installed at all
    assert cmd_resolve(["--tier", "EASY", "--configured", "my-model"]) == 3
    out = capsys.readouterr().out
    assert "NO ELIGIBLE MODEL" in out and "keeping: my-model" in out


def test_cli_json_is_parseable(monkeypatch, tmp_path, capsys):
    from llm_router.commands.resolve import cmd_resolve

    _cli(monkeypatch, tmp_path, claude="/bin/claude", usage=("ok", 0.1))
    assert cmd_resolve(["--tier", "medium", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "routed" and data["model"] == "claude_subscription/sonnet"
    assert data["fallbacks"][-1]["kind"] == "ask_user"          # no configured model supplied


def test_cli_errors(monkeypatch, tmp_path, capsys):
    from llm_router.commands.resolve import cmd_resolve

    assert cmd_resolve([]) == 2
    assert cmd_resolve(["--tier", "HUGE"]) == 2
    assert cmd_resolve(["--tier", "EASY", "--needs", "telepathy"]) == 2
    assert "unknown need" in capsys.readouterr().err


def test_configured_model_is_read_from_claude_settings_model_key_only(tmp_path):
    from llm_router.commands.resolve import configured_model_from_claude_settings as f

    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"model": "claude-opus-5-5[1m]", "env": {"ANTHROPIC_API_KEY": "sk-x"}}))
    assert f(p) == "claude-opus-5-5[1m]"
    p.write_text("{}")
    assert f(p) is None
    p.write_text("nope")
    assert f(p) is None
    assert f(tmp_path / "missing.json") is None
