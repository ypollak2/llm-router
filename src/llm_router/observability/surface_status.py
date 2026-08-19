"""Cross-surface "LLM Router is working" status — one source of truth, many renderers.

Every host (Claude Code, Codex, Gemini CLI, opencode, clawcode, …) appends the
same per-route record to ``~/.llm-router/savings_log.jsonl``, tagged with ``host``,
``model``, ``task_type``/``complexity`` and ``estimated_saved``. Claude Code's
statusline already surfaces that; the other hosts flush it silently. This module
turns the shared log into a single :class:`SurfaceStatus` and renders it the
three ways hosts without a statusline API can show it:

  * :func:`compact_line`     — an inline "⚡ llm_router · 🎯 hermes3:8b code/moderate · $0.03 · ✓" line
  * :func:`terminal_title`   — an OSC escape that sets the terminal title bar
  * :func:`notification`     — an OS-notification payload (rate-limited), or None

It answers all three indicator questions: *is llm_router active* (``active``),
*what did it just route* (``last_model``/``last_task``), and *is it actually
healthy* (``health``). Stdlib-only and fail-soft so hooks can import it on the
critical path without slowing the host down.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from llm_router.tool_surface import route_tool  # CHZ-SURF-01

# ── State locations (overridable for tests via LLM_ROUTER_STATE_DIR) ─────────────
def _state_dir() -> Path:
    return Path(os.environ.get("LLM_ROUTER_STATE_DIR", str(Path.home() / ".llm-router")))


_SAVINGS_LOG = "savings_log.jsonl"
_HEALTH_SNAPSHOT = "health.json"   # optional; written by a future `llm_router doctor`

# Routed within this many seconds → the host is considered actively routing.
ACTIVE_WINDOW_S = 1800             # 30 min

# Provider API-key env vars that mean "an external model is reachable".
_PROVIDER_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "GROQ_API_KEY",
)

HEALTH_OK = "ok"
HEALTH_DEGRADED = "degraded"
HEALTH_DOWN = "down"

# Host strings drift across the codebase (gemini_cli vs gemini-cli, subagent
# variants of claude_code). Each canonical host absorbs its known aliases so the
# indicator matches whatever string a given surface actually stamped into the log.
_HOST_ALIASES: dict[str, set[str]] = {
    "claude_code": {
        "claude_code", "claude-code",
        "claude_code_subagent", "claude_code_subagent_cli",
    },
    "codex": {"codex", "codex_cli", "codex-cli"},
    "gemini_cli": {"gemini_cli", "gemini-cli", "gemini"},
    "opencode": {"opencode", "open-code"},
    "clawcode": {"clawcode", "claw_code", "claw-code"},
    "desktop": {"desktop", "claude_desktop"},
}


def _host_match_set(host: str) -> set[str]:
    return _HOST_ALIASES.get(host, {host})


@dataclass
class SurfaceStatus:
    """A host's current LLM Router state, derived from the shared savings log."""

    host: str
    active: bool                      # routed within ACTIVE_WINDOW_S
    health: str                       # HEALTH_OK | HEALTH_DEGRADED | HEALTH_DOWN
    health_reason: str
    last_model: Optional[str]         # "ollama/hermes3:8b"
    last_task: Optional[str]          # "code/moderate"
    last_age_s: Optional[float]       # seconds since last route, or None
    last_tokens: Optional[int]        # input+output tokens of the last route
    routed_count_session: int         # routes for this host today (UTC day)
    saved_session: float              # $ saved for this host today
    saved_total: float                # $ saved for this host, all-time in the log
    tokens_session: int               # input+output tokens routed for this host today

    def short_model(self) -> str:
        """``ollama/hermes3:8b`` → ``hermes3:8b`` (drop the provider prefix)."""
        if not self.last_model:
            return "—"
        return self.last_model.split("/", 1)[-1]

    def health_glyph(self) -> str:
        return {HEALTH_OK: "✓", HEALTH_DEGRADED: "⚠", HEALTH_DOWN: "✗"}.get(
            self.health, "?"
        )


