"""One resource map: every thing the router can spend, with its state.

Rows are subscriptions seats (claude, codex, gemini_cli), local models and API
keys. Each row carries status and reason, quota values labelled with where they
came from, marginal cost, capabilities, per-model limits and an
``effective_priority`` from the user's routing policy. ``build`` is pure given a
:class:`MapInputs`; the default inputs read this machine. Key values are never
read into the map, only whether a variable is set.

The CLI (``llm-router map``) and the MCP status view (``view="map"``) both go
through :func:`view_json`, so they return the same bytes apart from timestamps.
Routing readers are a separate branch (P1.8 task 6); this module is the source
they will read.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA = 1
MAP_FILE_NAME = "resource_map.json"
REFRESH_TTL_S = 300
PROVENANCE = ("measured", "estimated", "default", "stale")
TIMESTAMP_KEYS = frozenset({"generated_at", "as_of", "reset_at"})

# api key variable -> (chain provider, display name)
_KEY_PROVIDERS = {
    "ANTHROPIC_API_KEY": "anthropic",
    "OPENAI_API_KEY": "openai",
    "GEMINI_API_KEY": "gemini",
    "GOOGLE_API_KEY": "gemini",
    "PERPLEXITY_API_KEY": "perplexity",
    "GROQ_API_KEY": "groq",
    "DEEPSEEK_API_KEY": "deepseek",
    "MOONSHOT_API_KEY": "moonshot",
}
DEFAULT_CODEX_DAILY = 1000
DEFAULT_GEMINI_DAILY = 1500


def quota_value(value: Any, provenance: str, as_of: float | None = None) -> dict:
    """The one shape of a quota value: ``{value, provenance, as_of}``."""
    if provenance not in PROVENANCE:
        raise ValueError(f"provenance must be one of {PROVENANCE}, got {provenance!r}")
    return {"value": value, "provenance": provenance, "as_of": _iso(as_of)}


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ── inputs ──────────────────────────────────────────────────────────────────

HttpGet = Callable[[str], Any]
HttpPost = Callable[[str, dict], Any]


def _http_get(url: str) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _http_post(url: str, body: dict) -> Any:
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2.0) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _default_seats():
    from llm_router import seats as _s
    return _s.detect_seats()


def _default_claude_reading() -> tuple[float | None, str, float | None]:
    from llm_router.claude_usage import get_claude_pressure_reading
    return get_claude_pressure_reading()


def _default_codex_counter() -> dict | None:
    from llm_router import paths
    try:
        return json.loads(paths.state_path("codex_quota.json").read_text())
    except (OSError, ValueError):
        return None


def _default_gemini_quota() -> dict | None:
    """Gemini quota with its source: a fresh ``gemini /stats`` cache entry is
    ``measured``; our own request counter (it carries a ``date``) is
    ``estimated``; no data at all is None (the caller labels that ``default``)."""
    try:
        from llm_router import gemini_cli_quota as g

        cached = g._load_quota_cache()
        if cached:
            src, data = ("estimated" if "date" in cached else "measured"), cached
        else:
            data = g._get_local_quota()
            src = "estimated"
        if not data:
            return None
        count = data.get("count", 0)
        limit = data.get("daily_limit", DEFAULT_GEMINI_DAILY)
        return {"provenance": src, "daily_limit": limit,
                "pressure": min(1.0, max(0.0, count / limit if limit else 0.0))}
    except Exception:
        return None


def codex_rate_limits(sessions_dir: Path, *, max_files: int = 20) -> dict | None:
    """Newest ``rate_limits`` object found in Codex rollout files, or None.

    Walks ``sessions_dir/**/rollout-*.jsonl`` newest first, and within a file
    takes the last line that carries ``rate_limits``. Returns
    ``{"primary": {...}, "secondary": {...}, "as_of": mtime}`` with the
    fields as Codex wrote them (``used_percent``, ``window_minutes``,
    ``resets_at``). The line shape is not verified against a live rollout on
    this machine; the reader finds the key at any depth and tolerates absence.
    """
    try:
        files = sorted(sessions_dir.glob("**/rollout-*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]
    except OSError:
        return None
    for f in files:
        found = None
        try:
            for line in f.read_text(errors="replace").splitlines():
                if '"rate_limits"' not in line:
                    continue
                try:
                    found = _find_key(json.loads(line), "rate_limits") or found
                except ValueError:
                    continue
            if isinstance(found, dict) and (found.get("primary") or found.get("secondary")):
                return {**found, "as_of": f.stat().st_mtime}
        except OSError:
            continue
    return None


def _find_key(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            hit = _find_key(v, key)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find_key(v, key)
            if hit is not None:
                return hit
    return None


@dataclass
class MapInputs:
    """Everything ``build`` reads. Tests replace the fields; nothing is global."""
    seats: Any = None                       # seats.Seats; None -> detect
    env: dict = field(default_factory=lambda: dict(os.environ))
    ollama_url: str = ""
    http_get: HttpGet = _http_get
    http_post: HttpPost = _http_post
    claude_reading: Callable[[], tuple] = _default_claude_reading
    codex_counter: Callable[[], dict | None] = _default_codex_counter
    codex_sessions: Path | None = None
    gemini_quota: Callable[[], dict | None] = _default_gemini_quota
    # local_eligibility.json contents ({model: {...}}). P2.10 writes that file;
    # until it exists every row's competence is null, which means "not measured".
    competence: dict | None = None
    competence_path: Path | None = None
    policy: str | None = None
    codex_daily_limit: int = DEFAULT_CODEX_DAILY
    now: float | None = None


# ── rows ────────────────────────────────────────────────────────────────────

def _row(resource: str, kind: str, chain_id: str, **kw) -> dict:
    row = {
        "resource": resource, "kind": kind, "chain_id": chain_id,
        "plan": None, "status": "absent", "reason": "", "quota": {},
        "reset_at": None, "marginal_cost": {"value": None, "label": ""},
        "capabilities": [], "limits": {}, "competence": None,
        "effective_priority": None,
    }
    row.update(kw)
    return row


def _claude_row(seat, inp: MapInputs, now: float) -> dict:
    row = _row("claude", "subscription", "anthropic/claude-subscription",
               plan=seat.plan,
               marginal_cost={"value": 0.0, "label": "included in the subscription seat"},
               capabilities=["generate", "tools", "vision", "thinking"])
    if not seat.present:
        row["reason"] = "no claude.ai login found" if seat.kind != "api-key" else "API-key login, not a subscription seat"
        return row
    row["status"], row["reason"] = "connected", f"logged in via {seat.kind}"
    value, state, as_of = inp.claude_reading()
    if value is None:
        row["quota"]["pressure"] = quota_value(None, "default", None)
        row["reason"] += f"; usage reading {state}"
    else:
        row["quota"]["pressure"] = quota_value(value, "measured", as_of)
    return row


def _window_expired(win: dict, as_of: float, now: float, *, default_minutes: int) -> bool:
    """True when the rate-limit window in *win* ended before *now*: its own
    ``resets_at`` if it has one, else ``as_of`` plus ``window_minutes``."""
    resets = win.get("resets_at")
    if isinstance(resets, (int, float)):
        return resets <= now
    minutes = win.get("window_minutes")
    minutes = minutes if isinstance(minutes, (int, float)) and minutes > 0 else default_minutes
    return as_of + minutes * 60 <= now


def _codex_row(seat, inp: MapInputs, now: float) -> dict:
    row = _row("codex", "subscription", "codex/codex-subscription", plan=seat.plan,
               marginal_cost={"value": 0.0, "label": "included in the ChatGPT seat"},
               capabilities=["generate", "tools"])
    if not seat.present:
        row["reason"] = "no ChatGPT login for codex found" if seat.kind != "api-key" else "API-key login, not a seat"
        return row
    row["status"] = "connected"
    row["reason"] = "logged in" + (" (plan claim expired)" if seat.plan_stale else "")
    rl = codex_rate_limits(inp.codex_sessions) if inp.codex_sessions else None
    prim = (rl or {}).get("primary") if rl else None
    if isinstance(prim, dict) and isinstance(prim.get("used_percent"), (int, float)):
        if _window_expired(prim, rl["as_of"], now, default_minutes=300):
            # The window this reading describes has reset: keep it visible as
            # history, never as the current value, and fall through to the counter.
            row["quota"]["last_rollout_pressure"] = quota_value(prim["used_percent"] / 100.0, "stale", rl["as_of"])
        else:
            row["quota"]["pressure"] = quota_value(prim["used_percent"] / 100.0, "measured", rl["as_of"])
            sec = rl.get("secondary")
            if (isinstance(sec, dict) and isinstance(sec.get("used_percent"), (int, float))
                    and not _window_expired(sec, rl["as_of"], now, default_minutes=10080)):
                row["quota"]["weekly_pressure"] = quota_value(sec["used_percent"] / 100.0, "measured", rl["as_of"])
            resets = prim.get("resets_at")
            if isinstance(resets, (int, float)):
                row["reset_at"] = _iso(float(resets))
            return row
    limit = inp.codex_daily_limit
    row["quota"]["daily_limit"] = quota_value(limit, "default", None)
    counter = inp.codex_counter()
    today = datetime.fromtimestamp(now, tz=timezone.utc).date().isoformat()
    if counter and counter.get("date") == today and limit > 0:
        count = int(counter.get("count", 0))
        row["quota"]["used_today"] = quota_value(count, "measured", counter.get("cached_at"))
        row["quota"]["pressure"] = quota_value(min(1.0, count / limit), "estimated", counter.get("cached_at"))
    else:
        row["quota"]["pressure"] = quota_value(0.0, "default", None)
    midnight = (int(now // 86400) + 1) * 86400
    row["reset_at"] = _iso(float(midnight))
    return row


def _gemini_row(seat, inp: MapInputs, now: float) -> dict:
    row = _row("gemini_cli", "subscription", "gemini_cli/gemini-subscription", plan=seat.plan,
               marginal_cost={"value": 0.0, "label": "included in the Google seat"},
               capabilities=["generate", "tools", "vision"])
    if not seat.present:
        row["reason"] = "no gemini login found" if seat.kind != "api-key" else "API-key only, no CLI login"
        return row
    row["status"], row["reason"] = "connected", "gemini CLI logged in"
    q = inp.gemini_quota()
    if q:
        prov = q.get("provenance", "estimated")
        row["quota"]["daily_limit"] = quota_value(q.get("daily_limit", DEFAULT_GEMINI_DAILY), "estimated", None)
        row["quota"]["pressure"] = quota_value(q.get("pressure", 0.0), prov, now)
    else:
        row["quota"]["daily_limit"] = quota_value(DEFAULT_GEMINI_DAILY, "default", None)
        row["quota"]["pressure"] = quota_value(0.0, "default", None)
    return row


def _ctx_from_info(info: Any) -> int | None:
    if isinstance(info, dict):
        for k, v in info.items():
            if str(k).endswith(".context_length") and isinstance(v, int) and not isinstance(v, bool):
                return v
    return None


def _ollama_rows(inp: MapInputs, now: float) -> list[dict]:
    from llm_router import local_models

    base = (inp.ollama_url or inp.env.get("OLLAMA_URL") or inp.env.get("OLLAMA_API_BASE")
            or "http://localhost:11434").rstrip("/")
    tags = inp.http_get(f"{base}/api/tags")
    if not isinstance(tags, dict) or "models" not in tags:
        return [_row("ollama", "local", "ollama", reason=f"Ollama not reachable at {base}")]
    rows = []
    seen: set[str] = set()
    for m in tags.get("models") or []:
        name = m.get("name")
        if not name or name in seen:   # /api/tags can list a name twice
            continue
        seen.add(name)
        show = inp.http_post(f"{base}/api/show", {"model": name})
        show = show if isinstance(show, dict) else {}
        reported = show.get("capabilities")
        caps: list[str] = []
        known = isinstance(reported, list)
        if known:
            if "completion" in reported:
                caps.extend(c for c in ("tools", "vision", "thinking") if c in reported)
            if "embedding" in reported:
                caps.append("embedding")
        declared = local_models.roles(name)
        serves_generate = (not known or "completion" in reported) and "generate" in declared
        if serves_generate:
            caps.insert(0, "generate")
        for r in sorted(declared - {"generate"}):
            caps.append(r)
        if serves_generate:
            reason = "listed by /api/tags"
        elif "generate" not in declared:
            reason = f"declared role {sorted(declared)} in local_models.ROLES; not a generate model"
        else:
            reason = "/api/show lists no completion capability"
        details = m.get("details") or {}
        comp = (inp.competence or {}).get(name)
        rows.append(_row(
            f"ollama/{name}", "local", f"ollama/{name}",
            status="connected", reason=reason,
            marginal_cost={"value": 0.0, "label": "local; electricity not counted"},
            capabilities=caps,
            limits={
                "num_ctx": local_models.num_ctx(name),
                "max_context": _ctx_from_info(show.get("model_info")),
                "parameter_size": details.get("parameter_size"),
                "size_bytes": m.get("size"),
            },
            competence=comp,
        ))
    if not rows:
        rows.append(_row("ollama", "local", "ollama", reason="Ollama reachable, no models installed"))
    return rows


def _key_rows(inp: MapInputs) -> list[dict]:
    seats = inp.seats
    keys = dict(getattr(seats, "api_keys", None) or {})
    for var in _KEY_PROVIDERS:
        if inp.env.get(var):
            keys[var] = True
    rows = []
    for var, present in sorted(keys.items()):
        if not present:
            continue
        provider = _KEY_PROVIDERS.get(var, var.removesuffix("_API_KEY").lower())
        rows.append(_row(
            f"api/{var}", "api", f"{provider}/api",
            status="connected", reason=f"{var} is set (value not read)",
            marginal_cost={"value": None, "label": "billed per token"},
            capabilities=["generate"],
            quota={"rpm_tpm": quota_value(None, "default", None)},
        ))
    return rows


def _prioritise(rows: list[dict], policy: str, now: float) -> None:
    from llm_router.user_routing_policy import apply_routing_policy
    connected = [r for r in rows if r["status"] == "connected" and "generate" in r["capabilities"]]
    ordered = apply_routing_policy([r["chain_id"] for r in connected], policy)
    rank = {cid: i + 1 for i, cid in enumerate(ordered)}
    for r in connected:
        r["effective_priority"] = rank[r["chain_id"]]


def _competence_default_path() -> Path:
    from llm_router import paths
    return paths.state_path("local_eligibility.json")


def build(inputs: MapInputs | None = None) -> dict:
    """The map, built from *inputs* (default: this machine)."""
    inp = inputs or MapInputs(competence_path=_competence_default_path())
    now = time.time() if inp.now is None else inp.now
    if inp.seats is None:
        inp.seats = _default_seats()
    if inp.competence is None and inp.competence_path is not None:
        try:
            loaded = json.loads(inp.competence_path.read_text())
            inp.competence = loaded if isinstance(loaded, dict) else None
        except (OSError, ValueError):
            pass
    if inp.codex_sessions is None:
        inp.codex_sessions = Path.home() / ".codex" / "sessions"
    seats = inp.seats
    rows = [
        _claude_row(seats.claude, inp, now),
        _codex_row(seats.codex, inp, now),
        _gemini_row(seats.gemini, inp, now),
        *_ollama_rows(inp, now),
        *_key_rows(inp),
    ]
    policy = inp.policy
    if policy is None:
        try:
            from llm_router.config import get_config
            policy = get_config().llm_router_routing_policy
        except Exception:
            policy = "balanced"
    _prioritise(rows, policy, now)
    return {"schema": SCHEMA, "generated_at": _iso(now), "policy": policy, "resources": rows}


# ── file, doors ─────────────────────────────────────────────────────────────

def map_path() -> Path:
    from llm_router import paths
    return paths.state_path(MAP_FILE_NAME)


def write_map(data: dict, path: Path | None = None) -> Path:
    """Atomic write: a reader sees the old file or the new one, never half."""
    path = path or map_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".resource_map.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def load_map(path: Path | None = None) -> dict | None:
    try:
        data = json.loads((path or map_path()).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA else None


def age_seconds(data: dict, now: float | None = None) -> float | None:
    try:
        then = datetime.fromisoformat(data["generated_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None
    return (time.time() if now is None else now) - then


def refresh_if_stale(inputs: MapInputs | None = None, *, path: Path | None = None,
                     max_age_s: float = REFRESH_TTL_S, now: float | None = None) -> dict:
    """The file's map when it is at most *max_age_s* old, else rebuild and write."""
    cur = load_map(path)
    if cur is not None:
        age = age_seconds(cur, now)
        if age is not None and 0 <= age <= max_age_s:
            return cur
    data = build(inputs)
    write_map(data, path)
    return data


