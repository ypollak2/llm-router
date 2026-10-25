"""One classifier engine: ``decide(text, door=...) -> Decision`` (PLAN v16 P1.6).

The seven classifier call sites each decide differently today (door agreement
on the frozen corpus: hook vs gateway 68.0%). This module is the single
decision they will all call. The gateway, proxy, MCP, hook, agent-route,
gemini-cli and router doors call it (the wiring PR) in SHADOW by default (D-72,
``LLM_ROUTER_ENGINE_MODE``): their rules route and ``shadow`` logs this engine's
Decision beside the rules class. ``decide`` mode, where the engine decides (the
hook then also leaves its Decision for the proxy by ``text_sha``,
``reuse_fields``), is opt-in and not the v16.0 default: P1.6-g FAILed.

Sync authority, per D-19 A: L1 heuristics (``classify.classify_signals`` under
``ENGINE_POLICY``) + L3 lexical model (``ml_lexical``) + a cache. No LLM and no
network on this path (D-13: the hook never waits). ``apply_l2`` is how a
schema-checked LLM verdict could be merged later; ``decide`` never calls it.

Not done here, on purpose (needs data or another item):
  * ``ENGINE_POLICY`` is GATEWAY_POLICY unrefitted; the refit on GT-500 tune
    needs that set (GE3).
  * ``TAU`` and the calibration tables are placeholders until fitted on tune, so
    abstain is log-only by default (``LLM_ROUTER_ENGINE_ABSTAIN=enforce`` moves a tier).
  * the ``prefix`` layer is P2.7's; the ``complexity_knn`` vote is left out
    because it needs an embedding call (see ``decide`` docstring).
"""

from __future__ import annotations

import contextvars
import dataclasses
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from llm_router import capped_log, decision_ledger, failopen, ml_lexical, prompt_key
from llm_router.classify import GATEWAY_POLICY, _score_categories, classify_signals
from llm_router.paths import state_path

log = logging.getLogger("llm_router.engine")

ENGINE_VERSION = "engine/1"
#: One policy for every door. Starts from GATEWAY_POLICY (the live proxy's) and adds keyword
#: complexity (D-69): the complex-keyword signals of the hook, agent-route and gemini-cli scorers
#: (classify.py `_engine_keyword_complex`) for prompts of >= 40 chars, and a simple band up to 599
#: chars (the router's wrapped-prompt fix of 2026-06-06). Chosen on the GT-500 tune split only
#: (n=108, Opus labels; `scripts/engine_policy_tune.py`); the held-out split is untouched.
ENGINE_POLICY = dataclasses.replace(GATEWAY_POLICY, engine_keywords=True, simple_max=599, keyword_min_len=40)
POLICY_ID = "ep1"
#: Abstain below this combined confidence. PLACEHOLDER: fitted on tune and
#: pre-registered before wiring. 0.5 equals L1's own ``_CONFIDENCE_THRESHOLD``.
TAU = 0.5


def abstain_enforced() -> bool:
    """``LLM_ROUTER_ENGINE_ABSTAIN``: ``log`` (default) or ``enforce``.

    TAU is a placeholder until it is fitted on GT-500 tune, so the wired doors
    must not move a tier on it: under ``log`` the Decision still carries
    ``abstain=True`` (and the reason says so) but tier and complexity stay what
    L1/L3 decided. ``enforce`` is the spec behaviour (one tier safer)."""
    return os.environ.get("LLM_ROUTER_ENGINE_ABSTAIN", "log").strip().lower() == "enforce"

DOORS = ("proxy", "mcp", "hook", "gateway", "agent_route", "gemini_cli", "router", "sdk")
Layer = Literal["prefix", "L1", "cache", "L3", "L2", "fallback"]

#: Mirrors ``proxy/claude_tiers.yaml`` route.default (a test pins the two).
TIERS = ("haiku", "sonnet", "opus")
_COMPLEXITY_TIER = {"simple": "haiku", "moderate": "sonnet", "complex": "opus", "deep_reasoning": "opus"}
_TIER_COMPLEXITY = {"haiku": "simple", "sonnet": "moderate", "opus": "complex"}
_TASK_TYPES = ("query", "research", "generate", "analyze", "code", "image", "video", "audio",
               "introspect", "coordinate", "coordination")


@dataclass(frozen=True)
class Decision:
    """Every R-CLS-2 field. ``ms`` is excluded from equality, so two decisions
    compare equal when they decide the same thing."""

    task_type: str
    dims: dict[str, int] | None
    tier: str
    complexity: str
    needs_tools: bool
    local_eligible: bool
    needs_repo_context: bool
    local_only: bool
    confidence: float | None
    calibrated: bool
    abstain: bool
    layer: str = field(compare=False)  # "cache" on a hit; origin layer is not part of the decision
    reason: str
    policy_version: str
    engine_version: str
    ms: float = field(default=0.0, compare=False)


class SecretLocalOnlyError(RuntimeError):
    """A prompt with a secret reached a door with no healthy local model."""


SECRET_REFUSAL = "prompt contains a secret; no local model available; not sent"


def secret_enforcement(decision: Decision, door: str, *, local_model_healthy: bool) -> str:
    """D-28 A. ``"none"``, ``"local_only"``, ``"refuse"`` or ``"log_only"``.

    The proxy only logs (a 400 would break a live Claude Code turn: fail-open
    NFR) until the FP-rate gate and the owner say otherwise. Other doors route
    local-only, and refuse when no local model is healthy.
    """
    if not decision.local_only:
        return "none"
    if door == "proxy":
        return "log_only"
    return "local_only" if local_model_healthy else "refuse"


def require_local_or_refuse(decision: Decision, door: str, *, local_model_healthy: bool) -> None:
    if secret_enforcement(decision, door, local_model_healthy=local_model_healthy) == "refuse":
        raise SecretLocalOnlyError(SECRET_REFUSAL)


