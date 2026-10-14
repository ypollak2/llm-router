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

Nothing here stores a token, an email, or an account id. ``seats.json``
holds kinds and plan names only.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

SEATS_FILE_NAME = "seats.json"
STALE_AFTER_SECONDS = 24 * 3600

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


# ── probes ──────────────────────────────────────────────────────────────────

def _default_runner(argv: list[str], timeout: float) -> tuple[int, str] | None:
    """Run a CLI and return (returncode, stdout+stderr), or None if it is
    missing or hangs. Never raises."""
    if shutil.which(argv[0]) is None:
        return None
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


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
    res = runner(["claude", "auth", "status"], timeout)
    not_logged = None
    if res is not None:
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
    res = runner(["codex", "login", "status"], timeout)
    not_logged = None
    if res is not None:
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
    res = runner(["gh", "copilot", "--version"], timeout) if which("gh") else None
    cli_ok = res is not None and res[0] == 0
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
                   which: Callable[[str], str | None] = shutil.which) -> Seat:
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
    which: Callable[[str], str | None] = shutil.which,
    opener=urllib.request.urlopen,
    now: float | None = None,
) -> Seats:
    env = dict(os.environ) if env is None else env
    home = home or Path.home()
    now = time.time() if now is None else now
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


def refresh_seats(home: Path | None = None, **kw) -> Seats:
    """Detect and persist. The one call install, doctor and the hook share."""
    seats = detect_seats(home=home, **kw)
    save_seats(seats, home)
    return seats