# ── Reading the shared log ───────────────────────────────────────────────────
def _read_tail(path: Path, max_lines: int = 500) -> list[dict]:
    """Return up to the last ``max_lines`` parsed JSON records from a .jsonl file.

    Reads the whole file but only keeps the tail — savings_log.jsonl is small
    and rotated elsewhere, so this stays cheap. Never raises.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except (OSError, ValueError):
        return []
    records: list[dict] = []
    for line in lines[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                records.append(obj)
        except (ValueError, TypeError):
            continue
    return records


def _read_stats_records(max_rows: int = 2000) -> list[dict]:
    """Durable savings records from the ``savings_stats`` SQLite table.

    ``savings_log.jsonl`` is a transient buffer — ``import_savings_log()`` flushes
    it into ``savings_stats`` and truncates the file. So the file alone leaves the
    indicators blind after any import. Reading ``savings_stats`` gives the durable,
    host-tagged history (and captures gateway/external traffic once it's imported).
    Mapped to the same record shape as the jsonl. Never raises.
    """
    import sqlite3

    db = _state_dir() / "usage.db"
    if not db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT timestamp, host, model_used, task_type, "
            "       estimated_claude_cost_saved, input_tokens, output_tokens "
            "FROM savings_stats ORDER BY id DESC LIMIT ?",
            (max_rows,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    out: list[dict] = []
    for ts, host, model, task, saved, in_tok, out_tok in rows:
        out.append({
            "timestamp": ts,
            "host": host or "claude_code",
            "model": model,
            "task_type": task,
            "estimated_saved": saved or 0.0,
            "input_tokens": in_tok or 0,
            "output_tokens": out_tok or 0,
        })
    return out


def _rec_tokens(rec: dict) -> int:
    """input + output tokens for a savings-log record (0 if absent)."""
    try:
        return int(rec.get("input_tokens") or 0) + int(rec.get("output_tokens") or 0)
    except (ValueError, TypeError):
        return 0


def _parse_ts(rec: dict) -> Optional[float]:
    """Epoch seconds for a record's ISO-8601 ``timestamp``, or None."""
    ts = rec.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        from datetime import datetime

        # fromisoformat handles the "+00:00" offset these records carry.
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return None


def _providers_available(now: float, records: list[dict]) -> bool:
    """True if LLM Router can route *something* — an API key is set, a local model
    answered recently, or any successful route exists in the log at all."""
    if any(os.environ.get(k) for k in _PROVIDER_KEYS):
        return True
    # A recent local (ollama) route proves a model is reachable without a key.
    for rec in reversed(records):
        model = rec.get("model", "")
        if isinstance(model, str) and model.startswith("ollama/"):
            ts = _parse_ts(rec)
            if ts is not None and (now - ts) <= ACTIVE_WINDOW_S:
                return True
    return False


def _read_health_snapshot(now: float) -> Optional[tuple[str, str]]:
    """Optional explicit health override from ``~/.llm-router/health.json`` if a
    doctor/healthcheck wrote one recently. Returns (health, reason) or None."""
    path = _state_dir() / _HEALTH_SNAPSHOT
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    ts = data.get("checked_at")
    # Ignore stale snapshots (>10 min) so a transient blip can't pin health.
    if isinstance(ts, (int, float)) and (now - ts) > 600:
        return None
    status = str(data.get("status", "")).lower()
    if status in (HEALTH_OK, HEALTH_DEGRADED, HEALTH_DOWN):
        return status, str(data.get("reason", ""))
    return None


def _is_usage_stale(now: float, max_age_s: int = 1800) -> bool:
    """True if usage.json is older than ``max_age_s`` (drives the degraded axis)."""
    path = _state_dir() / "usage.json"
    try:
        return (now - path.stat().st_mtime) > max_age_s
    except OSError:
        return True


def compute_status(host: str, now: Optional[float] = None) -> SurfaceStatus:
    """Build the :class:`SurfaceStatus` for ``host`` from the shared savings log."""
    if now is None:
        now = time.time()

    # Durable history (savings_stats) + the live buffer (savings_log.jsonl, not yet
    # imported). A record lives in one or the other — the jsonl is truncated on
    # import — so combining them double-counts nothing. Sort by timestamp so
    # "last route" is correct across both sources.
    records = _read_stats_records() + _read_tail(_state_dir() / _SAVINGS_LOG)
    records.sort(key=lambda r: _parse_ts(r) or 0.0)
    match = _host_match_set(host)
    host_recs = [r for r in records if str(r.get("host", "")) in match]

    # Last route for this host.
    last_model: Optional[str] = None
    last_task: Optional[str] = None
    last_age: Optional[float] = None
    last_tokens: Optional[int] = None
    for rec in reversed(host_recs):
        ts = _parse_ts(rec)
        if ts is None:
            continue
        last_model = rec.get("model") or None
        tt, cx = rec.get("task_type"), rec.get("complexity")
        last_task = f"{tt}/{cx}" if tt and cx else (tt or None)
        last_age = max(0.0, now - ts)
        last_tokens = _rec_tokens(rec)
        break

    # Today's (UTC day) aggregates for this host.
    day_start = now - (now % 86400)
    routed_today = 0
    saved_today = 0.0
    saved_total = 0.0
    tokens_today = 0
    for rec in host_recs:
        try:
            saved = float(rec.get("estimated_saved", 0.0) or 0.0)
        except (ValueError, TypeError):
            saved = 0.0
        saved_total += saved
        ts = _parse_ts(rec)
        if ts is not None and ts >= day_start:
            routed_today += 1
            saved_today += saved
            tokens_today += _rec_tokens(rec)

    active = last_age is not None and last_age <= ACTIVE_WINDOW_S
    health, reason = _compute_health(now, records, last_age)

    return SurfaceStatus(
        host=host,
        active=active,
        health=health,
        health_reason=reason,
        last_model=last_model,
        last_task=last_task,
        last_age_s=last_age,
        last_tokens=last_tokens,
        routed_count_session=routed_today,
        saved_session=round(saved_today, 4),
        saved_total=round(saved_total, 4),
        tokens_session=tokens_today,
    )


def _compute_health(
    now: float, records: list[dict], last_age: Optional[float]
) -> tuple[str, str]:
    """Health axis — capability, not just recency.

    down     : no model provider is reachable (no API key, no recent local model)
    degraded : an explicit snapshot says so, or usage data is stale
    ok       : a provider is available and nothing flags a problem
    """
    snapshot = _read_health_snapshot(now)
    if snapshot is not None:
        return snapshot

    if not _providers_available(now, records):
        return HEALTH_DOWN, "no model provider reachable (no API key / local model)"

    if _is_usage_stale(now):
        return HEALTH_DEGRADED, f"usage data stale (>30 min) — run {route_tool('llm_check_usage')}"

    return HEALTH_OK, "routing normally"


# ── Renderers ────────────────────────────────────────────────────────────────
# Minimal 16-color ANSI (portable; truecolor is reserved for the CC statusline).
_C = {
    "dim": "\033[2m",
    "reset": "\033[0m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "cyan": "\033[36m",
}
_HEALTH_COLOR = {HEALTH_OK: "green", HEALTH_DEGRADED: "yellow", HEALTH_DOWN: "red"}


def fmt_tokens(n: Optional[int]) -> str:
    """Compact token count: 0→"", 940→"940 tok", 1_250→"1.2k tok"."""
    if not n or n <= 0:
        return ""
    if n >= 1000:
        return f"{n / 1000:.1f}k tok"
    return f"{n} tok"


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("LLM_ROUTER_FORCE_COLOR"):
        return True
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def compact_line(status: SurfaceStatus, color: Optional[bool] = None) -> str:
    """A one-line inline indicator suitable for hosts that print hook stdout.

    Example: ``⚡ llm_router · 🎯 hermes3:8b code/moderate · 💰 $0.03 saved · ✓``
    When the host is down or has never routed, the line says so plainly rather
    than implying a route happened.
    """
    if color is None:
        color = _supports_color()

    def c(text: str, name: str) -> str:
        return f"{_C[name]}{text}{_C['reset']}" if color else text

    parts = [c("⚡ llm_router", "cyan")]

    if status.last_model:
        route = f"🎯 {status.short_model()}"
        if status.last_task:
            route += f" {status.last_task}"
        _tok = fmt_tokens(status.last_tokens)
        if _tok:
            route += f" · {_tok}"
        parts.append(route)
    else:
        parts.append(c("no route yet", "dim"))

    if status.saved_session > 0:
        parts.append(f"💰 ${status.saved_session:.2f} saved")

    glyph = c(status.health_glyph(), _HEALTH_COLOR.get(status.health, "dim"))
    if status.health != HEALTH_OK:
        glyph += " " + c(status.health_reason, _HEALTH_COLOR.get(status.health, "dim"))
    parts.append(glyph)

    return " · ".join(parts)


def terminal_title(status: SurfaceStatus) -> str:
    """An OSC-2 escape string that sets the terminal window/tab title.

    Caller writes the return value to the controlling tty (``/dev/tty``) or
    stderr. Title stays short: ``⚡ llm_router: hermes3:8b · $0.03 ✓``.
    """
    model = status.short_model() if status.last_model else "idle"
    saved = f" · ${status.saved_session:.2f}" if status.saved_session > 0 else ""
    _tok = fmt_tokens(status.last_tokens)
    tok = f" · {_tok}" if _tok else ""
    title = f"⚡ llm_router: {model}{tok}{saved} {status.health_glyph()}"
    # OSC 2 ; <title> BEL
    return f"\033]2;{title}\007"


def notification(
    status: SurfaceStatus,
    event: str,
    now: Optional[float] = None,
    *,
    min_interval_s: int = 1800,
) -> Optional[dict]:
    """Build an OS-notification payload, or None when it should be suppressed.

    Rate-limited per (host, event-class) via ``~/.llm-router/notify_<host>.json`` so
    routine "routed" pings fire at most once per ``min_interval_s``. Health
    transitions to ``down``/``degraded`` are de-duplicated (only fire when the
    state changes) but never throttled, so failures surface immediately.

    ``event`` is one of: ``"route"`` (a route happened) or ``"health"`` (the
    health state may have changed). Returns ``{"title", "message", "urgency"}``.
    """
    if now is None:
        now = time.time()

    state_path = _state_dir() / f"notify_{status.host}.json"
    try:
        last = json.loads(state_path.read_text())
    except (OSError, ValueError):
        last = {}

    payload: Optional[dict] = None
    if event == "health" and status.health != HEALTH_OK:
        # Fire only on a state change; not throttled.
        if last.get("health") != status.health:
            payload = {
                "title": f"⚠ LLM Router {status.health} on {status.host}",
                "message": status.health_reason,
                "urgency": "critical" if status.health == HEALTH_DOWN else "normal",
            }
    elif event == "route" and status.last_model:
        last_route_at = float(last.get("route_at", 0) or 0)
        if (now - last_route_at) >= min_interval_s:
            saved = (
                f" · saved ${status.saved_session:.2f} today"
                if status.saved_session > 0
                else ""
            )
            payload = {
                "title": f"⚡ LLM Router routing on {status.host}",
                "message": f"→ {status.short_model()} ({status.last_task or 'route'}){saved}",
                "urgency": "low",
            }

    # Persist dedup/rate-limit state regardless of whether we emit, so the next
    # call sees the latest health and route time.
    new_state = dict(last)
    new_state["health"] = status.health
    if event == "route" and payload is not None:
        new_state["route_at"] = now
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(new_state))
    except OSError:
        pass

    return payload


# ── Host-side emitter (used by per-surface post-tool hooks) ──────────────────
def _send_os_notification(payload: dict) -> None:
    """Best-effort native notification. Never raises, never blocks for long."""
    import platform
    import shutil
    import subprocess

    title = str(payload.get("title", "LLM Router"))
    message = str(payload.get("message", ""))
    try:
        system = platform.system()
        if system == "Darwin":
            # AppleScript string literals are double-quoted; escape accordingly.
            def _esc(s: str) -> str:
                return s.replace("\\", "\\\\").replace('"', '\\"')

            script = f'display notification "{_esc(message)}" with title "{_esc(title)}"'
            subprocess.run(
                ["osascript", "-e", script], timeout=3, capture_output=True
            )
        elif system == "Linux" and shutil.which("notify-send"):
            urgency = {"critical": "critical", "normal": "normal"}.get(
                str(payload.get("urgency", "")), "low"
            )
            subprocess.run(
                ["notify-send", "-u", urgency, title, message],
                timeout=3,
                capture_output=True,
            )
    except Exception:
        pass


def _open_tty():
    """Open the controlling terminal for writing, or None. Writing here makes the
    indicator visible no matter how the host pipes the hook's stdout/stderr."""
    try:
        return open("/dev/tty", "w")
    except OSError:
        return None


def _line_throttled(host: str, status: SurfaceStatus, now: float, interval_s: int) -> bool:
    """True if the inline line should be SUPPRESSED — identical status reprinted
    within ``interval_s``. The terminal title is never throttled (it overwrites)."""
    sig = f"{status.last_model}|{status.health}|{status.routed_count_session}"
    path = _state_dir() / f"line_{host}.json"
    try:
        prev = json.loads(path.read_text())
    except (OSError, ValueError):
        prev = {}
    if prev.get("sig") == sig and (now - float(prev.get("at", 0) or 0)) < interval_s:
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sig": sig, "at": now}))
    except OSError:
        pass
    return False


def emit_indicator(
    host: str,
    *,
    line: bool = True,
    title: bool = True,
    notify: bool = True,
    now: Optional[float] = None,
    stream=None,
    line_min_interval_s: int = 30,
) -> SurfaceStatus:
    """Render the indicator for ``host`` across the chosen channels.

    Designed to be called at the end of a per-surface post-tool hook. Writes to
    the controlling tty when available (so it survives stdout/stderr capture by
    the host), else falls back to stderr — never to stdout, which hosts parse.
    The terminal title refreshes every call; the inline line is throttled to
    avoid spam when status is unchanged. Fail-soft throughout.
    """
    _now = now if now is not None else time.time()
    status = compute_status(host, now=_now)

    if os.environ.get("LLM_ROUTER_INDICATOR", "on").strip().lower() in ("0", "off", "false", "no"):
        return status

    out = stream if stream is not None else _open_tty()
    target = out if out is not None else sys.stderr
    use_color = True if out is not None else _supports_color()
    # Only throttle real terminal writes; injected streams (tests) always render.
    print_line = line and not (
        stream is None and _line_throttled(host, status, _now, line_min_interval_s)
    )
    try:
        if title:
            target.write(terminal_title(status))
        if print_line:
            target.write("\n" + compact_line(status, color=use_color) + "\n")
        target.flush()
    except Exception:
        pass
    finally:
        if out is not None and out is not sys.stderr and stream is None:
            try:
                out.close()
            except Exception:
                pass

    if notify:
        # Health first (failures must surface), then a throttled route ping.
        for event in ("health", "route"):
            payload = notification(status, event, now=_now)
            if payload is not None:
                _send_os_notification(payload)

    return status


# ── CLI (used by the Claude Code statusline and by scripts) ──────────────────
def _main(argv: list[str]) -> int:
    """`python -m llm_router.observability.surface_status <host> [--glyph|--line]`.

    Default prints the compact line. ``--glyph`` prints just the health glyph
    (for embedding in a statusline segment). Always exits 0 — a status query
    must never break the caller's prompt/statusline.
    """
    host = "claude_code"
    mode = "line"
    for a in argv:
        if a in ("--glyph", "--line"):
            mode = a.lstrip("-")
        elif not a.startswith("-"):
            host = a
    try:
        status = compute_status(host)
        if mode == "glyph":
            sys.stdout.write(status.health_glyph())
        else:
            sys.stdout.write(compact_line(status))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