# ── calibration ──────────────────────────────────────────────────────────────

N_BINS = 10


def _bin(p: float) -> int:
    return min(N_BINS - 1, max(0, int(p * N_BINS)))


def reliability_table(confidences, correct) -> list[dict]:
    """10 equal-width bins: ``{lo, hi, n, mean_conf, accuracy}`` (empty bins n=0)."""
    rows = [{"lo": k / N_BINS, "hi": (k + 1) / N_BINS, "n": 0, "c": 0.0, "a": 0} for k in range(N_BINS)]
    for p, ok in zip(confidences, correct):
        r = rows[_bin(p)]
        r["n"] += 1
        r["c"] += p
        r["a"] += 1 if ok else 0
    return [{"lo": r["lo"], "hi": r["hi"], "n": r["n"],
             "mean_conf": r["c"] / r["n"] if r["n"] else None,
             "accuracy": r["a"] / r["n"] if r["n"] else None} for r in rows]


def ece(confidences, correct) -> float:
    """Expected calibration error, 10 equal-width bins (PREREG-GT-amend3)."""
    n = len(list(confidences))
    if n == 0:
        raise ValueError("ece of an empty set")
    t = reliability_table(confidences, correct)
    return sum(r["n"] / n * abs(r["mean_conf"] - r["accuracy"]) for r in t if r["n"])


def fit_calibration(confidences, correct) -> list[float | None]:
    """Per-bin empirical accuracy (None for an empty bin: that bin stays raw)."""
    return [r["accuracy"] for r in reliability_table(confidences, correct)]


def _calibration() -> dict[str, list[float | None]]:
    """``{"L1": [...10], "L3": [...10]}`` from ``engine_calibration.json``, or {}."""
    p = _artifact("engine_calibration.json")
    try:
        raw = json.loads(p.read_text())
        return {k: v for k, v in raw.items() if k in ("L1", "L3") and isinstance(v, list) and len(v) == N_BINS}
    except (OSError, ValueError, AttributeError):
        return {}


def _artifact(name: str) -> Path:
    if "lexical" in name:
        override = os.environ.get("LLM_ROUTER_ENGINE_LEXICAL", "").strip()
    else:
        override = os.environ.get("LLM_ROUTER_ENGINE_CALIBRATION", "").strip()
    return Path(override) if override else state_path(name)


def _apply_calibration(layer: str, raw: float, table: dict) -> tuple[float, bool]:
    t = table.get(layer)
    if t is None:
        return raw, False
    v = t[_bin(raw)]
    return (raw, False) if v is None else (v, True)


# ── cache ────────────────────────────────────────────────────────────────────

_LRU_MAX = 4096
_TTL_S = 30 * 86400.0  # persistent rows older than this are ignored and pruned
_MAX_ROWS = 50_000
_lru: "OrderedDict[str, Decision]" = OrderedDict()
_lru_lock = threading.Lock()
_db: dict[str, sqlite3.Connection | None] = {}
_db_lock = threading.Lock()


def _cache_mode() -> str:
    """``LLM_ROUTER_DECIDE_CACHE``: ``sqlite`` (default), ``memory`` or ``off``."""
    m = os.environ.get("LLM_ROUTER_DECIDE_CACHE", "sqlite").strip().lower()
    return m if m in ("sqlite", "memory", "off") else "sqlite"


def _conn() -> sqlite3.Connection | None:
    path = str(state_path("decide_cache.sqlite"))
    with _db_lock:
        if path not in _db:
            try:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))  # 0600 before sqlite makes it
                c = sqlite3.connect(path, timeout=0.2, check_same_thread=False)
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA synchronous=NORMAL")
                c.execute("CREATE TABLE IF NOT EXISTS decisions (k TEXT PRIMARY KEY, v TEXT NOT NULL, ts REAL NOT NULL)")
                _prune(c)
                c.commit()
                _db[path] = c
            except (sqlite3.Error, OSError) as exc:
                log.warning("engine: decide cache disabled: %s", exc)
                _db[path] = None
        return _db[path]


def _prune(c: sqlite3.Connection) -> None:
    """Drop rows past the TTL, then the oldest beyond ``_MAX_ROWS``. Run on open."""
    c.execute("DELETE FROM decisions WHERE ts < ?", (time.time() - _TTL_S,))
    c.execute("DELETE FROM decisions WHERE k IN (SELECT k FROM decisions ORDER BY ts DESC LIMIT -1 OFFSET ?)",
              (_MAX_ROWS,))


def clear_cache(*, persistent: bool = True) -> None:
    """Test seam and the operator's reset. Stability runs call this between passes."""
    with _lru_lock:
        _lru.clear()
    if persistent:
        c = _conn()
        if c is not None:
            with _db_lock:
                try:
                    c.execute("DELETE FROM decisions")
                    c.commit()
                except sqlite3.Error as exc:
                    failopen.record("LR-FO-ENGINE-CACHE-CLEAR", exc)


def _cache_key(text: str, policy_version: str, ctx_digest: str) -> str:
    # Key on the exact text decide computes on. A normalised key let a variant with a
    # stripped block (a secret, or enough length to change complexity) hit another
    # variant's cached Decision, so the result depended on call order.
    h = hashlib.sha256(text.encode()).hexdigest()
    return "%s|%s|%s" % (h, policy_version, (ctx_digest or "")[:8])


def _cache_get(key: str, mode: str) -> Decision | None:
    if mode == "off":
        return None
    with _lru_lock:
        d = _lru.get(key)
        if d is not None:
            _lru.move_to_end(key)
            return d
    if mode != "sqlite":
        return None
    c = _conn()
    if c is None:
        return None
    try:
        with _db_lock:
            row = c.execute("SELECT v FROM decisions WHERE k=? AND ts >= ?", (key, time.time() - _TTL_S)).fetchone()
        d = Decision(**json.loads(row[0])) if row else None
    except (sqlite3.Error, ValueError, TypeError):
        return None
    if d is not None:
        _lru_put(key, d)
    return d


