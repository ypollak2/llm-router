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
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

SCHEMA = 1
MAP_FILE_NAME = "resource_map.json"
REFRESH_TTL_S = 300
PROVENANCE = ("measured", "estimated", "default", "stale")
# "writer" (which process built the map) and "carried_from" vary per process like
# a timestamp, so the two doors' byte comparison drops them too.
TIMESTAMP_KEYS = frozenset({"generated_at", "as_of", "reset_at", "writer", "carried_from"})
CARRY_MAX_AGE_S = 3600

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
        import urllib.request   # lazy: the hook imports this module and must not load http.client
        with urllib.request.urlopen(url, timeout=2.0) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _http_post(url: str, body: dict) -> Any:
    try:
        import urllib.request
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
    keys: Callable[[], list] | None = None   # -> list[discovery.keys.KeyInfo]; None -> discover()
    now: float | None = None


# ── rows ────────────────────────────────────────────────────────────────────

def _row(resource: str, kind: str, chain_id: str, **kw) -> dict:
    row = {
        "resource": resource, "kind": kind, "chain_id": chain_id,
        "plan": None, "status": "absent", "reason": "", "quota": {},
        "reset_at": None, "marginal_cost": {"value": None, "label": ""},
        "capabilities": [], "limits": {}, "competence": None, "key": None,
        "effective_priority": None,
        "privacy": "cloud",   # SEC-S6: text sent to this resource leaves the machine unless _ollama_rows says otherwise
    }
    row.update(kw)
    return row


def privacy_class(model: str, base: str) -> str:
    """``local`` when a call to *model* via the Ollama daemon at *base* stays on this machine, else ``cloud``.
    The one definition of "stays local" is ``local_privacy.stays_local`` (loopback daemon, not a ``-cloud`` tag)."""
    from llm_router import local_privacy
    return "local" if local_privacy.stays_local(model, base) else "cloud"


def _not_connected(row: dict, seat, fallback: str) -> dict:
    """Status and reason for a seat that is not connected, from the detector."""
    row["status"] = seat.status if seat.status in ("installed_not_connected", "absent") else "absent"
    row["reason"] = seat.reason or fallback
    return row


def _copilot_row(seat) -> dict:
    # No router provider routes through Copilot, so it carries no `generate`
    # capability and never gets a priority; it is listed so the user can see it.
    row = _row("copilot", "subscription", "copilot", plan=seat.plan,
               marginal_cost={"value": 0.0, "label": "included in the Copilot seat"})
    if seat.present:
        row["status"], row["reason"] = "connected", seat.reason or "signed in"
        return row
    return _not_connected(row, seat, "no GitHub Copilot found")


def _claude_row(seat, inp: MapInputs, now: float) -> dict:
    row = _row("claude", "subscription", "anthropic/claude-subscription",
               plan=seat.plan,
               marginal_cost={"value": 0.0, "label": "included in the subscription seat"},
               capabilities=["generate", "tools", "vision", "thinking"])
    if not seat.present:
        return _not_connected(row, seat, "no claude.ai login found")
    row["status"], row["reason"] = "connected", seat.reason or f"logged in via {seat.kind}"
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
        return _not_connected(row, seat, "no ChatGPT login for codex found")
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
        return _not_connected(row, seat, "no gemini login found")
    row["status"], row["reason"] = "connected", seat.reason or "gemini CLI logged in"
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


def context_window_limit(window: int | None) -> int | str:
    """A model's reported context window, or ``unknown``. ``local_models.DEFAULT_NUM_CTX``
    is the size we request, never a window we know the model has."""
    return window if isinstance(window, int) and not isinstance(window, bool) and window > 0 else UNKNOWN