def strip_timestamps(obj: Any) -> Any:
    """Drop every timestamp field, for byte comparison of two views."""
    if isinstance(obj, dict):
        return {k: strip_timestamps(v) for k, v in obj.items() if k not in TIMESTAMP_KEYS}
    if isinstance(obj, list):
        return [strip_timestamps(v) for v in obj]
    return obj


def view_json(data: dict | None = None) -> str:
    """The JSON both doors print. Sorted keys so equal maps are equal bytes."""
    return json.dumps(data if data is not None else refresh_if_stale(), indent=2, sort_keys=True)


def render_table(data: dict) -> str:
    cols = ("resource", "kind", "status", "plan", "pressure", "prio", "reason")
    out = []
    for r in data["resources"]:
        p = (r["quota"].get("pressure") or {})
        pv = p.get("value")
        out.append((
            r["resource"], r["kind"], r["status"], r["plan"] or "-",
            "-" if pv is None else f"{pv:.0%} ({p['provenance']})",
            str(r["effective_priority"] or "-"), r["reason"],
        ))
    widths = [max(len(c), *(len(row[i]) for row in out)) if out else len(c) for i, c in enumerate(cols)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(cols, widths)).rstrip()]
    lines += ["  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() for row in out]
    lines.append(f"policy={data['policy']}  generated_at={data['generated_at']}")
    return "\n".join(lines)