def _lru_put(key: str, d: Decision) -> None:
    with _lru_lock:
        _lru[key] = d
        _lru.move_to_end(key)
        while len(_lru) > _LRU_MAX:
            _lru.popitem(last=False)


def _cache_put(key: str, d: Decision, mode: str) -> None:
    if mode == "off":
        return
    _lru_put(key, d)
    if mode != "sqlite":
        return
    c = _conn()
    if c is None:
        return
    try:
        with _db_lock:
            c.execute("INSERT OR REPLACE INTO decisions VALUES (?,?,?)",
                      (key, json.dumps(dataclasses.asdict(d), sort_keys=True), time.time()))
            c.commit()
    except sqlite3.Error as exc:
        failopen.record("LR-FO-ENGINE-CACHE-WRITE", exc)


# ── decision ─────────────────────────────────────────────────────────────────


def _safer(tier: str) -> str:
    return TIERS[min(len(TIERS) - 1, TIERS.index(tier) + 1)] if tier in TIERS else tier


def _reason(s: str) -> str:
    return s if len(s) <= 120 else s[:119] + "…"


#: Secret shapes. Every quantifier is bounded, so a scan is linear in the text (a 48k-char run of ``[a-z0-9+.-]``
#: took 1.6 s with the unbounded ``db_url_password`` scheme; LC-12h repair). Which shapes may REFUSE a call is
#: ``HIGH_PRECISION`` below; the rest are counted by category (``local_privacy``) and nothing more.
_SECRET_SHAPES = {
    "anthropic_key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{32,256}"),
    # Listed before openai_key; openai_key excludes this exact shape so one key yields one name.
    "openrouter_key": re.compile(r"\bsk-or-v1-[0-9a-f]{64}(?![A-Za-z0-9_-])"),
    "openai_key": re.compile(r"\bsk-(?!ant-[A-Za-z0-9_-]{32}|or-v1-[0-9a-f]{64}(?![A-Za-z0-9_-]))(?:proj-|svcacct-)?(?=[A-Za-z0-9_-]{0,300}\d)(?=[A-Za-z0-9_-]{0,300}[A-Za-z])[A-Za-z0-9_-]{32,256}"),
    "google_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35,128}"),
    "aws_key_id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{50,255})"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,256}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{8,512}\.[A-Za-z0-9_-]{8,512}\.[A-Za-z0-9_-]{8,512}"),
    "bearer_token": re.compile(r"\bBearer\s{1,8}(?=[A-Za-z0-9._~+/-]{0,300}\d)[A-Za-z0-9._~+/-]{30,512}=*"),
    "db_url_password": re.compile(
        r"\b[a-z][a-z0-9+.-]{0,31}://([^\s:/@]{1,64}):(?![$<{])([^\s@/]{8,128})@[\w.-]{1,255}", re.I),
}
#: ``api_key = "<literal>"`` style assignment of a quoted, high-entropy literal.
_ASSIGN = re.compile(
    r"\b(?:api[_-]?key|secret|token|passw(?:or)?d|auth)[\w-]{0,40}[\"']?\s{0,8}[:=]\s{0,8}[\"']([A-Za-z0-9+/=_-]{20,512})[\"']",
    re.I)

#: The shapes that may refuse a call (LC-12h repair, PR #721 review): a real provider key prefix with enough length
#: and entropy and no placeholder look, a PEM private-key block with a body, a bearer token with entropy, a
#: database URL whose password is not a common word, and a JWT that decodes to real-looking claims (not the jwt.io
#: sample or a test fixture). ``assigned_literal`` (any quoted 20-char mixed literal after "token =") is LOG-ONLY:
#: counted, never refused. Whatever a line, or the two lines above it, labels with a filename or config cue
#: (``.env.example``, ``example.env``, "sample config", a ``.example`` / ``.sample`` / ``.template`` path) is never
#: high either: :func:`_labelled_example`. Free-text words ("fake", "dummy", "placeholder") are not cues.
HIGH_PRECISION = frozenset({"anthropic_key", "openrouter_key", "openai_key", "google_key", "aws_key_id",
                            "github_token", "slack_token", "private_key", "bearer_token", "db_url_password", "jwt"})
#: What a person reads in a refusal (the category, never the value).
SECRET_LABELS = {
    "anthropic_key": "Anthropic API key", "openrouter_key": "OpenRouter API key", "openai_key": "OpenAI API key",
    "google_key": "Google API key", "aws_key_id": "AWS access key", "github_token": "GitHub token",
    "slack_token": "Slack token", "private_key": "private key block", "jwt": "JWT",
    "bearer_token": "bearer token", "db_url_password": "database URL password",
    "assigned_literal": "assigned secret literal",
}

# VALUE-based exemptions (not label-based): the token set below and ``_PLACEHOLDER_SUBSTR`` look at the value itself,
# so a real secret that contains "example" or "dummy" (or any listed word) is exempt. Known residual, documented in
# SECURITY.md; behaviour deliberately unchanged.
_PLACEHOLDER_TOKENS = frozenset({
    "your", "yours", "example", "examples", "sample", "dummy", "fake", "test", "tests", "testing", "demo",
    "placeholder", "changeme", "change", "replace", "redacted", "insert", "here", "todo", "foo", "bar", "xxx",
    "token", "key", "secret", "password", "passwd", "none", "null", "undefined", "mock", "fixture", "temp", "tmp"})
_PLACEHOLDER_SUBSTR = ("changeme", "placeholder", "example", "dummy", "redacted", "yourkey", "yourtoken",
                       "your_", "your-", "replace_me", "replaceme", "insert", "xxxxxxxx")
