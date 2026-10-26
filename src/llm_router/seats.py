"""Which subscriptions ("seats") this machine is logged in to.

A seat is a subscription the user already pays for, so routed work that
lands on it costs nothing extra. Knowing the seats lets the installer derive
the free bucket instead of asking for ``LLM_ROUTER_SUBSCRIPTION_PROVIDER``
by hand, and lets doctor say which tier no seat covers.

Sources, each optional and each with a timeout:

- Claude:  ``claude auth status`` (JSON: ``authMethod``, ``subscriptionType``).
           Official CLI output -- no keychain scraping.
- Codex:   ``codex login status`` decides the kind. The plan name is a claim
           in the ``id_token`` JWT in ``~/.codex/auth.json``; it is decoded
           locally (base64 only, unverified, no network) and treated as a
           HINT. The claim has been observed stale: a token can say the plan
           ended weeks ago while Codex still works. Login status is the fact.
- Gemini:  ``gemini`` binary plus ``~/.gemini/oauth_creds.json``.
- Ollama:  GET ``/api/tags`` on the configured URL.
- API keys: presence of the well-known env vars, never their values.

CLI lookup (MAP-FIX): processes run with different PATHs (a launchd service
gets ``/usr/bin:/bin:/usr/sbin:/sbin``; an MCP server or a hook gets the user's
shell PATH), and all of them write one shared map. So a CLI is looked up on the
process PATH, then on the directories where any process of this user last found
claude, codex, gemini, gh, node or ollama (``cli_path.json`` in the state dir,
the primary source), then on the generic install dirs in ``FALLBACK_DIRS``. The
probe subprocess gets that merged PATH, so a ``#!/usr/bin/env node`` script
finds ``node``. A learned dir is refreshed only when a binary is found in it and
expires after ``LEARNED_MAX_AGE_S``, so an uninstall still shows.

Nothing here stores a token, an email, or an account id. ``seats.json``
holds kinds and plan names only.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

SEATS_FILE_NAME = "seats.json"
STALE_AFTER_SECONDS = 24 * 3600

CLI_PATH_FILE = "cli_path.json"
LEARN_NAMES = ("claude", "codex", "gemini", "gh", "node", "ollama")
# Interpreters a `#!/usr/bin/env <name>` CLI needs (codex is a node script):
# looked up on every detection so a full-PATH process teaches their dirs.
INTERPRETERS = ("node",)
LEARNED_MAX_AGE_S = 7 * 24 * 3600
LEARN_REFRESH_S = 3600
LEARN_LOCK_TIMEOUT_S = 0.5
# Generic install locations (user-local bin, Homebrew on Apple silicon, the
# classic /usr/local). Searched after the learned dirs; never user-specific.
FALLBACK_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")
# A "not connected" seat whose reason ends with this, or mentions a failed
# probe, is "no evidence" rather than "logged out" (see no_evidence).
NOT_FOUND_SUFFIX = "CLI not found"
CARRY_MAX_AGE_S = 3600

_API_KEY_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "PERPLEXITY_API_KEY",
    "GROQ_API_KEY",
    "DEEPSEEK_API_KEY",
    "MOONSHOT_API_KEY",
)

Runner = Callable[[list[str], float], "tuple[int, str] | None"]


@dataclass(frozen=True)
class Seat:
    kind: str | None = None      # "claude.ai" | "chatgpt" | "google" | "local" | "api-key" | None
    plan: str | None = None      # "max" | "pro" | "plus" | "team" | ... | None
    plan_stale: bool = False     # the plan claim's own window has passed
    models: tuple[str, ...] = ()  # ollama only
    # P1.8: "connected" | "installed_not_connected" | "absent", with the reason.
    # compare=False: two detections of the same login are the same seat whatever
    # the reason text says. Left unset, status is derived from kind.
    status: str | None = field(default=None, compare=False)
    reason: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if self.status is None:
            object.__setattr__(
                self, "status", "connected" if self.present else "absent"
            )

    @property
    def present(self) -> bool:
        return self.kind is not None and self.kind != "api-key"

    def label(self) -> str:
        if self.kind is None:
            return "none"
        if self.kind == "api-key":
            return "api-key"
        if self.kind == "local":
            return f"local({len(self.models)} models)" if self.models else "local"
        plan = self.plan or "?"
        if self.plan_stale:
            plan += ",stale"
        return f"{self.kind}({plan})"


@dataclass(frozen=True)
class Seats:
    claude: Seat = field(default_factory=Seat)
    codex: Seat = field(default_factory=Seat)
    gemini: Seat = field(default_factory=Seat)
    ollama: Seat = field(default_factory=Seat)
    copilot: Seat = field(default_factory=Seat)
    api_keys: dict[str, bool] = field(default_factory=dict)
    detected_at: str = ""

    def free_bucket(self) -> frozenset[str]:
        """Providers that cost nothing extra: local + every logged-in seat.

        A stale plan claim still counts. ``codex exec`` either works or
        fails fast, and a failing provider is already skipped by the chain.
        """
        out = set()
        if self.ollama.present:
            out.add("ollama")
        if self.codex.present:
            out.add("codex")
        if self.gemini.present:
            out.add("gemini_cli")
        if self.claude.present:
            out.add("claude")
        return frozenset(out)

    def subscription_provider(self) -> str | None:
        """Default for ``LLM_ROUTER_SUBSCRIPTION_PROVIDER`` when unset, in
        that variable's vocabulary (``anthropic`` / ``openai`` / ``gemini``).

        The strongest seat heads complex chains: a Claude seat over a
        ChatGPT seat over a Google one. The seats that are not the
        subscription join the free bucket (:meth:`seat_free_providers`), so
        a machine with Claude Max and ChatGPT routes cheap work to Codex and
        hard work to Claude, from either host.
        """
        if self.claude.present:
            return "anthropic"
        if self.codex.present:
            return "openai"
        if self.gemini.present:
            return "gemini"
        return None

    def seat_free_providers(self) -> frozenset[str]:
        """Chain provider names that are free because a seat covers them and
        it is not the subscription seat. Ollama is already in LOCAL_PROVIDERS."""
        sub = self.subscription_provider()
        out = set()
        if self.codex.present and sub != "openai":
            out.add("codex")
        if self.gemini.present and sub != "gemini":
            out.add("gemini_cli")
        return frozenset(out)

    def summary_line(self) -> str:
        return (
            f"claude={self.claude.label()} · codex={self.codex.label()} · "
            f"gemini={self.gemini.label()} · ollama={self.ollama.label()}"
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Seats":
        def seat(x: dict | None) -> Seat:
            x = x or {}
            # seats.json from before P1.8 has no status/reason/copilot: they
            # are derived from kind, so an old file loads without a rewrite.
            return Seat(
                kind=x.get("kind"),
                plan=x.get("plan"),
                plan_stale=bool(x.get("plan_stale", False)),
                models=tuple(x.get("models") or ()),
                status=x.get("status"),
                reason=str(x.get("reason") or ""),
            )
        return cls(
            claude=seat(d.get("claude")),
            codex=seat(d.get("codex")),
            gemini=seat(d.get("gemini")),
            ollama=seat(d.get("ollama")),
            copilot=seat(d.get("copilot")),
            api_keys=dict(d.get("api_keys") or {}),
            detected_at=str(d.get("detected_at") or ""),
        )

    def age_seconds(self, now: float | None = None) -> float | None:
        if not self.detected_at:
            return None
        try:
            then = datetime.fromisoformat(self.detected_at).timestamp()
        except ValueError:
            return None
        return (now if now is not None else time.time()) - then

    def is_stale(self, now: float | None = None) -> bool:
        age = self.age_seconds(now)
        return age is None or age > STALE_AFTER_SECONDS


# ── CLI lookup ──────────────────────────────────────────────────────────────

def _cli_path_file() -> Path:
    from llm_router import paths as _paths
    return _paths.state_path(CLI_PATH_FILE)


def _read_learned() -> dict:
    try:
        data = json.loads(_cli_path_file().read_text())
    except (OSError, ValueError):
        return {}
    dirs = data.get("dirs") if isinstance(data, dict) else None
    return dirs if isinstance(dirs, dict) else {}


def learned_dirs(now: float | None = None) -> list[str]:
    """Dirs where a CLI was found within ``LEARNED_MAX_AGE_S``, newest first."""
    now = time.time() if now is None else now
    live = []
    for d, entry in _read_learned().items():
        seen = entry.get("seen_at") if isinstance(entry, dict) else None
        if isinstance(seen, (int, float)) and 0 <= now - seen <= LEARNED_MAX_AGE_S:
            live.append((seen, d))
    return [d for _, d in sorted(live, reverse=True)]


def search_path(env: dict | None = None, now: float | None = None) -> str:
    """Process PATH, then learned dirs, then existing ``FALLBACK_DIRS``; no repeats."""
    env = os.environ if env is None else env
    out: list[str] = []
    for d in [*(env.get("PATH") or "").split(os.pathsep), *learned_dirs(now),
              *(os.path.expanduser(f) for f in FALLBACK_DIRS if os.path.isdir(os.path.expanduser(f)))]:
        if d and d not in out:
            out.append(d)
    return os.pathsep.join(out)


def _learn(name: str, found: str, now: float) -> None:
    """Record the dir *found* is in. Writes only on a new entry or a stale one.

    A relative hit (a relative PATH entry, resolved against this process's cwd)
    is not recorded: other processes have other cwds. The read-modify-write runs
    under an exclusive lock so two processes cannot lose each other's entry; a busy
    lock skips the write, and a filesystem without locking writes unlocked (counted
    as fail-open ``CHZ-FO-CLI-PATH-LOCK``)."""
    if not os.path.isabs(found):
        return
    d = os.path.normpath(os.path.dirname(found))

    def fresh(dirs: dict) -> bool:
        entry = dirs.get(d) if isinstance(dirs.get(d), dict) else {}
        seen = entry.get("seen_at")
        return (name in (entry.get("names") or []) and isinstance(seen, (int, float))
                and 0 <= now - seen < LEARN_REFRESH_S)

    if fresh(_read_learned()):          # common case: no lock, no write
        return
    path = _cli_path_file()
    try:
        from llm_router import failopen
        from llm_router.file_lock import exclusive_lock
        status: dict = {}
        with exclusive_lock(path.with_name(path.name + ".lock"), timeout=LEARN_LOCK_TIMEOUT_S,
                            status=status) as locked:
            if not locked and status.get("state") == "held":
                return                  # busy: best-effort, the next sighting re-learns
            if not locked:              # no locking here (e.g. ENOLCK on NFS): write unlocked
                failopen.record("CHZ-FO-CLI-PATH-LOCK", detail=str(status.get("state")))
            dirs = _read_learned()
            if fresh(dirs):
                return
            entry = dirs.get(d) if isinstance(dirs.get(d), dict) else {}
            dirs[d] = {"names": sorted(set(entry.get("names") or []) | {name}), "seen_at": now}
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump({"schema": 1, "dirs": dirs}, f, indent=2, sort_keys=True)
                os.replace(tmp, path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
    except OSError as exc:
        failopen.record("CHZ-FO-CLI-PATH-WRITE", exc)


def find_cli(name: str, *, env: dict | None = None, now: float | None = None) -> str | None:
    """``shutil.which`` on :func:`search_path`; a hit teaches its dir to every process."""
    now = time.time() if now is None else now
    found = shutil.which(name, path=search_path(env, now))
    if found is None:
        log.info("seats: %s not found on the process PATH or learned dirs", name)
    elif name in LEARN_NAMES:
        _learn(name, found, now)
    return found


# ── probes ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProbeFailure:
    """The CLI exists but its probe gave no answer (timeout, launch error)."""
    reason: str


def _default_runner(argv: list[str], timeout: float) -> tuple[int, str] | None:
    """Run a CLI found by :func:`find_cli` with the merged PATH in its env, so a
    ``#!/usr/bin/env node`` script resolves ``node``. Returns (returncode,
    stdout+stderr), or None when the CLI is missing. A timeout or launch error
    is raised; :func:`_probe` turns it into a :class:`ProbeFailure`."""
    exe = find_cli(argv[0])
    if exe is None:
        return None
    proc = subprocess.run(
        [exe, *argv[1:]], capture_output=True, text=True, timeout=timeout, check=False,
        env={**os.environ, "PATH": search_path()},
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _probe(runner: Runner, argv: list[str], timeout: float):
    """runner(argv) with its failure kept apart from "not found"."""
    try:
        return runner(argv, timeout)
    except subprocess.TimeoutExpired:
        why = f"{argv[0]} CLI probe timed out after {timeout:g}s"
    except (OSError, subprocess.SubprocessError) as exc:
        why = f"{argv[0]} CLI probe failed to launch: {type(exc).__name__}"
    log.info("seats: %s", why)
    return ProbeFailure(why)


def no_evidence(status: str | None, reason: str | None) -> bool:
    """True for a not-connected verdict that only says "could not look":
    the CLI was not found on this process's PATH, or its probe failed."""
    reason = reason or ""
    return status != "connected" and (reason.endswith(NOT_FOUND_SUFFIX) or "CLI probe " in reason)


