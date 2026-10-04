"""Auto-detect what the user actually has. Read-only; never prints a secret.

Every probe goes through :class:`Probes`, so tests substitute fakes and no test
touches the real Ollama, CLIs or environment. What is read:

* Ollama: ``/api/tags``, ``/api/show`` (per model) and ``/api/ps``. Nothing else.
* Claude Code subscription: the cached ``usage.json`` via
  ``proxy.quota_pressure`` (a local file, no network) and the ``claude`` binary
  path. Credentials are never opened.
* Codex: the binary path and ``codex login status`` (reads local login state,
  spends nothing). Only the classification (ChatGPT vs API key vs none) is kept,
  never the raw output.
* Gemini CLI: the binary path and whether a login file EXISTS (its contents are
  never read).
* API providers: environment variable NAMES that are set. Values are not stored.
* Benches from ``provider_reset``; Codex pressure from ``quota_balance``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from llm_router import paths
from llm_router.resolver import profile as profile_mod
from llm_router.resolver.types import (
    CAP_NO,
    CAP_UNKNOWN,
    CAP_YES,
    PRIVACY_CLOUD,
    PRIVACY_LOCAL,
    ROUTE_API,
    ROUTE_LOCAL,
    ROUTE_SUBSCRIPTION,
    SRC_DECLARED,
    Cap,
    Inventory,
    ModelEntry,
    Quota,
    SourceStatus,
)

HTTP_TIMEOUT_S = 2.0
CMD_TIMEOUT_S = 8.0

#: Chat providers an API key can unlock, with the variable NAME each is read
#: from. Same names the router's config uses (``Config._PROVIDER_MAP``); media
#: providers (fal, stability, elevenlabs, runway, replicate) are not chat models.
API_KEY_ENV: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "groq": "GROQ_API_KEY",
    "xai": "XAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "together": "TOGETHER_API_KEY",
    "perplexity": "PERPLEXITYAI_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "cohere": "COHERE_API_KEY",
}

_CLAUDE_ALIASES = ("opus", "sonnet", "haiku")
_REGISTRY_TIER = {"local": None, "cheap": "EASY", "mid": "MEDIUM", "premium": "FRONTIER"}
_REGISTRY_CAP = {"function-calling": "tools", "vision": "vision", "json": "json",
                 "reasoning": "thinking"}
_LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")
_ROUTE_ORDER = {ROUTE_LOCAL: 0, ROUTE_SUBSCRIPTION: 1, ROUTE_API: 2}


# --------------------------------------------------------------------- probes

def _http_json(method: str, url: str, body: dict | None, timeout: float) -> Any | None:
    try:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local/explicit host
            return json.loads(resp.read())
    except Exception:  # noqa: BLE001 - unreachable/garbled is "unknown", never an exception
        return None


def _run_cmd(argv: list[str], timeout: float) -> tuple[int, str]:
    from llm_router.safe_subprocess import safe_subprocess_run

    try:
        cp = safe_subprocess_run(*argv, timeout=int(timeout), capture_output=True, text=True)
        return cp.returncode, (cp.stdout or "") + (cp.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 127, f"{type(exc).__name__}"


def _usage_reading():
    from llm_router.proxy import quota_pressure

    return quota_pressure.read()


def _resets() -> dict[str, float]:
    from llm_router import provider_reset

    return provider_reset.all_provider_resets()


def _codex_pressure() -> float | None:
    if not paths.state_path("codex_quota.json").exists():
        return None   # no counter yet: unknown, not 0.0
    from llm_router.quota_balance import get_codex_pressure

    try:
        from llm_router.config import get_config

        return get_codex_pressure(get_config().codex_daily_limit)
    except Exception:  # noqa: BLE001
        return get_codex_pressure()


def _registry():
    from llm_router.model_registry import ModelRegistry

    return ModelRegistry.load_default()


def _find_claude() -> str | None:
    from llm_router.claude_agent import find_claude_binary

    return find_claude_binary()


def _find_codex() -> str | None:
    from llm_router.codex_agent import find_codex_binary

    return find_codex_binary()


def _find_gemini() -> str | None:
    from llm_router.gemini_cli_agent import find_gemini_binary

    return find_gemini_binary()


def _codex_models() -> list[str]:
    from llm_router.codex_agent import CODEX_MODELS

    return list(CODEX_MODELS)


def _gemini_models() -> list[str]:
    from llm_router.gemini_cli_agent import GEMINI_MODELS

    return list(GEMINI_MODELS)


def _ollama_base() -> str:
    from llm_router.model_discovery import _ollama_url

    url = _ollama_url().strip()
    return url if "://" in url else f"http://{url}"


@dataclass
class Probes:
    """Everything the inventory reads. Tests replace fields with fakes."""

    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    ollama_base: Callable[[], str] = _ollama_base
    http_get: Callable[[str, float], Any | None] = lambda u, t: _http_json("GET", u, None, t)
    http_post: Callable[[str, dict, float], Any | None] = lambda u, b, t: _http_json("POST", u, b, t)
    run_cmd: Callable[[list[str], float], tuple[int, str]] = _run_cmd
    find_claude: Callable[[], str | None] = _find_claude
    find_codex: Callable[[], str | None] = _find_codex
    find_gemini: Callable[[], str | None] = _find_gemini
    codex_models: Callable[[], list[str]] = _codex_models
    gemini_models: Callable[[], list[str]] = _gemini_models
    path_exists: Callable[[str], bool] = lambda p: Path(p).expanduser().exists()
    usage_reading: Callable[[], Any] = _usage_reading
    resets: Callable[[], dict[str, float]] = _resets
    codex_pressure: Callable[[], float | None] = _codex_pressure
    registry: Callable[[], Any] = _registry
    now: Callable[[], float] = time.time


# ------------------------------------------------------------------- registry

def _registry_meta(reg: Any, model_id: str) -> Any | None:
    try:
        return reg.get(model_id)
    except Exception:  # noqa: BLE001
        return None


def _from_registry(meta: Any) -> dict[str, Any]:
    """Declared facts (context, caps, prices, prior tier) from a registry row."""
    if meta is None:
        return {"caps": {}, "context": None, "pin": None, "pout": None, "prior": None}
    caps = {}
    for raw, name in _REGISTRY_CAP.items():
        if raw in meta.capabilities:
            caps[name] = Cap(CAP_YES, SRC_DECLARED, "curated registry")
    tier = getattr(meta.tier, "value", str(meta.tier))
    return {
        "caps": caps,
        "context": meta.context_window or None,
        "pin": meta.price_per_1m_input_usd,
        "pout": meta.price_per_1m_output_usd,
        "prior": _REGISTRY_TIER.get(tier),
    }


def _quota_for(provider_key: str, resets: dict[str, float], *, pressure: float | None,
               pressure_detail: str, state_if_ok: str = "ok",
               unknown_state: str = "unknown") -> Quota:
    until = resets.get(provider_key)
    if until is not None:
        return Quota("benched", pressure, until, "provider reported a usage limit; skipped until it resets")
    if pressure is None:
        return Quota(unknown_state, None, None, pressure_detail or "no trustworthy quota reading")
    return Quota(state_if_ok, pressure, None, pressure_detail)


# --------------------------------------------------------------------- ollama

def _ctx_from_model_info(info: Any) -> int | None:
    if not isinstance(info, dict):
        return None
    for key, val in info.items():
        if str(key).endswith(".context_length") and isinstance(val, int) and not isinstance(val, bool):
            return val
    return None


def _inventory_ollama(p: Probes) -> tuple[list[ModelEntry], SourceStatus]:
    from llm_router.model_discovery import _is_completion_model

    base = p.ollama_base().rstrip("/")
    tags = p.http_get(f"{base}/api/tags", HTTP_TIMEOUT_S)
    if not isinstance(tags, dict) or "models" not in tags:
        return [], SourceStatus(False, f"Ollama not reachable at {_safe_host(base)}")
    host = _safe_host(base)
    local = host.split(":")[0].strip("[]") in {h.strip("[]") for h in _LOOPBACK} or host.startswith("[::1]")
    ps = p.http_get(f"{base}/api/ps", HTTP_TIMEOUT_S)
    loaded: dict[str, int | None] = {}
    if isinstance(ps, dict):
        for m in ps.get("models") or []:
            if m.get("name"):
                loaded[m["name"]] = m.get("context_length")

    entries: list[ModelEntry] = []
    for m in tags.get("models") or []:
        name = m.get("name")
        if not name or not _is_completion_model(name):
            continue
        show = p.http_post(f"{base}/api/show", {"model": name}, HTTP_TIMEOUT_S)
        show = show if isinstance(show, dict) else {}
        reported = show.get("capabilities")
        caps: dict[str, Cap] = {}
        if isinstance(reported, list):
            if "embedding" in reported and "completion" not in reported:
                continue
            for cap_name, key in (("tools", "tools"), ("vision", "vision"), ("thinking", "thinking")):
                has = key in reported
                caps[cap_name] = Cap(CAP_YES if has else CAP_NO, SRC_DECLARED,
                                     "ollama /api/show capabilities")
        else:
            for cap_name in ("tools", "vision", "thinking"):
                caps[cap_name] = Cap(CAP_UNKNOWN, SRC_DECLARED, "/api/show reported no capabilities")
        ctx = _ctx_from_model_info(show.get("model_info"))
        notes: list[str] = []
        if not local:
            notes.append(f"remote Ollama host {host}: treated as non-local for privacy")
        run_ctx = loaded.get(name)
        if name in loaded and isinstance(run_ctx, int):
            notes.append(f"currently loaded with a {run_ctx}-token window (the model's maximum may be larger)")
        details = m.get("details") or {}
        if details.get("parameter_size"):
            notes.append(f"{details.get('family', '')} {details['parameter_size']} {details.get('quantization_level', '')}".strip())
        entries.append(ModelEntry(
            id=f"ollama/{name}", provider="ollama", route_kind=ROUTE_LOCAL,
            privacy=PRIVACY_LOCAL if local else PRIVACY_CLOUD,
            exec_path=_safe_base(base), path_verified=True,
            path_detail="listed by the running Ollama server",
            authorized=True, auth_detail="local server, no credential",
            quota=Quota("n/a", None, None, "local"),
            capabilities=caps, context_window=ctx,
            context_source="ollama /api/show model_info" if ctx else "",
            loaded=name in loaded, size_bytes=m.get("size"), notes=tuple(notes),
        ))
    return entries, SourceStatus(True, f"{len(entries)} model(s) at {host}")


def _safe_base(base: str) -> str:
    scheme = base.split("://", 1)[0] if "://" in base else "http"
    return f"{scheme}://{_safe_host(base)}"


def _safe_host(base: str) -> str:
    """host:port only: a base URL may carry credentials in its userinfo."""
    rest = base.split("://", 1)[-1]
    return rest.split("@")[-1].split("/")[0]


# --------------------------------------------------------- subscription CLIs

def _inventory_claude(p: Probes, reg: Any, resets: dict[str, float]) -> tuple[list[ModelEntry], SourceStatus]:
    binary = p.find_claude()
    if not binary:
        return [], SourceStatus(False, "claude CLI not found")
    reading = p.usage_reading()
    state = getattr(reading, "state", "unknown")
    pressure = getattr(reading, "pressure", None)
    if state == "ok" and pressure is not None:
        quota = _quota_for("anthropic", resets, pressure=pressure,
                           pressure_detail="max(session, weekly) from usage.json")
    else:
        quota = _quota_for("anthropic", resets, pressure=None,
                           pressure_detail=f"usage.json {state}; pressure not trusted")
    entries = []
    for alias in _CLAUDE_ALIASES:
        meta = _alias_meta(reg, "anthropic", alias)
        facts = _from_registry(meta)
        entries.append(ModelEntry(
            id=f"claude_subscription/{alias}", provider="anthropic",
            route_kind=ROUTE_SUBSCRIPTION, privacy=PRIVACY_CLOUD, exec_path=binary,
            path_verified=True, path_detail="claude CLI is an executable file",
            authorized=True,
            auth_detail="claude CLI present; login state is not inspected (credentials are never read)",
            quota=quota, capabilities=facts["caps"], context_window=facts["context"],
            context_source="curated registry" if facts["context"] else "",
            price_in_per_mtok=None, price_out_per_mtok=None,
            tier_ceiling=facts["prior"], tier_basis="prior" if facts["prior"] else "unmeasured",
            notes=((f"registry row {meta.id}",) if meta else ()),
        ))
    return entries, SourceStatus(True, f"claude CLI at {binary}; usage.json {state}")


def _alias_meta(reg: Any, provider: str, alias: str) -> Any | None:
    try:
        rows = [m for m in reg.models.values()
                if m.provider == provider and alias in m.id.lower()]
    except Exception:  # noqa: BLE001
        return None
    rows.sort(key=lambda m: m.id)
    return rows[-1] if rows else None


def _codex_login_kind(text: str) -> str:
    low = text.lower()
    if "not logged in" in low or "no auth" in low:
        return "none"
    if "chatgpt" in low:
        return "chatgpt"
    if "api key" in low or "apikey" in low:
        return "api_key"
    return "unknown"


def _inventory_codex(p: Probes, reg: Any, resets: dict[str, float]) -> tuple[list[ModelEntry], SourceStatus]:
    binary = p.find_codex()
    if not binary:
        return [], SourceStatus(False, "codex CLI not found")
    rc, out = p.run_cmd([binary, "login", "status"], CMD_TIMEOUT_S)
    kind = _codex_login_kind(out) if rc == 0 else "none"
    authorized = kind in ("chatgpt", "api_key")
    route = ROUTE_API if kind == "api_key" else ROUTE_SUBSCRIPTION
    pressure = p.codex_pressure() if kind != "api_key" else None
    quota = _quota_for("codex", resets, pressure=pressure,
                       pressure_detail="estimate from the local request counter" if pressure is not None
                       else "no local request counter yet",
                       state_if_ok="ok")
    auth_detail = {"chatgpt": "codex login status: ChatGPT account",
                   "api_key": "codex login status: API key (metered billing)",
                   "none": "codex login status: not logged in",
                   "unknown": "codex login status output not recognised"}[kind]
    entries = []
    for name in p.codex_models():
        facts = _from_registry(_registry_meta(reg, f"openai/{name}"))
        entries.append(ModelEntry(
            id=f"codex/{name}", provider="codex", route_kind=route, privacy=PRIVACY_CLOUD,
            exec_path=binary, path_verified=authorized,
            path_detail="codex CLI is executable and reports a login" if authorized
            else "codex CLI found but not logged in",
            authorized=authorized, auth_detail=auth_detail, quota=quota,
            capabilities=facts["caps"], context_window=facts["context"],
            context_source="curated registry" if facts["context"] else "",
            tier_ceiling=facts["prior"], tier_basis="prior" if facts["prior"] else "unmeasured",
        ))
    return entries, SourceStatus(authorized, f"codex CLI at {binary}; {auth_detail}")


def _inventory_gemini(p: Probes, reg: Any, resets: dict[str, float]) -> tuple[list[ModelEntry], SourceStatus]:
    binary = p.find_gemini()
    if not binary:
        return [], SourceStatus(False, "gemini CLI not found")
    if p.path_exists("~/.gemini/oauth_creds.json"):
        authorized, auth_detail = True, "gemini login file present (contents not read)"
    elif p.environ.get("GEMINI_API_KEY"):
        authorized, auth_detail = True, "GEMINI_API_KEY is set"
    else:
        authorized, auth_detail = False, "no gemini login file and no GEMINI_API_KEY"
    quota = _quota_for("gemini_cli", resets, pressure=None,
                       pressure_detail="no cheap local gemini quota reading")
    entries = []
    for name in p.gemini_models():
        facts = _from_registry(_registry_meta(reg, f"gemini/{name}"))
        entries.append(ModelEntry(
            id=f"gemini_cli/{name}", provider="gemini_cli", route_kind=ROUTE_SUBSCRIPTION,
            privacy=PRIVACY_CLOUD, exec_path=binary, path_verified=authorized,
            path_detail="gemini CLI is executable and a login exists" if authorized
            else "gemini CLI found but no login",
            authorized=authorized, auth_detail=auth_detail, quota=quota,
            capabilities=facts["caps"], context_window=facts["context"],
            context_source="curated registry" if facts["context"] else "",
            tier_ceiling=facts["prior"], tier_basis="prior" if facts["prior"] else "unmeasured",
        ))
    return entries, SourceStatus(authorized, f"gemini CLI at {binary}; {auth_detail}")


# ------------------------------------------------------------------ API keys

def _inventory_api(p: Probes, reg: Any, resets: dict[str, float]) -> tuple[list[ModelEntry], list[str]]:
    names: list[str] = []
    entries: list[ModelEntry] = []
    try:
        rows = sorted(reg.models.values(), key=lambda m: m.id)
    except Exception:  # noqa: BLE001
        rows = []
    for provider, env_name in API_KEY_ENV.items():
        if not p.environ.get(env_name):
            continue
        names.append(env_name)
        for meta in rows:
            if meta.provider != provider and not (
                    provider == "gemini" and meta.provider == "google"):
                continue
            facts = _from_registry(meta)
            entries.append(ModelEntry(
                id=meta.id, provider=provider, route_kind=ROUTE_API, privacy=PRIVACY_CLOUD,
                exec_path=f"api:{provider}", path_verified=False,
                path_detail=("key present; not verified by a round-trip yet "
                             "(`llm-router calibrate --allow-paid`)"),
                authorized=True, auth_detail=f"{env_name} is set",
                quota=_quota_for(provider, resets, pressure=None,
                                 pressure_detail="pay-per-use; no quota window",
                                 state_if_ok="metered", unknown_state="metered"),
                capabilities=facts["caps"], context_window=facts["context"],
                context_source="curated registry" if facts["context"] else "",
                price_in_per_mtok=facts["pin"], price_out_per_mtok=facts["pout"],
                tier_ceiling=facts["prior"], tier_basis="prior" if facts["prior"] else "unmeasured",
            ))
    return entries, names


# ------------------------------------------------------------------ assemble

def collect_inventory(
    probes: Probes | None = None,
    *,
    profiles: dict | None = None,
    previous: Inventory | None = None,
) -> Inventory:
    """Detect the setup. ``profiles`` defaults to the stored calibration;
    ``previous`` (an earlier snapshot) marks models that have since vanished."""
    p = probes or Probes()
    profs = profile_mod.load_profiles() if profiles is None else profiles
    try:
        reg = p.registry()
    except Exception:  # noqa: BLE001
        reg = None
    try:
        resets = p.resets()
    except Exception:  # noqa: BLE001
        resets = {}

    models: list[ModelEntry] = []
    sources: dict[str, SourceStatus] = {}
    for name, fn in (("ollama", lambda: _inventory_ollama(p)),
                     ("claude", lambda: _inventory_claude(p, reg, resets)),
                     ("codex", lambda: _inventory_codex(p, reg, resets)),
                     ("gemini_cli", lambda: _inventory_gemini(p, reg, resets))):
        try:
            found, status = fn()
        except Exception as exc:  # noqa: BLE001 - one broken probe must not blank the inventory
            found, status = [], SourceStatus(False, f"probe failed: {type(exc).__name__}")
        models.extend(found)
        sources[name] = status
    api_entries, key_names = _inventory_api(p, reg, resets)
    models.extend(api_entries)
    sources["api_keys"] = SourceStatus(bool(key_names), f"{len(key_names)} provider key(s) set (names only)")

    merged = [
        profile_mod.apply_profile(
            m, profs.get(m.id),
            prior_tier=m.tier_ceiling if m.tier_basis == "prior" else None)
        for m in models
    ]
    merged.sort(key=lambda m: (_ROUTE_ORDER[m.route_kind], m.id))
    inv = Inventory(generated_at=p.now(), models=merged, sources=sources, api_key_names=key_names)
    return reconcile(previous, inv) if previous is not None else inv


def reconcile(previous: Inventory, live: Inventory) -> Inventory:
    """Keep models that were in ``previous`` but are gone now, marked
    ``present=False`` so a stale snapshot can never route to them."""
    live_ids = {m.id for m in live.models}
    gone = [replace(m, present=False,
                    notes=m.notes + ("removed since the last inventory",))
            for m in previous.models if m.id not in live_ids and m.present]
    out = replace(live, models=live.models + gone, removed=sorted(m.id for m in gone))
    return out


# --------------------------------------------------------------- persistence

def snapshot_path() -> Path:
    return paths.state_path("inventory.json")


def save_snapshot(inv: Inventory, path: Path | None = None) -> Path:
    from llm_router.resolver.types import inventory_to_dict

    p = path or snapshot_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(inventory_to_dict(inv), indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def load_snapshot(path: Path | None = None) -> Inventory | None:
    from llm_router.resolver.types import inventory_from_dict

    try:
        return inventory_from_dict(json.loads((path or snapshot_path()).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ------------------------------------------------------------------- display

def _fmt_quota(q: Quota) -> str:
    if q.state == "benched":
        return "benched"
    if q.pressure is not None:
        return f"{q.state} {q.pressure * 100:.0f}%"
    return q.state


def _fmt_caps(m: ModelEntry) -> str:
    parts = []
    for name in ("tools", "vision", "thinking", "json", "edit"):
        c = m.capabilities.get(name)
        if c and c.state != CAP_UNKNOWN:
            mark = "+" if c.state == CAP_YES else "-"
            parts.append(f"{mark}{name}{'*' if c.source == 'measured' else ''}")
    return " ".join(parts) or "?"


def render_table(inv: Inventory) -> str:
    rows = [("MODEL", "ROUTE", "PRIVACY", "PATH", "AUTH", "QUOTA", "CTX", "TIER", "CAPS (*=measured)")]
    for m in inv.models:
        rows.append((
            m.id + ("" if m.present else " (removed)"), m.route_kind, m.privacy,
            "yes" if m.path_verified else "no", "yes" if m.authorized else "no",
            _fmt_quota(m.quota),
            f"{m.effective_context // 1000}k" if m.effective_context else "?",
            f"{m.tier_ceiling or '-'}/{m.tier_basis}", _fmt_caps(m),
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    lines.append("")
    for name, st in inv.sources.items():
        lines.append(f"{'ok ' if st.ok else '-- '}{name}: {st.detail}")
    if inv.api_key_names:
        lines.append("API key variables set (names only): " + ", ".join(inv.api_key_names))
    if inv.removed:
        lines.append("Removed since the last inventory: " + ", ".join(inv.removed))
    return "\n".join(lines)