_COMMON_PASSWORDS = frozenset({
    "password", "passw0rd", "password1", "password123", "pass", "passwd", "secret", "secrets", "postgres",
    "mysql", "root", "rootpassword", "admin", "administrator", "adminpassword", "user", "username", "mypassword",
    "mysecret", "changeme", "example", "test", "testing", "testpass", "testpassword", "dev", "devpassword",
    "localpass", "localhost", "guest", "default", "letmein", "qwerty", "qwertyuiop", "12345678", "123456789",
    "1234567890", "redis", "mongo", "mongodb", "rabbitmq", "secretpassword", "dbpassword", "dbpass",
    "supersecret", "foobarbaz", "hunter2", "welcome", "welcome1", "p@ssw0rd", "p@ssword"})
_KEY_PREFIX = re.compile(r"^(?:sk-ant-|sk-or-v1-|sk-(?:proj-|svcacct-)?|AIza|gh[pousr]_|github_pat_|xox[baprs]-)")
_FILLER_RUN = re.compile(r"[xX*0.]{8,}")


def _sequential(s: str) -> bool:
    """8+ consecutive ascending or descending characters (``01234567``, ``abcdefgh``): a typed placeholder."""
    run = 1
    for i in range(1, len(s)):
        step = ord(s[i]) - ord(s[i - 1])
        run = run + 1 if (step == 1 and s[i].isalnum() and s[i - 1].isalnum()) else 1
        if run >= 8:
            return True
    return False


def _is_placeholder(value: str) -> bool:
    """True for an obviously fake value: documentation keys, filler runs, ``YOUR_...`` / ``<...>`` / ``${...}``,
    ``changeme``-style words, sequential typing."""
    low = value.lower()
    if _FILLER_RUN.search(value) or _sequential(value):
        return True
    if "<" in value or ">" in value or "${" in value or "{{" in value:
        return True
    if any(sub in low for sub in _PLACEHOLDER_SUBSTR):
        return True
    # whole delimiter-separated words only: a random key never splits into "key" or "test"
    return any(tok in _PLACEHOLDER_TOKENS for tok in re.split(r"[_\-. :/]+", low) if tok)


def _random_looking(body: str, *, bits: float = 3.5, distinct: int = 10) -> bool:
    return len(set(body)) >= distinct and _entropy(body) >= bits


def _pem_block_is_real(text: str, end: int) -> bool:
    """A PEM private-key header followed by a base64 body that is not filler (not a bare header in a question)."""
    tail = text[end:end + 4096]
    body = tail.split("-----END", 1)[0]
    body = "".join(body.split())
    return len(body) >= 40 and re.fullmatch(r"[A-Za-z0-9+/=]+", body) is not None and not _is_placeholder(body) \
        and _random_looking(body, bits=3.0, distinct=12)


def _db_password_is_real(user: str, password: str) -> bool:
    low = password.lower()
    if low == user.lower() or low in _COMMON_PASSWORDS or _is_placeholder(password):
        return False
    mixed = ((any(c.isdigit() for c in password) and any(c.isalpha() for c in password))
             or (any(c.isupper() for c in password) and any(c.islower() for c in password)))
    return mixed and _random_looking(password, bits=3.3, distinct=8)


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in (s.count(ch) for ch in set(s)))


#: A filename or config cue near a value that says the value is an example: ``.env.example`` / ``.env.sample`` /
#: ``example.env``, "sample config" / "example env file", or a path ending ``.example`` / ``.sample`` /
#: ``.template``. Free-text words ("fake", "dummy", "placeholder", "not a real", "for illustration") are NOT cues:
#: anyone can write them next to a real key (#721 review, round 2), so they exempt nothing.
_EXAMPLE_LABEL = re.compile(
    r"\.env\.(?:example|sample|template)\b|\b(?:example|sample)\.env\b|"
    r"\b(?:example|sample)\s+(?:env|config|configuration)\b|"
    r"[\w./-]+\.(?:example|sample|template)(?![\w-]|\.\w)", re.I)


def _labelled_example(text: str, start: int, end: int) -> bool:
    """True when the line holding ``text[start:end]``, or one of the two lines above it (a fence or heading), carries
    a filename/config example cue (:data:`_EXAMPLE_LABEL`). The cue is the author's statement that the value is not
    real; it exempts a real-looking value on purpose, and the door counts every exemption
    (``exempted_by_label:<label>``). Bounded to 300 characters before and 200 after the value."""
    window_start = text.rfind("\n", 0, start) + 1
    for _ in range(2):
        window_start = text.rfind("\n", 0, max(0, window_start - 1)) + 1
    line_end = text.find("\n", end)
    lo, hi = max(window_start, start - 300), min(len(text) if line_end < 0 else line_end, end + 200)
    return bool(_EXAMPLE_LABEL.search(text, lo, hi))


_JWT_DUMMY_CLAIM = re.compile(
    r"^(?:1234567890|123456|123|1|0|john doe|jane doe|me|you|subject|sub|"
    r"(?:user|demo|test\w*|admin|alice|bob|foo|bar|baz|fake\w*|dummy\w*|sample\w*|example\w*)[-_. ]?\d{0,4})$", re.I)
_JWT_ID_CLAIMS = ("sub", "name", "email", "preferred_username", "username", "user", "user_id", "uid", "id", "iss", "aud")
_JWT_IO_SIGNATURE = "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"


def _b64json(part: str) -> dict | None:
    import base64
    try:
        out = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:  # noqa: BLE001 - not base64 / not JSON: not a decodable token
        return None
    return out if isinstance(out, dict) else None


def _dummy_claim(value: str) -> bool:
    v = value.strip().lower()
    return bool(_JWT_DUMMY_CLAIM.match(v) or "@example." in v or v.endswith("example.com") or re.search(r"\btest", v)
                or "example" in v or "fixture" in v)