def _ollama_rows(inp: MapInputs, now: float) -> list[dict]:
    from llm_router import local_models

    base = (inp.ollama_url or inp.env.get("OLLAMA_URL") or inp.env.get("OLLAMA_API_BASE")
            or "http://localhost:11434").rstrip("/")
    tags = inp.http_get(f"{base}/api/tags")
    if not isinstance(tags, dict) or "models" not in tags:
        seat = getattr(inp.seats, "ollama", None)
        status = seat.status if seat is not None and seat.status == "installed_not_connected" else "absent"
        why = seat.reason if status == "installed_not_connected" else f"Ollama not reachable at {base}"
        return [_row("ollama", "local", "ollama", status=status, reason=why,
                     privacy=privacy_class("ollama/-", base))]
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
            privacy=privacy_class(f"ollama/{name}", base),
            marginal_cost={"value": 0.0, "label": "local; electricity not counted"},
            capabilities=caps,
            # D-159: True only when /api/show reported a capabilities list. Without it the
            # list above is just ``generate``, which says nothing about tools or vision.
            capabilities_reported=known,
            digest=m.get("digest"),
            limits={
                "num_ctx": local_models.num_ctx(name),
                "max_context": _ctx_from_info(show.get("model_info")),
                "context_window": context_window_limit(_ctx_from_info(show.get("model_info"))),
                "parameter_size": details.get("parameter_size"),
                "size_bytes": m.get("size"),
            },
            competence=comp,
        ))
    if not rows:
        rows.append(_row("ollama", "local", "ollama", reason="Ollama reachable, no models installed",
                     privacy=privacy_class("ollama/-", base)))
    return rows


