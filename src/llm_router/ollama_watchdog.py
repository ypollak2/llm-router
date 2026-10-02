"""Detect an Ollama server that accepts connections but no longer generates.

WHY THIS EXISTS
---------------
2026-10-02: the owner's Ollama server was found HUNG — 0% CPU, 16 days of uptime,
dozens of stale connections — and had to be restarted by hand. Earlier live
zero-Claude edits had timed out against it. Every existing local pre-flight
(``direct_executor.ollama_is_alive``, ``available_ollama_models``,
``warm.ollama_ps``) asks the HTTP surface (``/api/tags``, ``/api/ps``). That
surface is served by the Ollama *server* process; inference runs in a separate
per-model *runner*. A wedged runner leaves the server answering ``/api/tags``
instantly, so the pre-flight passes and the real call then burns the whole hook
deadline (~37 s for a zero-Claude edit) before failing.

So the only honest probe is a 1-token ``/api/generate`` against a model that is
already resident, with a short deadline.

DESIGN
------
* ``probe_generation`` — the probe. Three outcomes, never conflated:
  ``ok=True`` (a token came back, or nothing is resident so there is nothing to
  hang), ``ok=False`` (server reachable but ``/api/ps`` or the generate stalled /
  errored / reported a runner crash) and ``ok=None`` (cannot tell — server down,
  unparseable reply). A cold model is NOT probed: its load time is not a hang.
* A persisted breaker (``ollama_watchdog.json``), because hooks are short-lived
  separate processes and ``proxy.backend_health.BackendHealth`` is an in-memory
  object of the long-lived proxy. It reuses that module's reason string and
  runner-crash regex so there is one vocabulary for "the local backend is
  unhealthy". ``fail_n`` consecutive failures flip ``hung``; one success clears it.
* ``gate`` — what the local paths call (zero-Claude edit, direct executor). It
  returns ``None`` (proceed) or a reason (fail fast to Claude). It is rate
  limited: within ``min_interval`` it answers from the persisted state with no
  network call; past it, it runs one inline probe, which is also how a recovered
  server clears the flag. The probe is claimed in the state file BEFORE it runs,
  so concurrent hooks do not stampede a server that is already struggling.
* ``main`` / ``confirm`` — what session start runs detached in the background
  (``python -m llm_router.ollama_watchdog``): up to ``fail_n`` probes spaced
  apart, and, only if restart is opted in, a restart.
* Restart is OPT-IN (``LLM_ROUTER_OLLAMA_WATCHDOG_RESTART=1``), default OFF.
  Safety: loopback servers only; the launch mode is detected from the process
  table and an unknown mode does nothing; SIGTERM only, never SIGKILL; a cooldown
  stops a model that wedges on load from being restarted in a loop; and there is
  deliberately NO env var that supplies a restart command (a cloned repo's
  ``.env`` could set one — a code-execution sink).

KNOWN LIMITS
------------
* A hand-run ``ollama serve`` takes its tuning (``OLLAMA_NUM_PARALLEL`` etc.)
  from the shell that started it. A restart relaunches through
  ``hooks/start-ollama.sh``, which restores the script's own tuning
  (keep-alive, max-loaded-models, context length) and whatever ``OLLAMA_*`` the
  Claude session itself exports — not the original shell's. Ollama.app discards
  such env anyway (it is capped at one slot), so restarting the app loses nothing.
* A legitimately long generation from another client on a single-slot server
  queues the probe and looks like a stall. ``fail_n`` spaced probes plus a grace
  period before any restart make this unlikely, not impossible — one more reason
  restart is opt-in.
* The probe uses the zero-Claude edit call's ``num_ctx`` and ``keep_alive``
  (``warm.warmup_payload``) so it never changes how the model is held; if the
  resident runner was loaded with a different ``num_ctx`` the probe causes the
  same reload the edit call would.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from llm_router import failopen
from llm_router.paths import state_path
from llm_router.proxy.backend_health import CRASH_SIGNATURE, DEFAULT_FAIL_N, REASON_BACKEND_UNHEALTHY

__all__ = [
    "ProbeResult", "probe_generation", "record_probe", "gate", "confirm", "main",
    "detect_launch_mode", "restart", "hung_hint", "read_state",
]

_STATE_FILE = "ollama_watchdog.json"

DEFAULT_TIMEOUT_S = 8.0
DEFAULT_MIN_INTERVAL_S = 60.0
#: Spacing between the probes of one confirmation round.
CONFIRM_SPACING_S = 4.0
#: After a confirmed hang, wait this long and probe once more before restarting:
#: a long generation from another client should have finished by then.
RESTART_GRACE_S = 60.0
#: At most one restart per window, so a model that wedges on load is not looped.
RESTART_COOLDOWN_S = 1800.0
#: A confirmation round holds this lease so two session starts do not both run one.
CONFIRM_LEASE_S = 300.0
#: Session start also fires the model warm-up; let a load in flight finish before
#: probing so a cold load is not read as a stall.
SETTLE_S = 15.0
_PS_TIMEOUT_S = 2.0

MODE_APP = "app"
MODE_SERVE = "serve"
MODE_UNKNOWN = "unknown"
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


# ── knobs (literal env reads: tests/test_env_registry.py scans for them) ──────

def enabled() -> bool:
    return os.environ.get("LLM_ROUTER_OLLAMA_WATCHDOG", "on").strip().lower() not in (
        "0", "off", "false", "no")


def restart_enabled() -> bool:
    return os.environ.get("LLM_ROUTER_OLLAMA_WATCHDOG_RESTART", "").strip().lower() in (
        "1", "on", "true", "yes")


def _float_env(raw: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    return value if value > minimum else default


def probe_timeout_s() -> float:
    return _float_env(os.environ.get("LLM_ROUTER_OLLAMA_WATCHDOG_TIMEOUT_S", ""), DEFAULT_TIMEOUT_S)


def min_interval_s() -> float:
    return _float_env(os.environ.get("LLM_ROUTER_OLLAMA_WATCHDOG_MIN_INTERVAL_S", ""),
                      DEFAULT_MIN_INTERVAL_S)


def fail_n() -> int:
    raw = os.environ.get("LLM_ROUTER_OLLAMA_WATCHDOG_FAIL_N", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_FAIL_N
    return value if value >= 1 else DEFAULT_FAIL_N


# ── the probe ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProbeResult:
    #: True healthy (or nothing resident), False hung/erroring, None cannot tell.
    ok: bool | None
    detail: str
    model: str | None = None
    elapsed_s: float = 0.0


def _base_url() -> str:
    from llm_router.warm import _base_url as base
    return base()


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(
        getattr(exc, "reason", None), (TimeoutError, socket.timeout))


def _post_json(url: str, body: dict, timeout: float) -> dict | None:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    # URL comes from warm._base_url(), which validates scheme and host.
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
        data = json.loads(resp.read())
    return data if isinstance(data, dict) else None


def _resident_models(base: str, timeout: float) -> list[str] | ProbeResult:
    """Resident model names, or a ProbeResult when the answer is not a list."""
    try:
        req = urllib.request.Request(f"{base}/api/ps")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            data = json.loads(resp.read())
    except Exception as exc:                                     # noqa: BLE001
        if _is_timeout(exc):
            return ProbeResult(False, f"/api/ps did not answer within {timeout:g}s")
        return ProbeResult(None, f"/api/ps unreachable ({type(exc).__name__})")
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return ProbeResult(None, "/api/ps returned an unexpected shape")
    return [str(m.get("name") or m.get("model") or "") for m in models if isinstance(m, dict)]


def _pick_model(resident: list[str]) -> str:
    """The edit model when resident (it is the one the edit path needs), else the first."""
    from llm_router import warm
    names = [r for r in resident if r]
    try:
        from llm_router.zero_claude_edit import edit_model
        wanted = edit_model()
        for r in names:
            if warm.model_matches(wanted, r):
                return r
    except Exception as exc:                                     # noqa: BLE001
        failopen.record("OLLAMA-WATCHDOG-PICK-MODEL", exc)
    return names[0]


def probe_generation(timeout: float | None = None) -> ProbeResult:
    """One 1-token generation against a resident model. Never raises."""
    t = probe_timeout_s() if timeout is None else timeout
    started = time.monotonic()
    try:
        base = _base_url()
        resident = _resident_models(base, min(_PS_TIMEOUT_S, t))
        if isinstance(resident, ProbeResult):
            return ProbeResult(resident.ok, resident.detail, None, time.monotonic() - started)
        if not any(resident):
            return ProbeResult(True, "no model resident; nothing to hang", None,
                               time.monotonic() - started)
        model = _pick_model(resident)
        from llm_router import warm
        try:
            data = _post_json(f"{base}/api/generate", warm.warmup_payload(model), t)
        except Exception as exc:                                 # noqa: BLE001
            elapsed = time.monotonic() - started
            if _is_timeout(exc):
                return ProbeResult(False, f"1-token generate on {model} stalled past {t:g}s",
                                   model, elapsed)
            body = ""
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    body = exc.read().decode("utf-8", "replace")[:300]
                except Exception:                                # noqa: BLE001
                    body = ""
            if body and CRASH_SIGNATURE.search(body):
                return ProbeResult(False, f"runner crashed: {body[:120]}", model, elapsed)
            if isinstance(exc, urllib.error.HTTPError):
                return ProbeResult(False, f"generate returned HTTP {exc.code}", model, elapsed)
            return ProbeResult(None, f"generate unreachable ({type(exc).__name__})", model, elapsed)
        elapsed = time.monotonic() - started
        if data is None:
            return ProbeResult(None, "generate returned an unexpected shape", model, elapsed)
        err = str(data.get("error") or "")
        if err:
            kind = "runner crashed" if CRASH_SIGNATURE.search(err) else "generate error"
            return ProbeResult(False, f"{kind}: {err[:120]}", model, elapsed)
        if data.get("done") is True or data.get("response"):
            return ProbeResult(True, f"1-token generate on {model} ok in {elapsed:.1f}s",
                               model, elapsed)
        return ProbeResult(None, "generate reply carried neither done nor a token", model, elapsed)
    except Exception as exc:                                     # noqa: BLE001
        failopen.record("OLLAMA-WATCHDOG-PROBE", exc)
        return ProbeResult(None, f"probe error ({type(exc).__name__})",
                           None, time.monotonic() - started)


# ── persisted breaker ─────────────────────────────────────────────────────────

def _state_file() -> Path:
    return state_path(_STATE_FILE)


def read_state() -> dict:
    """The persisted breaker state; ``{}`` when absent or unreadable (fail open)."""
    path = _state_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:                                     # noqa: BLE001
        failopen.record("OLLAMA-WATCHDOG-STATE-READ", exc)
        return {}


def _write_state(state: dict) -> None:
    path = _state_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:                                     # noqa: BLE001
        failopen.record("OLLAMA-WATCHDOG-STATE-WRITE", exc)


def _within(state: dict, key: str, now: float, window: float) -> bool:
    """True when ``state[key]`` is a timestamp less than ``window`` seconds before ``now``.

    A missing or non-numeric timestamp means "never happened", which is NOT an age
    of zero: it must read as outside the window, never inside it.
    """
    raw = state.get(key)
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return False
    return now - float(raw) < window


def _reason(state: dict) -> str:
    detail = str(state.get("detail") or "")
    base = (f"{REASON_BACKEND_UNHEALTHY}: local Ollama accepts connections but does not "
            f"generate (failed-probe streak: {int(state.get('streak') or 0)})")
    return f"{base}; last: {detail}" if detail else base


def record_probe(result: ProbeResult, *, now: float | None = None) -> str | None:
    """Fold one probe into the breaker. Returns a reason string on the transition to hung.

    ``ok=None`` (cannot tell) changes nothing: unknown is neither a failure nor
    proof of health, so it must not trip the breaker or clear it.
    """
    ts = time.time() if now is None else now
    state = read_state()
    state["last_probe_ts"] = ts
    state["detail"] = result.detail
    if result.ok is None:
        _write_state(state)
        return None
    if result.ok:
        state.update(streak=0, hung=False, last_ok_ts=ts)
        state.pop("hung_since", None)
        _write_state(state)
        return None
    state["streak"] = int(state.get("streak") or 0) + 1
    newly_hung = False
    if state["streak"] >= fail_n() and not state.get("hung"):
        state.update(hung=True, hung_since=ts)
        newly_hung = True
    _write_state(state)
    if not newly_hung:
        return None
    reason = _reason(state)
    failopen.record("OLLAMA-WATCHDOG-HUNG", detail=result.detail)
    print(f"llm-router: {reason}", file=sys.stderr, flush=True)
    return reason


def hung_hint() -> str | None:
    """One session-start line when the persisted state says hung, else None."""
    state = read_state()
    if not state.get("hung"):
        return None
    restart_note = ("auto-restart is on" if restart_enabled() else
                    "restart Ollama, or set LLM_ROUTER_OLLAMA_WATCHDOG_RESTART=1 to let "
                    "llm-router do it")
    return (f"⚠️  Local Ollama is HUNG (accepts connections, does not generate); local edits "
            f"fail over to Claude until it recovers — {restart_note}")


def gate(*, now: float | None = None) -> str | None:
    """None to proceed with local work, or a reason to fail fast to Claude.

    Rate limited: within ``min_interval`` this reads the persisted state only.
    Past it, one inline probe runs (claimed in the state file first). A probe
    that fails returns a reason even before ``fail_n`` is reached — that probe
    just proved generation stalls, so this call would stall too. Never raises;
    unknown / disabled / any error proceeds.
    """
    try:
        if not enabled():
            return None
        ts = time.time() if now is None else now
        state = read_state()
        fresh = _within(state, "last_probe_ts", ts, min_interval_s())
        if fresh:
            return _reason(state) if state.get("hung") else None
        _write_state(dict(state, last_probe_ts=ts))          # claim before probing
        result = probe_generation()
        reason = record_probe(result, now=ts)
        if result.ok is False:
            return reason or _reason(read_state() | {"detail": result.detail})
        return None
    except Exception as exc:                                     # noqa: BLE001
        failopen.record("OLLAMA-WATCHDOG-GATE", exc)
        return None


# ── restart (opt-in) ──────────────────────────────────────────────────────────

def _is_loopback(url: str) -> bool:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "").lower() in _LOOPBACK


def _process_table() -> list[tuple[int, str]]:
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True,
                         timeout=5, check=False, env=os.environ.copy()).stdout
    rows: list[tuple[int, str]] = []
    for line in out.splitlines():
        pid_s, _, cmd = line.strip().partition(" ")
        if pid_s.isdigit():
            rows.append((int(pid_s), cmd.strip()))
    return rows


def detect_launch_mode(rows: list[tuple[int, str]]) -> tuple[str, list[int]]:
    """``(mode, serve_pids)`` from a process table of ``(pid, command)``.

    ``serve``  a ``ollama serve`` that is NOT inside Ollama.app (hand-run).
    ``app``    the Ollama.app GUI / its bundled ``ollama serve``.
    ``unknown`` neither is visible, so there is nothing safe to restart.
    A hand-run serve wins over the app: it is the one holding the port.
    """
    own = os.getpid()
    hand: list[int] = []
    bundled: list[int] = []
    gui = False
    for pid, cmd in rows:
        if pid == own or pid <= 1:
            continue
        tokens = cmd.split()
        if "Ollama.app/Contents/MacOS/Ollama" in cmd:
            gui = True
        if len(tokens) >= 2 and Path(tokens[0]).name == "ollama" and tokens[1] == "serve":
            (bundled if "Ollama.app" in tokens[0] else hand).append(pid)
    if hand:
        return MODE_SERVE, hand
    if gui or bundled:
        return MODE_APP, bundled
    return MODE_UNKNOWN, []


def _wait_gone(pids: list[int], seconds: float, sleep: Callable[[float], None]) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except OSError:
                pass
        if not alive:
            return True
        sleep(0.5)
    return False


def _wait_up(seconds: float, sleep: Callable[[float], None]) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{_base_url()}/api/version", timeout=2):  # nosec B310
                return True
        except Exception:                                        # noqa: BLE001
            sleep(1.0)
    return False


def _start_script() -> Path:
    return Path(__file__).resolve().parent / "hooks" / "start-ollama.sh"


def restart(mode: str, pids: list[int], *, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Restart Ollama for a detected ``mode``. SIGTERM only; True when it is back up."""
    if mode == MODE_UNKNOWN:
        return False
    env = os.environ.copy()
    if mode == MODE_APP:
        subprocess.run(["osascript", "-e", 'tell application "Ollama" to quit'],
                       capture_output=True, timeout=15, check=False, env=env)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if not _wait_gone(pids, 15.0, sleep):
        failopen.record("OLLAMA-WATCHDOG-RESTART-STUCK",
                        detail=f"pids {pids} ignored SIGTERM; not escalating")
        return False
    if mode == MODE_APP:
        subprocess.run(["open", "-a", "Ollama"], capture_output=True, timeout=15, check=False,
                       env=env)
    else:
        subprocess.run(["bash", str(_start_script())], capture_output=True, timeout=30,
                       check=False, env=env)
    return _wait_up(30.0, sleep)