def _jwt_is_real(token: str) -> bool:
    """A JWT that may be a live credential: three decodable parts, a random-looking signature that is not the jwt.io
    sample, not ``alg: none``, and identity claims that are not all test/demo values. The shape alone is how every
    test fixture looks; that was the reviewer's 20/20."""
    header, payload, signature = token.split(".")
    head, body = _b64json(header), _b64json(payload)
    if head is None or body is None or str(head.get("alg", "")).lower() == "none":
        return False
    if signature.startswith(_JWT_IO_SIGNATURE) or _is_placeholder(signature) or not _random_looking(signature, bits=3.0, distinct=12):
        return False
    ids = [str(body[k]) for k in _JWT_ID_CLAIMS if isinstance(body.get(k), (str, int))]
    return not (ids and all(_dummy_claim(v) for v in ids))


#: Text is scanned in windows so no input shape costs more than a window's worth of regex work; consecutive windows
#: overlap by more than the longest possible match (jwt: 3 x 512 + 2; bearer 8 + 512; everything else shorter), so a
#: secret straddling a window edge is whole in the next one. Nothing is skipped: the whole text is scanned.
_SCAN_WINDOW = 16384
_SCAN_OVERLAP = 2048


def _windows(text: str):
    n = len(text)
    if n <= _SCAN_WINDOW:
        yield 0, text
        return
    step = _SCAN_WINDOW - _SCAN_OVERLAP
    for start in range(0, n, step):
        yield start, text[start:start + _SCAN_WINDOW]
        if start + _SCAN_WINDOW >= n:
            return


def secret_findings(text: str, on_exempt: Callable[[str], None] | None = None) -> dict[str, bool]:
    """``{shape name: is_high}`` for the secret shapes in ``text`` (names only, never the secret).

    ``is_high`` is True when some match of a ``HIGH_PRECISION`` shape is a real-looking value: not a placeholder
    (``AKIA...EXAMPLE``, ``ghp_xxxx...``, ``YOUR_API_KEY_HERE``, ``postgres:postgres@``), long and random enough.
    ``on_exempt(name)`` is called for each real-looking match a filename/config label exempted (the door counts them).
    Linear in ``len(text)``."""
    found: dict[str, bool] = {}
    for offset, window in _windows(text):
        for name, pat in _SECRET_SHAPES.items():
            for m in pat.finditer(window):
                if found.get(name):
                    break  # already high: nothing more to learn from this shape
                found.setdefault(name, False)
                if name not in HIGH_PRECISION:
                    break
                if name == "private_key":
                    high = _pem_block_is_real(text, offset + m.end())  # the body may lie past this window
                elif name == "db_url_password":
                    high = _db_password_is_real(m.group(1), m.group(2))
                elif name == "jwt":
                    high = _jwt_is_real(m.group(0))
                elif name == "bearer_token":
                    value = m.group(0).split(None, 1)[1].rstrip("=")
                    high = not value.startswith("eyJ") and not _is_placeholder(value) and _random_looking(value)
                else:
                    value = m.group(0)
                    body = _KEY_PREFIX.sub("", value, count=1)
                    high = not _is_placeholder(value) and _random_looking(
                        body, bits=3.0 if name == "aws_key_id" else 3.5, distinct=9 if name == "aws_key_id" else 12)
                if high and _labelled_example(window, m.start(), m.end()):
                    high = False  # a filename/config cue labels it an example (.env.example, sample config, ...)
                    if on_exempt is not None:
                        on_exempt(name)
                if high:
                    found[name] = True
        if "assigned_literal" not in found:
            for m in _ASSIGN.finditer(window):
                lit = m.group(1)
                if any(c.isdigit() for c in lit) and any(c.isalpha() for c in lit) and _entropy(lit) >= 3.3:
                    found["assigned_literal"] = False
                    break
    return found


def _secret_names(text: str) -> list[str]:
    return list(secret_findings(text))


def secret_names(text: str) -> list[str]:
    """The secret shapes found in ``text`` (names only, never the secret), placeholders included: the engine's
    ``local_only`` signal and the CLS pipeline read this. The secret door (``local_privacy``) reads
    :func:`secret_findings` instead, which separates the shapes that may refuse a call."""
    return _secret_names(text)


def guard(text: str, *, door: str, local_model_healthy: bool, ctx_digest: str = "",
          session_id: str | None = None, task_id: str | None = None) -> Decision:
    """``decide`` plus D-28 enforcement, shaped like a door's call: returns the
    Decision, or raises ``SecretLocalOnlyError`` (non-proxy doors, secret, no
    healthy local model). The proxy never raises (log-only)."""
    d = decide(text, ctx_digest=ctx_digest, door=door, session_id=session_id, task_id=task_id)
    require_local_or_refuse(d, door, local_model_healthy=local_model_healthy)
    return d


def _lexical() -> ml_lexical.LexicalModel | None:
    return ml_lexical.load(_artifact("engine_lexical.json"))


def _policy_version(lex, cal) -> str:
    sig = json.dumps([dataclasses.asdict(ENGINE_POLICY), TAU, abstain_enforced(), lex.version if lex else None, cal],
                     sort_keys=True)
    return "%s-%s" % (POLICY_ID, hashlib.sha256(sig.encode()).hexdigest()[:8])


def canonical_text(text: str) -> str:
    """What every door's class is computed on: the human prompt with Claude Code's harness blocks
    (``<system-reminder>``, ``<command-*>``, ``<agent-message>``, ...) and a leading ``Caveat:`` line
    removed, its last 3,000 chars. That is the proxy's ``steps.tier_text`` of a one-user-message
    body, so a door that stripped and truncated first (the proxy) and a door that passed the raw
    prompt get the same Decision (D-83: a 2,088-char prompt was complex at six doors and moderate at
    the proxy, which classified its 1,995 stripped chars)."""
    from llm_router.proxy.steps import _human_text

    return _human_text(text or "")[-_TIER_TEXT_LIMIT:]