def _key_rows(inp: MapInputs) -> list[dict]:
    if inp.keys is not None:
        infos = inp.keys()
    else:
        from llm_router.discovery.keys import discover
        infos = discover(env=inp.env)
    rows = []
    for k in infos:
        rows.append(_row(
            f"api/{k.var}", "api", f"{k.provider}/api",
            status="connected", reason=f"{k.var} is set (value not read)",
            marginal_cost={"value": None, "label": "billed per token" if k.billing == "paid"
                           else f"billing {k.billing}"},
            capabilities=["generate"],
            quota={"rpm_tpm": quota_value(None, "default", None)},
            key={"provider": k.provider, "source": k.source,
                 "usable_by": list(k.usable_by), "billing": k.billing},
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
        _copilot_row(seats.copilot),
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
    from llm_router.map_rediscovery import writer_identity
    return {"schema": SCHEMA, "generated_at": _iso(now), "policy": policy, "resources": rows,
            "writer": writer_identity()}


def carry_connected(old: dict | None, new: dict, now: float,
                    max_age_s: float = CARRY_MAX_AGE_S) -> dict:
    """Keep a subscription row another writer saw connected within *max_age_s*
    when this build only says it could not look (``seats.no_evidence``: the CLI
    is not on this process's PATH, or its probe failed). The carried row keeps
    the first sighting's time, so the carry ends and a real uninstall shows."""
    from llm_router import seats as _seats

    if not isinstance(old, dict):
        return new
    prev = {r.get("resource"): r for r in old.get("resources") or [] if isinstance(r, dict)}
    rows = new.get("resources") or []
    for i, row in enumerate(rows):
        o = prev.get(row.get("resource"))
        if (row.get("kind") != "subscription" or not isinstance(o, dict)
                or o.get("status") != "connected"
                or not _seats.no_evidence(row.get("status"), row.get("reason"))):
            continue
        seen = (o.get("carried_from") or {}).get("seen_at") or old.get("generated_at")
        try:
            age = now - datetime.fromisoformat(seen).timestamp()
        except (TypeError, ValueError):
            continue
        if not 0 <= age <= max_age_s:
            continue
        log.info(
            "resource_map: keeping %s connected (seen %.0fs ago) over %r",
            row.get("resource"), age, row.get("reason"))
        rows[i] = {**o, "carried_from": {"seen_at": seen, "writer": old.get("writer"),
                                         "new_reason": row.get("reason")}}
    return new


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
    from llm_router import map_rediscovery as _mr
    from llm_router.file_lock import exclusive_lock
    sdir = _mr._home()
    # Same non-blocking lock as map_rediscovery.poll: the holder is already
    # rebuilding and logging this diff, so we neither rebuild nor log it again.
    st: dict = {}
    with exclusive_lock(sdir / _mr.LOCK_FILE, timeout=0, status=st) as got:
        if not got and st.get("state") == "held":
            return cur if cur is not None else build(inputs)
        if not got:
            # Locking unavailable (e.g. unwritable LLM_ROUTER_HOME): rebuild
            # unlocked, as before the lock existed, and record why.
            _mr._lost("CHZ-FO-MAP-LOCK-UNAVAILABLE", OSError("map refresh lock unavailable"))
        cur = load_map(path)          # the previous holder may have just refreshed
        if cur is not None:
            age = age_seconds(cur, now)
            if age is not None and 0 <= age <= max_age_s:
                return cur
        data = carry_connected(cur, build(inputs), time.time() if now is None else now)
        try:
            _mr.record_changes(cur, data, source="refresh_if_stale",
                               now=time.time() if now is None else now,
                               path=(path.parent if path is not None else sdir) / _mr.CHANGES_FILE)
        except Exception as exc:  # noqa: BLE001 - the change log never blocks a refresh
            _mr._lost("CHZ-FO-MAP-CHANGES-WRITE", exc)
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


# ── score (P2.3) ──
# Read these module attributes at call time; see docs/prereg/PREREG-P23-quota-score.md.
SCORE_W1 = 10.0
SCORE_W2 = 0.05
SCORE_W3 = 1.0
RESET_FLOOR_H = 0.25
SCORE_PMAX_HIGH = 0.95
UNKNOWN = "unknown"


def _known_number(x, lo=None, hi=None) -> float | None:
    import math

    if type(x) not in (int, float):
        return None
    try:
        value = float(x)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(value):
        return None
    if lo is not None and value < lo:
        return None
    if hi is not None and value > hi:
        return None
    return value


def _quota_pressure(q):
    if not isinstance(q, dict):
        return None
    provenance = q.get("provenance")
    if not isinstance(provenance, str) or provenance not in ("measured", "estimated"):
        return None
    return _known_number(q.get("value"), 0, 1)


def _pmax(resource):
    if not isinstance(resource, dict):
        return None
    quota = resource.get("quota")
    if not isinstance(quota, dict):
        return None
    values = [_quota_pressure(quota.get(k)) for k in ("pressure", "weekly_pressure")]
    known = [v for v in values if v is not None]
    return max(known) if known else None


def quota_pressure(resource) -> float | None:
    """The resource's quota pressure in 0..1: the higher of its session and weekly readings,
    measured or estimated only; ``None`` when neither is known (the P2.3 score's ``pmax``)."""
    return _pmax(resource)


def _reset_in_h(reset_at, now):
    if not isinstance(reset_at, str):
        return None
    try:
        reset = datetime.fromisoformat(reset_at)
        if reset.tzinfo is None:
            reset = reset.replace(tzinfo=timezone.utc)
        hours = (reset.timestamp() - now) / 3600
        return _known_number(hours) if hours > 0 else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _score_resource_name(resource):
    name = resource.get("resource") if isinstance(resource, dict) else None
    return name if isinstance(name, str) else UNKNOWN


def _score_cost(resource):
    cost = resource.get("marginal_cost")
    return _known_number(cost.get("value"), 0) if isinstance(cost, dict) else None


def score(resource, decision=None) -> tuple[float, dict]:
    """Sum known terms only; the reset bonus is positive (prereg §1)."""
    row = resource if isinstance(resource, dict) else {}
    d = decision if isinstance(decision, dict) else {}
    inputs = dict.fromkeys(("effective_priority", "pressure", "reset_in_h", "marginal_cost"))
    terms = dict.fromkeys(("priority", "pressure", "reset", "marginal_cost"))
    p = row.get("effective_priority")
    if type(p) is int and p >= 1 and d.get("priority_ordered") is True:
        known_p = _known_number(p, 1)
        if known_p is not None:
            inputs["effective_priority"] = known_p
            terms["priority"] = 1 / p
    pressure = _pmax(row)
    if pressure is not None:
        inputs["pressure"] = pressure
        terms["pressure"] = -SCORE_W1 * pressure
    now = _known_number(d.get("now"))
    if now is None:
        now = time.time()
    reset = _reset_in_h(row.get("reset_at"), now)
    if reset is not None:
        inputs["reset_in_h"] = reset
        terms["reset"] = SCORE_W2 / max(reset, RESET_FLOOR_H)
    cost = _score_cost(row)
    if cost is not None:
        inputs["marginal_cost"] = cost
        terms["marginal_cost"] = -SCORE_W3 * cost
    value = float(sum(v for v in terms.values() if v is not None))
    return value, {
        "resource": _score_resource_name(row), "score": value,
        "terms": {k: UNKNOWN if v is None else float(v) for k, v in terms.items()},
        "inputs": {k: UNKNOWN if v is None else float(v) for k, v in inputs.items()},
    }


def _eligible(resource, decision) -> str | None:
    row = resource if isinstance(resource, dict) else {}
    d = decision if isinstance(decision, dict) else {}
    status = row.get("status")
    if not isinstance(status, str):
        return "status unknown"
    if status != "connected":
        return "status " + status
    caps = row.get("capabilities")
    if not isinstance(caps, (list, tuple)):
        return "capabilities unknown"
    capabilities = {c for c in caps if isinstance(c, str)}
    if "generate" not in capabilities:
        return "capability generate missing"
    needs = d.get("needs")
    if isinstance(needs, (list, tuple, set, frozenset)):
        if any(n not in capabilities for n in needs if isinstance(n, str)):
            return "capabilities missing"
    competence = row.get("competence")
    task_type = d.get("task_type")
    if isinstance(competence, dict) and isinstance(task_type, str):
        task = competence.get(task_type)
        if isinstance(task, dict):
            tiers = [v for v in task.values() if isinstance(v, dict)]
            if tiers and all(t.get("eligible") is False for t in tiers):
                return "competence failed"
    return None


def order_resources(resources, decision=None) -> tuple[list[dict], list[dict]]:
    if resources is None or isinstance(resources, (str, bytes)):
        return [], []
    try:
        rows = list(resources)
    except Exception:
        return [], []
    ranked, explains = [], []
    for index, row in enumerate(rows):
        reason = _eligible(row, decision)
        if reason is not None:
            explains.append({"resource": _score_resource_name(row), "filtered": reason})
            continue
        value, explain = score(row, decision)
        kind = row.get("kind")
        subscription = isinstance(kind, str) and kind == "subscription"
        cost_group = int(not subscription and _score_cost(row) is None)
        pressure = _pmax(row)
        pressure_group = 0
        if subscription:
            pressure_group = 1 if pressure is None else (2 if pressure >= SCORE_PMAX_HIGH else 0)
        ranked.append(((cost_group, pressure_group, -value, index), row, explain))
        explains.append(explain)
    ranked.sort(key=lambda item: item[0])
    for rank, (_, _, explain) in enumerate(ranked, 1):
        explain["rank"] = rank
    return [row for _, row, _ in ranked], explains


def order_chain(
    chain, map_data, decision=None, *, key=lambda m: m,
    hold_providers=frozenset(), exempt=None,
) -> tuple[list, list[dict]]:
    out = list(chain)
    if not isinstance(map_data, dict):
        return out, []
    from llm_router import map_reader

    resources = map_data.get("resources")
    if not isinstance(resources, (list, tuple)):
        return out, []
    rows = []
    for row in resources:
        if not isinstance(row, dict):
            continue
        kind, cid = row.get("kind"), row.get("chain_id")
        if isinstance(kind, str) and kind == "subscription" and isinstance(cid, str):
            rows.append(row)
    d = dict(decision) if isinstance(decision, dict) else {}
    d["priority_ordered"] = map_reader._expresses_order(map_data.get("policy"))
    ordered, explains = order_resources(rows, d)
    providers = {r["chain_id"].split("/", 1)[0] for r in rows}
    positions = {}
    for row in ordered:
        provider = row["chain_id"].split("/", 1)[0]
        if provider not in positions:
            positions[provider] = len(positions)
    slots = []
    for index, entry in enumerate(out):
        cid = key(entry)
        if not isinstance(cid, str):
            continue
        provider = cid.split("/", 1)[0]
        if provider in providers and provider not in hold_providers and cid != exempt:
            slots.append(index)
    moved = sorted(
        (out[i] for i in slots),
        key=lambda entry: positions.get(key(entry).split("/", 1)[0], len(positions)),
    )
    for index, entry in zip(slots, moved):
        out[index] = entry
    return out, explains


def live_subscription_rows(now=None) -> dict:
    """Build live subscription readings without discovery or Codex rollout reads."""
    from types import SimpleNamespace

    from llm_router import failopen

    timestamp = _known_number(now)
    if timestamp is None:
        timestamp = time.time()
    seat = SimpleNamespace(
        present=True, plan=None, reason="", kind="live", plan_stale=False, status="connected",
    )
    inp = MapInputs(codex_sessions=None)
    rows = []
    for builder in (_claude_row, _codex_row, _gemini_row):
        try:
            rows.append(builder(seat, inp, timestamp))
        except Exception as exc:
            failopen.record("CHZ-FO-QUOTA-SCORE-LIVE-ROW", exc)
    return {"schema": SCHEMA, "policy": None, "resources": rows}