def _maybe_restart(sleep: Callable[[float], None], now: float) -> bool:
    """Restart only when opted in, loopback, past the cooldown and the mode is known."""
    if not restart_enabled():
        return False
    state = read_state()
    if _within(state, "last_restart_ts", now, RESTART_COOLDOWN_S):
        return False
    if not _is_loopback(_base_url()):
        failopen.record("OLLAMA-WATCHDOG-RESTART-REMOTE", detail="remote server; not restarting")
        return False
    mode, pids = detect_launch_mode(_process_table())
    if mode == MODE_UNKNOWN:
        failopen.record("OLLAMA-WATCHDOG-RESTART-UNKNOWN-MODE", detail="no ollama process found")
        return False
    state["last_restart_ts"] = now           # claim the cooldown before acting
    _write_state(state)
    ok = restart(mode, pids, sleep=sleep)
    state = read_state()
    if ok:
        state.update(streak=0, hung=False)
        state.pop("hung_since", None)
        _write_state(state)
        failopen.record("OLLAMA-WATCHDOG-RESTARTED", detail=mode)
    else:
        failopen.record("OLLAMA-WATCHDOG-RESTART-FAILED", detail=mode)
    return ok


# ── background confirmation ───────────────────────────────────────────────────

def confirm(*, sleep: Callable[[float], None] = time.sleep, now: float | None = None,
            settle_s: float = 0.0) -> dict:
    """One confirmation round (session start, detached). Returns a small outcome dict.

    Skips when a recent probe was healthy or another round holds the lease.
    Probes up to ``fail_n`` times, spaced; stops early on the first success.
    On a confirmed hang, optionally waits a grace period, re-probes, and restarts.
    """
    out = {"probes": 0, "hung": False, "restarted": False, "skipped": ""}
    if not enabled():
        out["skipped"] = "disabled"
        return out
    ts = time.time() if now is None else now
    state = read_state()
    if _within(state, "confirm_lease_ts", ts, CONFIRM_LEASE_S):
        out["skipped"] = "another confirmation round is running"
        return out
    healthy_recently = (not state.get("hung") and not state.get("streak")
                        and _within(state, "last_probe_ts", ts, min_interval_s()))
    if healthy_recently:
        out["skipped"] = "probed healthy within the rate limit"
        return out
    _write_state(dict(state, confirm_lease_ts=ts))
    try:
        if settle_s > 0:
            sleep(settle_s)
        for i in range(fail_n()):
            result = probe_generation()
            out["probes"] += 1
            record_probe(result, now=time.time() if now is None else now)
            if result.ok is not False:
                break
            if i < fail_n() - 1:
                sleep(CONFIRM_SPACING_S)
        out["hung"] = bool(read_state().get("hung"))
        if out["hung"] and restart_enabled():
            sleep(RESTART_GRACE_S)
            again = probe_generation()
            out["probes"] += 1
            record_probe(again, now=time.time() if now is None else now)
            if again.ok is False:
                out["restarted"] = _maybe_restart(sleep, time.time() if now is None else now)
            out["hung"] = bool(read_state().get("hung"))
    finally:
        done = read_state()
        done.pop("confirm_lease_ts", None)
        _write_state(done)
    return out


def main() -> int:
    """``python -m llm_router.ollama_watchdog`` — one confirmation round."""
    try:
        outcome = confirm(settle_s=SETTLE_S)
    except Exception as exc:                                     # noqa: BLE001
        failopen.record("OLLAMA-WATCHDOG-CONFIRM", exc)
        return 0
    if outcome["hung"]:
        print(f"llm-router: {hung_hint()}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