def _compute(text: str, lex, cal, policy_version: str, *, capabilities: bool = True) -> Decision:
    ctext = canonical_text(text)
    sig = classify_signals(ctext, ENGINE_POLICY, capabilities=capabilities)
    cx = sig.complexity.value
    tier = _COMPLEXITY_TIER.get(cx, "sonnet")
    dims = {k: int(v) for k, v in _score_categories(ctext).items()}
    req = sig.capabilities
    needs_tools = bool(req.needs_tools)
    needs_repo = bool(req.repo_search or req.read_files or req.git_operations)

    layer = "L1"
    if sig.confident:
        raw = sig.score / (sig.score + 2.0)
        reason = "L1 %s score=%d" % (sig.task_type.value, sig.score)
    else:
        raw = sig.score / (sig.score + 2.0)  # 0 for no signal, 1/3 for weak
        reason = "L1 low-signal score=%d default=%s" % (sig.score, ENGINE_POLICY.low_signal_default)
        if lex is not None:
            ltier, p = lex.predict(ctext)
            if ltier in TIERS:
                layer, tier, raw = "L3", ltier, p
                cx = _TIER_COMPLEXITY[tier] if _COMPLEXITY_TIER.get(cx) != tier else cx
                reason = "L3 lexical tier=%s p=%.2f (L1 low-signal)" % (tier, p)
    conf, calibrated = _apply_calibration(layer, raw, cal)
    abstain = conf < TAU
    if abstain and abstain_enforced():
        tier = _safer(tier)
        cx = _TIER_COMPLEXITY.get(tier, cx) if _COMPLEXITY_TIER.get(cx) != tier else cx
        reason += "; abstain, tier up"
    elif abstain:
        reason += "; abstain (log only)"
    # Raw text, not ctext: a secret inside a stripped harness block or before the tail must still
    # set local_only (NFR-PRIV).
    secrets = _secret_names(text)
    if secrets:
        reason += "; secret:" + ",".join(secrets)
    task = sig.task_type.value
    return Decision(
        task_type=task, dims=dims, tier=tier, complexity=cx, needs_tools=needs_tools,
        local_eligible=bool(secrets) or (not needs_tools and not abstain and tier != "opus" and task != "image"),
        needs_repo_context=needs_repo, local_only=bool(secrets), confidence=round(conf, 6),
        calibrated=calibrated, abstain=abstain, layer=layer, reason=_reason(reason),
        policy_version=policy_version, engine_version=ENGINE_VERSION)


def _fallback(exc: BaseException, policy_version: str) -> Decision:
    return Decision(
        task_type="query", dims=None, tier=_safer("haiku"), complexity="moderate", needs_tools=False,
        local_eligible=False, needs_repo_context=False, local_only=False, confidence=None,
        calibrated=False, abstain=True, layer="fallback",
        reason=_reason("engine error %s; abstain, safer tier" % type(exc).__name__),
        policy_version=policy_version, engine_version=ENGINE_VERSION)


def compute(text: str, *, ctx_digest: str = "", cache: bool = True) -> Decision:
    """``decide`` without the decision-ledger write: the same Decision for the same text. For
    callers that only read the verdict (the proxy's lane A confidence stamp, which must not add
    a ledger row per continuation). ``cache=False`` also skips the decision cache (no sqlite
    file, no LRU entry), so the call has no side effect at all."""
    t0 = time.perf_counter()
    mode = _cache_mode() if cache else "off"
    lex, cal, pv = None, {}, POLICY_ID
    try:
        lex, cal = _lexical(), _calibration()
        pv = _policy_version(lex, cal)
        key = _cache_key(text or "", pv, ctx_digest)
        hit = _cache_get(key, mode)
        if hit is not None:
            out = dataclasses.replace(hit, layer="cache", dims=dict(hit.dims) if hit.dims else hit.dims,
                                      ms=(time.perf_counter() - t0) * 1000)
        else:
            d = _compute(text or "", lex, cal, pv)
            _cache_put(key, d, mode)
            out = dataclasses.replace(d, ms=(time.perf_counter() - t0) * 1000)
    except Exception as exc:  # noqa: BLE001 — a classifier never stalls routing
        log.warning("engine: decide failed, abstaining: %s", exc)
        out = dataclasses.replace(_fallback(exc, pv), ms=(time.perf_counter() - t0) * 1000)
    return out


def decide(text: str, *, ctx_digest: str = "", door: str, session_id: str | None = None,
           task_id: str | None = None) -> Decision:
    """The one decision. Sync, no LLM, no network, never raises on content.

    ``door`` names the call site and must be one of ``DOORS``; it is NOT part
    of the cache key or the result: every door gets the same Decision for the
    same text (P1.6-a). ``session_id`` / ``task_id`` are accepted for the
    decision ledger (P1.7) and do not affect the decision. The class (task_type, complexity,
    tier, dims, capabilities) is computed on ``canonical_text(text)``; secrets and the cache key
    use the raw text.

    L3 here is the lexical model only. The ``complexity_knn`` vote the plan
    also names embeds the prompt through Ollama, which is a model call on the
    sync path, so it is not used (open question for the owner).
    """
    if door not in DOORS:
        raise ValueError("unknown door %r; expected one of %s" % (door, ", ".join(DOORS)))
    out = compute(text, ctx_digest=ctx_digest)
    decision_ledger.record(out, door=door, text=text or "", session_id=session_id, task_id=task_id)  # P1.7; never raises
    return out


# ── wiring mode: shadow by default (PLAN v16 D-72) ───────────────────────────