def carry_recent(new: "Seats", prior: "Seats | None", now: float,
                 max_age_s: float = CARRY_MAX_AGE_S) -> "Seats":
    """Keep a prior connected claude/codex/gemini seat, at most *max_age_s* old,
    over a :func:`no_evidence` verdict from a process that could not see the CLI."""
    age = prior.age_seconds(now) if prior is not None else None
    if age is None or not 0 <= age <= max_age_s:
        return new
    keep = {}
    for name in ("claude", "codex", "gemini"):
        old, cur = getattr(prior, name), getattr(new, name)
        if old.status == "connected" and no_evidence(cur.status, cur.reason):
            log.info("seats: keeping %s connected (seen %.0fs ago) over %r", name, age, cur.reason)
            keep[name] = old
    return replace(new, **keep) if keep else new


def carry_for_save(new: "Seats", prior: "Seats | None", now: float) -> "Seats":
    """:func:`carry_recent` for a writer of seats.json. A result that carried a seat
    keeps *prior*'s ``detected_at`` (the sighting), so repeated blind saves cannot
    move it and the carry ends ``CARRY_MAX_AGE_S`` after that sighting."""
    out = carry_recent(new, prior, now)
    return new if out is new else replace(out, detected_at=prior.detected_at)


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:300]
    return ""


