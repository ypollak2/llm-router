"""Fake probes for the resolver tests. Nothing here touches Ollama, a CLI, the
network or the real environment."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from llm_router.lineage import Tier as RegTier
from llm_router.model_registry import ModelMetadata, ModelRegistry
from llm_router.resolver.inventory import Probes

NOW = 1_800_000_000.0


def meta(model_id, tier, *, ctx=200_000, caps=("function-calling", "json"), pin=1.0, pout=5.0):
    provider = model_id.split("/")[0]
    return ModelMetadata(id=model_id, provider=provider, tier=RegTier(tier), quality_score=0.5,
                         price_per_1m_input_usd=pin, price_per_1m_output_usd=pout,
                         context_window=ctx, capabilities=tuple(caps))


REGISTRY = ModelRegistry.from_models([
    meta("anthropic/claude-haiku-4-5", "cheap", caps=("function-calling", "vision", "json")),
    meta("anthropic/claude-sonnet-5", "mid", ctx=1_000_000, caps=("function-calling", "vision", "json", "reasoning")),
    meta("anthropic/claude-opus-4-8", "premium", ctx=1_000_000, caps=("function-calling", "vision", "json", "reasoning")),
    meta("openai/gpt-5.5", "premium", ctx=400_000, caps=("function-calling", "vision", "json", "reasoning"), pin=5.0, pout=20.0),
    meta("openai/gpt-5.4-mini", "cheap", ctx=400_000, pin=0.2, pout=0.8),
    meta("gemini/gemini-2.5-flash", "cheap", ctx=1_000_000, caps=("function-calling", "vision")),
])


def ollama_server(models):
    """models: {name: {"caps": [...], "ctx": int, "loaded": bool, "size": int}}.
    Returns (http_get, http_post) fakes. ``None`` models means unreachable."""
    def get(url, timeout):
        if models is None:
            return None
        if url.endswith("/api/tags"):
            return {"models": [{"name": n, "size": v.get("size", 1_000_000_000),
                                "details": {"family": "fam", "parameter_size": "7B",
                                            "quantization_level": "Q4"},
                                **v.get("tags_extra", {})}
                               for n, v in models.items()]}
        if url.endswith("/api/ps"):
            return {"models": [{"name": n, "context_length": v.get("run_ctx", 4096)}
                               for n, v in models.items() if v.get("loaded")]}
        return None

    def post(url, body, timeout):
        if models is None or not url.endswith("/api/show"):
            return None
        v = models.get(body.get("model"))
        if v is None:
            return None
        out = {"model_info": {"general.architecture": "fam",
                              "fam.context_length": v.get("ctx", 32768)}}
        if "caps" in v:
            out["capabilities"] = list(v["caps"])
        out.update(v.get("show_extra", {}))
        return out

    return get, post


def make_probes(*, ollama=None, claude=None, usage=None, codex=None, gemini=None,
                env=None, resets=None, codex_pressure=None, registry=REGISTRY,
                gemini_login=False, ping=None, ping_cache=None, ollama_base=None,
                claude_login="unknown", claude_oauth=False, ollama_signin="unknown"):
    """claude/codex/gemini: a binary path string, or None for absent.
    codex: (path, login_status_text).  usage: (state, pressure).
    claude_login: "logged_in" / "logged_out" / "unknown" (the fake
    `claude auth status --json` result). claude_oauth: whether the fake
    `~/.claude.json` has an oauthAccount key. ollama_signin: "signed_in" /
    "signed_out" / "unknown" (the fake `POST /api/me` status of the Ollama
    daemon), or an Exception instance to raise; "unknown" never authorizes a
    cloud-backed Ollama model."""
    def signin(base):
        if isinstance(ollama_signin, Exception):
            raise ollama_signin
        return ollama_signin

    get, post = ollama_server(ollama)
    codex_path, codex_text = codex if codex else (None, "")

    def run_cmd(argv, timeout):
        if argv[1:3] == ["login", "status"]:
            return (0, codex_text) if "not logged in" not in codex_text.lower() else (1, codex_text)
        raise AssertionError(f"unexpected command {argv}")

    return Probes(
        environ=dict(env or {}),
        ollama_base=lambda: ollama_base or "http://127.0.0.1:11434",
        http_get=get, http_post=post, run_cmd=run_cmd,
        find_claude=lambda: claude,
        claude_auth_status=lambda binary: claude_login,
        ollama_signin_status=signin,
        claude_oauth_present=lambda: claude_oauth,
        find_codex=lambda: codex_path, find_gemini=lambda: gemini,
        codex_models=lambda: ["gpt-5.5"], gemini_models=lambda: ["gemini-2.5-flash"],
        path_exists=lambda p: gemini_login and "oauth_creds" in p,
        usage_reading=lambda: SimpleNamespace(state=usage[0], pressure=usage[1]) if usage
        else SimpleNamespace(state="unknown", pressure=None),
        resets=lambda: dict(resets or {}),
        codex_pressure=lambda: codex_pressure,
        registry=lambda: registry, now=lambda: NOW,
        ping=ping or (lambda provider, key: (_ for _ in ()).throw(AssertionError("auth ping must not run"))),
        ping_cache=lambda: ping_cache or Path(tempfile.mkdtemp(prefix="ping-cache-")) / "c.json",
    )