#: ``LLM_ROUTER_ENGINE_MODE``: ``shadow`` (default), ``decide`` or ``off``.
#: ``shadow``: every wired door routes with its own rules, exactly as before this wiring, and the
#: engine computes a Decision AFTER that, logged to ``engine_shadow.jsonl`` next to the rules class.
#: ``decide``: the engine decides at the doors (the #433 behaviour). NOT the v16.0 default:
#: P1.6-g FAILed on the outcome basis (D-71/D-72), so the decider role moves to 16.1.
#: ``off``: rules only, no engine call at all. An unknown value reads as ``shadow``.
MODE_ENV = "LLM_ROUTER_ENGINE_MODE"
SHADOW_LOG = "engine_shadow.jsonl"
_SHADOW_MAX_BYTES = 2_000_000  # capped_log keeps this + one rotated generation


def wire_mode() -> str:
    v = os.environ.get("LLM_ROUTER_ENGINE_MODE", "shadow").strip().lower()  # literal: test_env_registry scans for it
    return v if v in ("shadow", "decide", "off") else "shadow"


def decides() -> bool:
    """True when the engine, not the door's rules, decides the route."""
    return wire_mode() == "decide"


def _decide_light(text: str, *, door: str, lexical: bool) -> Decision:
    """The shadow call's Decision: no capability detector (``needs_tools`` / ``needs_repo_context`` /
    ``local_eligible`` are not computed), no cache read or write, no sqlite. ``lexical`` adds L3 when
    the model is already loadable (server doors); the hooks pass False (L1 only: a short-lived
    process must not load an artifact for a log row). task_type, tier and complexity are what
    ``decide`` returns for the same text without the cache."""
    if door not in DOORS:
        raise ValueError("unknown door %r; expected one of %s" % (door, ", ".join(DOORS)))
    t0 = time.perf_counter()
    lex, cal = (_lexical() if lexical else None), _calibration()
    d = _compute(text or "", lex, cal, _policy_version(lex, cal), capabilities=False)
    return dataclasses.replace(d, ms=(time.perf_counter() - t0) * 1000)


def _shadow_run(text: str, door: str, rules_task: str | None, rules_complexity: str | None,
                l1_only: bool, log_path: str | None = None) -> Decision | None:
    try:
        d = _decide_light(text, door=door, lexical=not l1_only)
        row = dataclasses.asdict(d)
        row.update(needs_tools=None, needs_repo_context=None, local_eligible=None, light=True)  # not computed
        rtier = _COMPLEXITY_TIER.get(rules_complexity or "")
        row.update(ts=round(time.time(), 3), door=door, l1_only=bool(l1_only), rules_task_type=rules_task,
                   rules_complexity=rules_complexity, rules_tier=rtier,
                   agree=bool(rtier == d.tier and rules_task == d.task_type))
        # The background worker may run after the caller's environment changed (a test restoring
        # HOME, a reloaded config), so the queued path is the one resolved at enqueue time.
        target = Path(log_path) if log_path else state_path(SHADOW_LOG)
        capped_log.append(target, (json.dumps(row, separators=(",", ":")) + "\n").encode(),
                          _SHADOW_MAX_BYTES)
        return d
    except Exception as exc:  # noqa: BLE001 — a shadow classifier must never touch routing
        failopen.record("LR-FO-ENGINE-SHADOW", exc)
        return None


_SHADOW_QUEUE_MAX = 256
_pool_lock = threading.Lock()
# One ThreadPoolExecutor worker, not a daemon thread: it is joined at interpreter exit, so queued
# rows finish (or the exit waits for at most the 256 queued items) before the process ends.
_pool = None
_pending = 0


def _submit(args: tuple) -> None:
    global _pool, _pending
    from concurrent.futures import ThreadPoolExecutor

    with _pool_lock:
        if _pending >= _SHADOW_QUEUE_MAX:
            failopen.record("LR-FO-ENGINE-SHADOW-DROP", RuntimeError("shadow queue full"))
            return
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm_router-engine-shadow")
        _pending += 1
        fut = _pool.submit(_shadow_run, *args)

    def _done(_f) -> None:
        global _pending
        with _pool_lock:
            _pending -= 1

    fut.add_done_callback(_done)


def shadow_drain(timeout: float = 10.0) -> bool:
    """Wait for queued background shadow rows (tests, shutdown). True when the queue is empty."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with _pool_lock:
            if _pending == 0:
                return True
        time.sleep(0.005)
    return False


def shadow(text: str, *, door: str, rules_task: str | None, rules_complexity: str | None,
           l1_only: bool = False, background: bool = False) -> Decision | None:
    """Shadow mode: compute the engine's Decision and log it beside the rules class.

    Call it AFTER the rules decision, and never use the result to route. ``background=True`` (the
    long-lived server doors: proxy, gateway, MCP, router) queues the work to one
    ThreadPoolExecutor worker (NOT a daemon thread: the worker is joined at interpreter exit, and the queue is bounded at 256 items), so
    the request path pays only the queue put and the result is None; the hooks are short-lived
    processes and run it inline with ``l1_only=True``. Returns the Decision when run inline (None
    when the mode is not ``shadow`` or the engine failed). Never raises into the request path: any
    error is recorded through ``failopen`` (``LR-FO-ENGINE-SHADOW``). The row holds the Decision's
    fields and the rules tier, no prompt text."""
    if wire_mode() != "shadow":
        return None
    if background:
        try:
            _submit((text, door, rules_task, rules_complexity, l1_only, str(state_path(SHADOW_LOG))))
        except Exception as exc:  # noqa: BLE001
            failopen.record("LR-FO-ENGINE-SHADOW", exc)
        return None
    return _shadow_run(text, door, rules_task, rules_complexity, l1_only)


# ── hook -> proxy reuse (P1.6 task 5, P1.6-h) ────────────────────────────────

#: ``proxy.steps.tier_text`` keeps the last 3,000 chars of the newest human prompt.
_TIER_TEXT_LIMIT = 3000
_REUSE_TTL_S = 300.0  # the proxy's turn-first call follows the hook's prompt by seconds
_session_var: "contextvars.ContextVar[str | None]" = contextvars.ContextVar("engine_session", default=None)


def text_sha(text: str) -> str:
    """The ledger's ``text_sha``: ``prompt_key.key`` (16 hex of sha256 over the normalized text)."""
    return prompt_key.key(text)