def _key_only_seat(why: str, not_logged: "Seat | None") -> Seat:
    """An API-key fallback. When the CLI is installed but failing, the status says
    so and the reason is the CLI's own first line; ``kind`` (so routing) is unchanged."""
    if not_logged is not None:
        return Seat(kind="api-key", status="installed_not_connected",
                    reason=not_logged.reason)
    return Seat(kind="api-key", reason=why)


def _detect_claude(runner: Runner, env: dict, timeout: float) -> Seat:
    res = _probe(runner, ["claude", "auth", "status"], timeout)
    not_logged = None
    if isinstance(res, ProbeFailure):
        not_logged = Seat(status="installed_not_connected", reason=res.reason)
    elif res is not None:
        not_logged = Seat(status="installed_not_connected",
                          reason=_first_line(res[1]) or "claude auth status reports no login")
        code, out = res
        try:
            data = json.loads(out[out.index("{"):])
        except (ValueError, json.JSONDecodeError):
            data = {}
        if data.get("loggedIn"):
            method = str(data.get("authMethod") or "").lower()
            plan = data.get("subscriptionType")
            if method == "claude.ai":
                return Seat(kind="claude.ai", plan=str(plan).lower() if plan else None)
            # console / API-key login through the CLI: not a subscription seat
            return Seat(kind="api-key", reason="API-key login, not a subscription seat")
    if env.get("ANTHROPIC_API_KEY"):
        return _key_only_seat("ANTHROPIC_API_KEY only, no subscription seat", not_logged)
    return not_logged or Seat(reason="claude CLI not found")


