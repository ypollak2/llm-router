"""P0.2 (plan v16): hooks name the proxy's opus tier, not a literal model id."""

from __future__ import annotations

from llm_router import pricing


def test_opus_resolves_through_the_proxy_tier_policy(tmp_path, monkeypatch):
    import yaml

    from llm_router.proxy.tiers import DEFAULT_POLICY_PATH, tier_model

    monkeypatch.delenv("LLM_ROUTER_PROXY_TIER_POLICY", raising=False)
    bundled = yaml.safe_load(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    bundled_opus = next(t["model"] for t in bundled["tiers"] if t["name"] == "opus")
    assert tier_model("opus") == bundled_opus
    assert bundled_opus not in pricing.retired_models()

    # The proxy's own override variable moves the hooks with it.
    custom = tmp_path / "tiers.yaml"
    custom.write_text("tiers:\n  - name: opus\n    model: claude-opus-test\nroute:\n  default: {complex: opus}\n")
    monkeypatch.setenv("LLM_ROUTER_PROXY_TIER_POLICY", str(custom))
    assert tier_model("opus") == "claude-opus-test"

    monkeypatch.setenv("LLM_ROUTER_PROXY_TIER_POLICY", str(tmp_path / "missing.yaml"))
    assert tier_model("opus") is None


def test_hook_chain_names_the_tier_opus(monkeypatch):
    from llm_router.hooks.chain_builder import build_chain
    from llm_router.proxy.tiers import tier_model

    monkeypatch.delenv("LLM_ROUTER_PROXY_TIER_POLICY", raising=False)
    claude = [m.model for m in build_chain("complex", "green", "code") if m.provider == "claude"]
    assert claude == [tier_model("opus")]