def _tier_sha(text: str) -> str:
    return prompt_key.key(prompt_key.normalize(text)[-_TIER_TEXT_LIMIT:])


def _reuse_path(session_id: str) -> Path:
    return state_path("last_classification_%s.json" % re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:64])


def reuse_fields(text: str, decision: Decision) -> dict:
    """The keys the hook adds to its ``last_classification_<sid>.json`` sidecar so the
    proxy can reuse the Decision: ``text_sha``, ``tier_sha`` (sha of the proxy's
    3,000-char tail), ``decision`` and ``ts``. Additive: the sidecar's older readers
    (``tools/text.py``, ``doctor``) never look at them. No prompt text."""
    return {"text_sha": text_sha(text), "tier_sha": _tier_sha(text),
            "decision": dataclasses.asdict(decision), "ts": time.time()}


def write_hook_decision(session_id: str | None, text: str, decision: Decision) -> None:
    """Merge ``reuse_fields`` into the session's sidecar (0600, atomic). The hook itself
    writes the sidecar once per turn with the same fields; this is for callers that are
    not the hook. Never raises: a failed write costs one re-classification, nothing else."""
    if not session_id:
        return
    path = _reuse_path(session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            cur = json.loads(path.read_text())
            cur = cur if isinstance(cur, dict) else {}
        except (OSError, ValueError):
            cur = {}
        cur.update(reuse_fields(text, decision))
        tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(cur, fh)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("engine: could not write hook decision: %s", exc)


def read_hook_decision(session_id: str | None, text: str) -> tuple[bool, Decision | None]:
    """``(hook_seen, decision)``. ``hook_seen`` is True when a fresh record exists for
    the session; ``decision`` is set only when its sha matches ``text`` (the full
    normalized prompt, or the proxy's 3,000-char tail of it)."""
    if not session_id:
        return False, None
    try:
        rec = json.loads(_reuse_path(session_id).read_text())
        if "decision" not in rec:  # a sidecar written before this turn's reuse fields: not seen
            return False, None
        if time.time() - float(rec["ts"]) > _REUSE_TTL_S:
            return False, None
        if text_sha(text) in (rec.get("text_sha"), rec.get("tier_sha")):
            return True, Decision(**rec["decision"])
        return True, None
    except (OSError, ValueError, KeyError, TypeError):
        return False, None


def bind_session(session_id: str | None):
    """Name the session for the next ``decide_for_proxy`` in this context; returns a token for ``unbind_session``."""
    return _session_var.set(session_id)


def unbind_session(token) -> None:
    _session_var.reset(token)


def _note_reuse(hook_seen: bool, reused: bool) -> None:
    """One counts-only line per hook-seen proxy classification: the P1.6-h instrument
    (``scripts/hook_reuse_rate.py`` reads it). No text, no session id."""
    if not hook_seen:
        return
    try:
        p = state_path("decision_reuse.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps({"ts": round(time.time(), 3), "reused": bool(reused)}) + "\n")
    except OSError:
        pass


def decide_for_proxy(text: str, *, session_id: str | None = None) -> Decision:
    """The proxy's door: the hook's Decision for this turn when its ``text_sha``
    matches (one classification per turn), else ``decide(door="proxy")``."""
    sid = session_id if session_id is not None else _session_var.get()
    seen, d = read_hook_decision(sid, text)
    _note_reuse(seen, d is not None)
    return d if d is not None else decide(text, door="proxy", session_id=sid)


# ── L2: schema-checked LLM verdicts (not on the sync path) ───────────────────


def apply_l2(base: Decision, raw) -> Decision:
    """Merge an LLM verdict into ``base`` only if it is schema-valid.

    Valid: a dict (or JSON string) with ``task_type`` in the known set,
    ``tier`` in ``TIERS`` and ``confidence`` a finite number in [0, 1]. Anything
    else abstains: ``base`` one tier safer, ``abstain=True``, and a warning is
    logged (P1.6-f). ``decide`` never calls this; an L2 layer gets authority
    only through a D-19 B/C candidate that passes task 8.
    """
    try:
        v = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        ok = (isinstance(v, dict) and v.get("task_type") in _TASK_TYPES and v.get("tier") in TIERS
              and isinstance(v.get("confidence"), (int, float)) and not isinstance(v.get("confidence"), bool)
              and math.isfinite(v["confidence"]) and 0.0 <= v["confidence"] <= 1.0)
    except (ValueError, TypeError):
        ok = False
    if not ok:
        log.warning("engine: L2 output failed schema, abstaining (layer=%s)", base.layer)
        return dataclasses.replace(base, tier=_safer(base.tier), abstain=True,
                                   complexity=_TIER_COMPLEXITY.get(_safer(base.tier), base.complexity),
                                   reason=_reason("L2 output schema-invalid; abstain, tier up"))
    conf = float(v["confidence"])
    abstain = conf < TAU
    tier = _safer(v["tier"]) if abstain else v["tier"]
    return dataclasses.replace(base, task_type=v["task_type"], tier=tier,
                               complexity=_TIER_COMPLEXITY[tier], confidence=conf, calibrated=False,
                               abstain=abstain, layer="L2", reason=_reason("L2 %s %s" % (v["task_type"], tier)))