def _jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}


def _codex_plan_from_auth(home: Path, now: float) -> tuple[str | None, bool]:
    """(plan, stale) from the id_token claim, or (None, False)."""
    auth = home / ".codex" / "auth.json"
    try:
        data = json.loads(auth.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None, False
    tokens = data.get("tokens") or {}
    claims = _jwt_payload(str(tokens.get("id_token") or ""))
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    plan = auth_claims.get("chatgpt_plan_type")
    until = auth_claims.get("chatgpt_subscription_active_until")
    stale = False
    if until:
        try:
            stale = datetime.fromisoformat(str(until)).timestamp() < now
        except ValueError:
            stale = False
    return (str(plan).lower() if plan else None), stale


def _detect_codex(runner: Runner, env: dict, home: Path, timeout: float, now: float) -> Seat:
    res = _probe(runner, ["codex", "login", "status"], timeout)
    not_logged = None
    if isinstance(res, ProbeFailure):
        not_logged = Seat(status="installed_not_connected", reason=res.reason)
    elif res is not None:
        _, out = res
        low = out.lower()
        # The reason is the first line codex itself printed (for a broken
        # config.toml that is the "Error loading configuration: ..." line).
        not_logged = Seat(status="installed_not_connected",
                          reason=_first_line(out) or "codex login status printed nothing")
        if "logged in using chatgpt" in low:
            plan, stale = _codex_plan_from_auth(home, now)
            return Seat(kind="chatgpt", plan=plan, plan_stale=stale)
        if "logged in" in low and "not logged in" not in low:
            return Seat(kind="api-key", reason="codex logged in with an API key, not a seat")
    if env.get("OPENAI_API_KEY"):
        return _key_only_seat("OPENAI_API_KEY only, no ChatGPT seat", not_logged)
    return not_logged or Seat(reason="codex CLI not found")


# ~/.gemini/settings.json auth-type value -> plan label. The two key paths are
# the ones Gemini CLI has used (flat `selectedAuthType`, later
# `security.auth.selectedType`); neither is verified against a live file on
# this machine, so an unrecognised value stays "unknown" with its reason.
_GEMINI_AUTH_PLAN = {"oauth-personal": "google-account", "login-with-google": "google-account"}


def _gemini_plan(home: Path) -> tuple[str, str]:
    try:
        data = json.loads((home / ".gemini" / "settings.json").read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return "unknown", "no readable ~/.gemini/settings.json"
    sec = ((data.get("security") or {}).get("auth") or {}) if isinstance(data, dict) else {}
    auth = sec.get("selectedType") or (data.get("selectedAuthType") if isinstance(data, dict) else None)
    if not auth:
        return "unknown", "settings.json names no auth type"
    plan = _GEMINI_AUTH_PLAN.get(str(auth))
    if plan:
        return plan, f"auth type {auth}"
    return "unknown", f"unrecognised auth type {auth}"


def _detect_gemini(env: dict, home: Path, which: Callable[[str], str | None]) -> Seat:
    has_bin = bool(which("gemini"))
    if has_bin and (home / ".gemini" / "oauth_creds.json").exists():
        plan, why = _gemini_plan(home)
        return Seat(kind="google", plan=plan, reason=f"logged in; plan {plan}: {why}")
    if env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"):
        return Seat(kind="api-key", reason="API key only, no gemini CLI login")
    if has_bin:
        return Seat(status="installed_not_connected",
                    reason="gemini binary found, no ~/.gemini/oauth_creds.json")
    return Seat(reason="gemini CLI not found")


def _detect_copilot(runner: Runner, home: Path, which: Callable[[str], str | None],
                    timeout: float) -> Seat:
    """GitHub Copilot: `gh copilot --version`, hosts.json, VS Code extension dir.
    Existence checks only; the files' contents (tokens) are never read."""
    res = _probe(runner, ["gh", "copilot", "--version"], timeout) if which("gh") else None
    cli_ok = isinstance(res, tuple) and res[0] == 0
    hosts = (home / ".config" / "github-copilot" / "hosts.json").exists()
    try:
        ext = any((home / ".vscode" / "extensions").glob("github.copilot-*"))
    except OSError:
        ext = False
    if hosts and (cli_ok or ext):
        via = "gh copilot" if cli_ok else "VS Code extension"
        return Seat(kind="github-copilot", reason=f"hosts.json present; {via} installed")
    if cli_ok or ext or hosts:
        found = [n for n, ok in (("gh copilot", cli_ok), ("VS Code extension", ext),
                                 ("hosts.json", hosts)) if ok]
        why = "no signed-in host in ~/.config/github-copilot/hosts.json" if not hosts \
            else "hosts.json present but no gh copilot or VS Code extension"
        return Seat(status="installed_not_connected", reason=f"found {', '.join(found)}; {why}")
    return Seat(reason="no gh copilot, hosts.json or VS Code extension")


def _detect_ollama(env: dict, timeout: float, opener=urllib.request.urlopen,
                   which: Callable[[str], str | None] | None = None) -> Seat:
    which = which or find_cli
    url = env.get("OLLAMA_URL") or "http://localhost:11434"
    try:
        req = urllib.request.Request(f"{url}/api/tags", method="GET")
        with opener(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception:
        if which("ollama"):
            return Seat(status="installed_not_connected",
                        reason=f"ollama binary found, server not reachable at {url}")
        return Seat(reason="ollama not installed")
    names = tuple(m.get("name", "") for m in data.get("models", []) if m.get("name"))
    return Seat(kind="local", models=names,
                reason=f"{len(names)} model(s) listed" if names else "server up, no models installed")


def detect_seats(
    *,
    timeout: float = 3.0,
    runner: Runner = _default_runner,
    env: dict | None = None,
    home: Path | None = None,
    which: Callable[[str], str | None] | None = None,
    opener=urllib.request.urlopen,
    now: float | None = None,
) -> Seats:
    env = dict(os.environ) if env is None else env
    home = home or Path.home()
    now = time.time() if now is None else now
    if which is None:
        which = find_cli
        for name in INTERPRETERS:
            find_cli(name)
    return Seats(
        claude=_detect_claude(runner, env, timeout),
        codex=_detect_codex(runner, env, home, timeout, now),
        gemini=_detect_gemini(env, home, which),
        ollama=_detect_ollama(env, min(timeout, 2.0), opener, which),
        copilot=_detect_copilot(runner, home, which, timeout),
        api_keys={k: bool(env.get(k)) for k in _API_KEY_VARS},
        detected_at=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
    )


# ── persistence ─────────────────────────────────────────────────────────────

def seats_path(home: Path | None = None) -> Path:
    # R11: canonical resolver — see direct_diagnostics for the same defect.
    if home is not None:
        return Path(home) / ".llm-router" / SEATS_FILE_NAME
    from llm_router import paths as _paths
    return _paths.state_path(SEATS_FILE_NAME)


def save_seats(seats: Seats, home: Path | None = None) -> Path:
    path = seats_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(seats.to_dict(), indent=2))
    return path


def load_seats(home: Path | None = None) -> Seats | None:
    path = seats_path(home)
    try:
        return Seats.from_dict(json.loads(path.read_text()))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def refresh_seats(home: Path | None = None, *, carried: dict | None = None, **kw) -> Seats:
    """Detect and persist. The one call install, doctor and the hook share.
    Like the rescan, a seat another writer saw connected within the last hour is
    carried over this process's "could not look" verdict (:func:`carry_for_save`).
    Pass a dict as *carried* to get ``{name: this process's own Seat}`` per carried seat."""
    now = time.time() if kw.get("now") is None else kw["now"]
    raw = detect_seats(home=home, **{**kw, "now": now})
    seats = carry_for_save(raw, load_seats(home), now)
    if carried is not None:
        carried.update({n: getattr(raw, n) for n in ("claude", "codex", "gemini")
                        if getattr(seats, n) != getattr(raw, n)})
    save_seats(seats, home)
    return seats
