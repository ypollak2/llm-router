"""T13f (floor, synthetic): the gate changes only what P0.15 says it changes.

NOT a replay of recorded production decisions (none are available to a test): a matrix of
12 chains x 5 routing policies x 5 local-model sets = 300 cells, each run twice. (`dynamic` is
left out of the matrix: it round-robins and so answers differently on each call.)

  * every model validated  -> the gate-on chain equals the gate-off chain, head and all;
  * unvalidated models present -> the gate-on chain equals today's chain for the world that
    holds only the validated models, plus the unvalidated tail on a non-critical chain.
    That difference is the whole expected-change list.
"""
from __future__ import annotations

import pytest

from llm_router import model_discovery as md
from tests.fixtures.dyn import homes
from tests.fixtures.dyn.router_chain import INJECTED, PROFILES, TASKS, FakeConfig, build

A, B, C, D, X, Y = INJECTED
POLICIES = ("balanced", "local-first", "cost", "quality", "quota-exhaustion")
SETS = {
    "ollama": dict(ollama=[A, B, C, D], configured_ollama="cfg-d:8b"),
    "compat": dict(compat=[X, Y], configured_compat="cfg-y"),
    "both": dict(ollama=[A, B, C, D], compat=[X, Y], configured_ollama="cfg-d:8b", configured_compat="cfg-y"),
    "ollama_new_only": dict(ollama=[B, C]),
    "compat_new_only": dict(compat=[X]),
}
VALIDATED = {"ollama": dict(ollama=[A, D], configured_ollama="cfg-d:8b"),
             "compat": dict(compat=[Y], configured_compat="cfg-y"),
             "both": dict(ollama=[A, D], compat=[Y], configured_ollama="cfg-d:8b", configured_compat="cfg-y"),
             "ollama_new_only": dict(), "compat_new_only": dict()}
NEW = {B, C, X}


@pytest.fixture
def state(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    for v in ("LLM_ROUTER_PROMOTE_GATE", "LLM_ROUTER_OLLAMA_MODEL", "OLLAMA_BUDGET_MODELS", "OLLAMA_MODELS"):
        monkeypatch.delenv(v, raising=False)
    homes.write_discovery(home, ["old-a:7b"])
    md.ensure_grandfather([A])
    return home


def test_300_cells_every_difference_is_on_the_expected_change_list(state, monkeypatch):
    cells = led_before = tail = absent = 0
    for set_name, kw in SETS.items():
        for policy in POLICIES:
            for p in PROFILES:
                for t in TASKS:
                    gated = build(p, t, FakeConfig(**kw, routing_policy=policy))
                    monkeypatch.setenv("LLM_ROUTER_PROMOTE_GATE", "off")
                    today_front_only = build(p, t, FakeConfig(**VALIDATED[set_name], routing_policy=policy))
                    today_all = build(p, t, FakeConfig(**kw, routing_policy=policy))
                    monkeypatch.delenv("LLM_ROUTER_PROMOTE_GATE")
                    cells += 1
                    present = [m for m in gated if m in NEW]
                    base = [m for m in gated if m not in NEW]
                    if VALIDATED[set_name]:
                        expected = today_front_only
                    else:
                        # No validated model: "Ollama is there" is still true (the provider filter keeps the
                        # static ollama entries), so the reference is today's chain minus the new models.
                        expected = [m for m in today_all if m not in NEW]
                    assert base == expected, (set_name, policy, p, t)
                    assert gated[0] == expected[0], "chain head moved"
                    if present:
                        assert f"{p}/{t}" in {f"BUDGET/{x}" for x in ("QUERY", "GENERATE", "ANALYZE")}
                        assert gated[-len(present):] == present and not (set(gated[:-len(present)]) & NEW)
                        tail += 1
                    else:
                        absent += 1
                    if today_all[0] in NEW:
                        assert gated[0] not in NEW, "an unvalidated model led before and still leads"
                        led_before += 1
    assert cells == 300
    assert tail > 0 and absent > 0 and led_before > 0, (tail, absent, led_before)
    print(f"T13f cells={cells} with_tail={tail} without={absent} led_before_the_gate={led_before}")


def test_all_validated_means_gate_on_equals_gate_off(state, monkeypatch):
    """Every model configured: the gate must be a no-op, byte for byte, in 300 cells."""
    cells = 0
    for set_name, kw in SETS.items():
        everything = dict(kw)
        everything["configured_ollama"] = "old-a:7b,new-b:7b,new-c:70b,cfg-d:8b"
        everything["configured_compat"] = "new-x,cfg-y"
        everything.pop("routing_policy", None)
        for policy in POLICIES:
            for p in PROFILES:
                for t in TASKS:
                    cfg = lambda: FakeConfig(**everything, routing_policy=policy)  # noqa: E731
                    on = build(p, t, cfg())
                    monkeypatch.setenv("LLM_ROUTER_PROMOTE_GATE", "off")
                    off = build(p, t, cfg())
                    monkeypatch.delenv("LLM_ROUTER_PROMOTE_GATE")
                    assert on == off, (set_name, policy, p, t)
                    cells += 1
    assert cells == 300


def test_env_registry_keeps_every_key_and_adds_the_valve():
    from llm_router import env_registry

    assert "LLM_ROUTER_PROMOTE_GATE" in env_registry.ENV_REGISTRY
