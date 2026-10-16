#!/usr/bin/env python3
"""CLS-v16 send library (build B-L). Every off-machine or cross-process send goes through here.

Owns: the secret detector (PREREG-CLS-DEV 3.2 a-e), PII replacement, the independent scanner,
`outgoing_check` on the exact bytes, the only child-spawning wrapper `send`, child argv/env
builders, the Codex read-scope canary, the OpenRouter guard, the spend counter, TEST-SYN custody
guards and the session-log helpers.

Privacy: no function here prints, logs or raises item text. Exceptions carry detector names and
batch ids only. Stdlib only; the repo detectors are imported lazily.

CLI: `cls_send_lib.py self-test|session-snapshot|session-log-report|purge` (counts and paths only).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# Exit codes shared by every CLS-v16 B-L tool.
EXIT_OK = 0
EXIT_REFUSE = 5
EXIT_SELFTEST = 6
EXIT_PINS = 9
EXIT_CUSTODY = 10
EXIT_CANARY = 11
EXIT_OPENROUTER = 12
EXIT_PLANT_VOID = 13
EXIT_RESEMBLE_FAIL = 14
EXIT_NO_COUNTS = 15
EXIT_SPEC_VERSION = 16
EXIT_RESEMBLANCE_FAILED = 17
EXIT_REGEN_LIMIT = 18
EXIT_TWO_VOIDS = 19
EXIT_GEN_FILE = 20
EXIT_CAP = 21
EXIT_MODEL_ID = 22
EXIT_SCRUB_ABORT = 23
EXIT_GPT_PARK = 24  # a codex call returned non-zero (bad -m, timeout, CLI error): stop and park, never a void

SEED = 20261017
DIGEST_CAP = 400
CLAUDE_TIMEOUT_S = 900


class CLSExit(Exception):
    """A refusal with an exit code. The message never contains item text."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class ScrubAbort(CLSExit):
    """Raised by outgoing_check. Carries detector names and the batch id, never the text."""

    def __init__(self, detectors: list[str], batch_id: str = ""):
        super().__init__(EXIT_SCRUB_ABORT, "scrub_abort batch=%s detectors=%s" % (batch_id, ",".join(detectors)))
        self.detectors = list(detectors)
        self.batch_id = batch_id


class CwdNotEmpty(CLSExit):
    def __init__(self, cwd: str):
        super().__init__(EXIT_REFUSE, "child cwd is not an empty directory: %s" % cwd)


def utc_now() -> str:
    """System clock only (date -u semantics); never a typed stamp."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_private(path, data: bytes | str, mode: int = 0o600) -> None:
    """Write a file with a restrictive mode; the parent is created 0700."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(str(p), mode)


def append_jsonl(path, record: dict) -> None:
    """Append one JSON line (0600). Append-only logs are never rewritten."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def read_jsonl(path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------------------------
# Detector (a)-(e)
# ---------------------------------------------------------------------------------------------

_ENV_KEY_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASS", re.IGNORECASE)
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
ENV_MIN_LEN = 8


def env_key_qualifies(name: str) -> bool:
    return bool(_ENV_KEY_RE.search(name))


@dataclass(frozen=True, repr=False)
class ScrubContext:
    """In-memory denylist entries and qualifying .env values. Never printed or logged."""

    denylist: frozenset = frozenset()
    env_values: frozenset = frozenset()

    def __repr__(self) -> str:
        return "ScrubContext(denylist=%d, env_values=%d)" % (len(self.denylist), len(self.env_values))


_CTX = ScrubContext()


def configure(ctx: ScrubContext) -> None:
    """Set the process-wide context used when a caller passes none."""
    global _CTX
    _CTX = ctx


def load_denylist(path) -> frozenset:
    if not path or not Path(path).exists():
        return frozenset()
    out = set()
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.add(s)
    return frozenset(out)


def load_env_values(path) -> tuple[frozenset, int]:
    """Qualifying values of the owner's .env: key matches KEY|TOKEN|SECRET|PASSWORD|PASS
    (case-insensitive), value length >= 8, empty skipped. Returns (values, count). Values stay
    in memory."""
    if not path or not Path(path).exists():
        return frozenset(), 0
    vals = set()
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = _ENV_LINE_RE.match(line)
        if not m:
            continue
        name, val = m.group(1), m.group(2)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if not val or len(val) < ENV_MIN_LEN:
            continue
        if not env_key_qualifies(name):
            continue
        vals.add(val)
    return frozenset(vals), len(vals)


def env_sha_at_use(path) -> str | None:
    """sha256 of the .env file, logged at each use (the file changing is intentional)."""
    if path and Path(path).exists():
        return sha256_file(path)
    return None


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in (s.count(ch) for ch in set(s)))


def _entropy_token_hit(text: str) -> bool:
    for tok in text.split():
        if len(tok) >= 24 and _entropy(tok) >= 3.3 and any(c.isalpha() for c in tok) and any(c.isdigit() for c in tok):
            return True
    return False


def _repo_detectors():
    try:
        from llm_router.engine import _secret_names
    except ImportError:
        sys.path.insert(0, str(REPO / "src"))
        from llm_router.engine import _secret_names
    from llm_router.persist_redaction import _PROSE_SECRET_RE
    from llm_router.secret_scrubber import scrub_text

    return _secret_names, scrub_text, _PROSE_SECRET_RE


def scrub_detect(text: str, ctx: ScrubContext | None = None) -> list[str]:
    """Detector names that fire on text: engine, scrubber, prose, denylist, env_value, entropy."""
    ctx = ctx if ctx is not None else _CTX
    secret_names, scrub_text, prose_re = _repo_detectors()
    hits = []
    if secret_names(text):
        hits.append("engine")
    if scrub_text(text) != text:
        hits.append("scrubber")
    if prose_re.search(text):
        hits.append("prose")
    if any(e in text for e in ctx.denylist if e):
        hits.append("denylist")
    if any(v in text for v in ctx.env_values if v):
        hits.append("env_value")
    if _entropy_token_hit(text):
        hits.append("entropy")
    return hits


# ---------------------------------------------------------------------------------------------
# PII replacement
# ---------------------------------------------------------------------------------------------

_SRC_EXT = {"py", "js", "ts", "tsx", "jsx", "md", "json", "txt", "rs", "go", "java", "c", "h", "cpp",
            "rb", "sh", "toml", "yaml", "yml", "html", "css", "sql", "lock", "cfg", "ini", "log",
            "ipynb", "swift", "kt", "php", "xml", "csv", "jsonl"}
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_HOME_RE = re.compile(r"/(?:Users|home)/[^/\s\"'`:;,)\]>]+")
_PHONE_RE = re.compile(
    r"(?<![\w.-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)|\d{2,4})[ .-]\d{3,4}[ .-]\d{3,4}(?![\w.-])"
    r"|(?<![\w.-])\+\d{8,14}(?![\w.-])")
_IP_RE = re.compile(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\w.])")
_HOSTNAME_RE = re.compile(r"(?<![\w.-])[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*\.(?:local|internal|lan|corp|home)\b(?::\d{2,5})?",
                          re.IGNORECASE)
_HOSTPORT_RE = re.compile(
    r"(?<![\w.-])(?:localhost|[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+|[A-Za-z][A-Za-z0-9-]*(?=:\d{4,5}\b)):\d{2,5}\b")
TOKENS = {"email": "[EMAIL]", "phone": "[PHONE]", "home": "[HOME]", "host": "[HOST]", "ip": "[IP]"}


def _is_internal_ip(m: re.Match) -> bool:
    o = [int(g) for g in m.groups()]
    if any(x > 255 for x in o):
        return False
    return (o[0] == 10 or (o[0] == 172 and 16 <= o[1] <= 31) or (o[0] == 192 and o[1] == 168)
            or (o[0] == 169 and o[1] == 254) or o[0] == 127)


def _hostport_sub(m: re.Match) -> str:
    host = m.group(0).rsplit(":", 1)[0]
    last = host.rsplit(".", 1)[-1].lower()
    if "." in host and last in _SRC_EXT:
        return m.group(0)  # file.py:123 is a file reference, not a host
    return TOKENS["host"]


def pii_replace(text: str) -> str:
    """Emails, phones, absolute home paths, hostnames/host:port and internal IPs -> fixed tokens.
    Owner repo and project names are NOT replaced (disclosed). Idempotent."""
    t = _EMAIL_RE.sub(TOKENS["email"], text)
    t = _HOME_RE.sub(TOKENS["home"], t)
    t = _HOSTNAME_RE.sub(TOKENS["host"], t)
    t = _IP_RE.sub(lambda m: TOKENS["ip"] if _is_internal_ip(m) else m.group(0), t)
    t = _HOSTPORT_RE.sub(_hostport_sub, t)
    t = _PHONE_RE.sub(TOKENS["phone"], t)
    return t


# ---------------------------------------------------------------------------------------------
# Independent scanner (shares no pattern with a-e)
# ---------------------------------------------------------------------------------------------

_INDEP_SHAPES = {
    "begin_marker": re.compile(r"-----BEGIN\b"),
    "url_userinfo": re.compile(r"://[^\s/@:]+:[^\s/@]{3,}@"),
    "uuid_near_keyword": re.compile(
        r"(?i)(?:key|token|secret)\W{0,12}[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
    "assigned_mixedcase": re.compile(
        r"[=:]\s*[\"']?(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{20,}"),
    "high_entropy_blob": re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/])"),
}


def _builtin_independent(text: str) -> list[str]:
    hits = []
    for name, pat in _INDEP_SHAPES.items():
        for m in pat.finditer(text):
            if name == "high_entropy_blob":
                s = m.group(0)
                if s.count("/") >= 3:  # a filesystem path, not a base64 blob
                    continue
                if not (any(c.isdigit() for c in s) and any(c.isalpha() for c in s) and _entropy(s) >= 3.5):
                    continue
            hits.append("independent:" + name)
            break
    if re.search(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40,}(?![0-9a-fA-F])", text):
        hits.append("independent:long_hex")
    return hits


def _tool_scan(text: str, which, runner) -> list[str] | None:
    """gitleaks or trufflehog when on PATH (no network: trufflehog runs with --no-verification).
    Documented argv. Returns hit names, or None when no tool is installed. A tool error fails
    closed (a hit named independent:tool_error)."""
    gl = which("gitleaks")
    if gl:
        argv = [gl, "stdin", "--no-banner", "--redact", "--exit-code", "1"]
        try:
            r = runner(argv, input=text.encode("utf-8", errors="replace"), capture_output=True, timeout=120,
                       env={"PATH": os.environ.get("PATH", "")}, shell=False)
        except (OSError, subprocess.SubprocessError):
            return ["independent:tool_error"]
        if r.returncode == 0:
            return []
        return ["independent:gitleaks"] if r.returncode == 1 else ["independent:tool_error"]
    th = which("trufflehog")
    if th:
        d = tempfile.mkdtemp(prefix="cls-scan-")
        os.chmod(d, 0o700)
        f = os.path.join(d, "scan.txt")
        write_private(f, text)
        argv = [th, "filesystem", f, "--no-update", "--no-verification", "--json", "--fail"]
        try:
            r = runner(argv, capture_output=True, timeout=120, env={"PATH": os.environ.get("PATH", "")}, shell=False)
        except (OSError, subprocess.SubprocessError):
            return ["independent:tool_error"]
        finally:
            shutil.rmtree(d, ignore_errors=True)
        out = r.stdout.decode("utf-8", errors="replace") if isinstance(r.stdout, bytes) else (r.stdout or "")
        if r.returncode == 0 and not out.strip():
            return []
        return ["independent:trufflehog"] if (out.strip() or r.returncode == 183) else ["independent:tool_error"]
    return None


def independent_scan(text: str, *, which=shutil.which, runner=subprocess.run) -> list[str]:
    """Second scanner. A tool on PATH (gitleaks, trufflehog) is used when present, else the built-in
    regex set that shares no pattern with the a-e detector."""
    tool = _tool_scan(text, which, runner)
    if tool is not None:
        return tool
    return _builtin_independent(text)


def outgoing_check(payload: bytes, *, batch_id: str = "", ctx: ScrubContext | None = None) -> None:
    """Scan the EXACT bytes about to be sent. Any hit raises ScrubAbort (names, never text)."""
    text = payload.decode("utf-8", errors="replace")
    hits = scrub_detect(text, ctx) + independent_scan(text)
    if hits:
        raise ScrubAbort(hits, batch_id)


# ---------------------------------------------------------------------------------------------
# Self-test (per shape)
# ---------------------------------------------------------------------------------------------

def _j(*parts: str) -> str:
    return "".join(parts)


def _shape_table():
    """(shape, expected detector, secret built from fragments, clean control). No real-looking literal."""
    return [
        ("anthropic_key", "engine", _j("sk-", "ant-", "A1b2C3d4" * 5), "the anthropic client takes a model name"),
        ("openrouter_key", "engine", _j("sk-", "or-v1-", "0123456789abcdef" * 4), "openrouter routes to many models"),
        ("openai_key", "engine", _j("sk-", "proj-", "Ab12" * 10), "the openai client is configured elsewhere"),
        ("google_key", "engine", _j("AI", "za", "Xy9-" * 9), "google search is a web tool"),
        ("aws_key_id", "engine", _j("AK", "IA", "ABCD1234EFGH5678"), "the aws region is us-east-1"),
        ("github_token", "engine", _j("gh", "p_", "aB3dE5gH7j" * 4), "github hosts the repository"),
        ("slack_token", "engine", _j("xo", "xb-", "1234567890-1234567890-", "abcdefghij"), "slack is the chat tool"),
        ("private_key", "engine", _j("-----BEGIN ", "RSA PRIVATE", " KEY-----"), "a pem file holds the certificate"),
        ("jwt", "engine", _j("ey", "JhbGciOiJIUzI1", ".", "eyJzdWIiOjEyMzQ", ".", "abcdefghij12"), "the session uses a signed cookie"),
        ("bearer_token", "engine", _j("Bear", "er ", "a1b2c3d4e5" * 3, "x1"), "the auth header format is documented"),
        ("db_url_password", "engine", _j("post", "gres://", "user", ":", "pwAbcdef12", "@db.example.com"), "the database url lives in config"),
        ("assigned_literal", "engine", _j("api_", "key = \"", "aB3dE5gH7jK9mN2pQ4rS", "\""), "set the api key from the vault"),
        ("scrubber_password", "scrubber", _j("pass", "word=", "hunter22"), "the user changes the account setting"),
        ("prose_secret", "prose", _j("the launch ", "code is ", "ORANGE-742"), "the launch is next week"),
        ("denylist_entry", "denylist", "see zq-fake-denylist-entry-7391 here", "see the usual entry here"),
        ("env_value", "env_value", "uses fakeEnvVal-0042x as given", "uses the given value as set"),
        ("entropy_token", "entropy", "paste q7Zk2mXv9Lp4Rt8Wn3Yb6Hc5Jd1 now", "paste the sentence now"),
        ("indep_begin_marker", "independent", _j("-----BEGIN ", "CERTIFICATE-----"), "a certificate request"),
        ("indep_url_userinfo", "independent", _j("https", "://alice", ":Zx81qpLm", "@host.example.com/x"), "https://host.example.com/x"),
        ("indep_uuid_near_keyword", "independent", _j("the api ", "key 123e4567-e89b-12d3-", "a456-426614174000"), "request 7 of 9 done"),
        ("indep_assigned_mixedcase", "independent", _j("client_id: ", "aB3dE5gH7jK9mN2pQ4rStU"), "client_id: none"),
        ("indep_high_entropy_blob", "independent", _j("dGhpcyBp", "cyBhIGZha2U", "gYmxvYiBvZiBkYXRh", "1234"), "a short readable sentence"),
        ("indep_long_hex", "independent", _j("0123456789abcdef", "0123456789abcdef", "01234567"), "a short hex 0a1b"),
    ]


def self_test_context() -> ScrubContext:
    return ScrubContext(denylist=frozenset({"zq-fake-denylist-entry-7391"}), env_values=frozenset({"fakeEnvVal-0042x"}))


def scrubber_sha() -> str:
    """Hash of everything the detector is made of: this file plus the three repo detector modules."""
    h = hashlib.sha256()
    for p in (Path(__file__), REPO / "src/llm_router/engine.py", REPO / "src/llm_router/secret_scrubber.py",
              REPO / "src/llm_router/persist_redaction.py"):
        h.update(sha256_file(p).encode())
    return h.hexdigest()


def self_test(result_path=None, *, env_path=None, scan=None) -> tuple[bool, dict]:
    """One fake secret per shape plus one clean control per shape; reported PER SHAPE. The test is
    green only when every shape fires its own detector and no control flags. Writes the result file."""
    ctx = self_test_context()
    scan = scan or independent_scan
    shapes, controls = {}, {}
    for name, expected, secret, control in _shape_table():
        fired = scrub_detect(secret, ctx) + [h.split(":")[0] for h in scan(secret)]
        shapes[name] = {"expected": expected, "fired": sorted(set(fired)), "ok": expected in fired}
        flagged = scrub_detect(control, ctx) + scan(control)
        controls[name] = {"flagged": sorted(set(flagged)), "ok": not flagged}
    ok = all(s["ok"] for s in shapes.values()) and all(c["ok"] for c in controls.values())
    report = {"ok": ok, "utc": utc_now(), "scrubber_sha": scrubber_sha(), "shapes": shapes, "controls": controls,
              "env_sha_at_use": env_sha_at_use(env_path)}
    if result_path:
        write_private(result_path, json.dumps(report, sort_keys=True, indent=1) + "\n")
    return ok, report


def require_fresh_selftest(result_path) -> None:
    """Other subcommands refuse (exit 6) if the result file is absent, failed, or stale against the
    scrubber's own sha."""
    p = Path(result_path) if result_path else None
    if not p or not p.exists():
        raise CLSExit(EXIT_SELFTEST, "self-test result file absent")
    try:
        rep = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        raise CLSExit(EXIT_SELFTEST, "self-test result file unreadable") from None
    if not rep.get("ok"):
        raise CLSExit(EXIT_SELFTEST, "self-test result file records a failure")
    if rep.get("scrubber_sha") != scrubber_sha():
        raise CLSExit(EXIT_SELFTEST, "self-test result is stale against the scrubber sha")


# ---------------------------------------------------------------------------------------------
# Child argv / env / cwd
# ---------------------------------------------------------------------------------------------

def claude_child_env(base: dict | None = None) -> dict:
    """Real HOME (auth only). ANTHROPIC_BASE_URL and ANTHROPIC_API_KEY removed and asserted absent."""
    env = dict(os.environ if base is None else base)
    env.pop("ANTHROPIC_BASE_URL", None)
    env.pop("ANTHROPIC_API_KEY", None)
    env["LLM_ROUTER_SESSION_KIND"] = "research"
    assert "ANTHROPIC_BASE_URL" not in env and "ANTHROPIC_API_KEY" not in env
    return env


def codex_child_env(base: dict | None = None) -> dict:
    """Real HOME (its auth lives there). API-key variables removed so the subscription login is used."""
    env = dict(os.environ if base is None else base)
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        env.pop(k, None)
    return env


def write_claude_settings(run_dir) -> Path:
    p = Path(run_dir) / "claude_settings.json"
    write_private(p, json.dumps({"disableAllHooks": True}) + "\n")
    return p


def claude_argv(model: str, settings_path, claude_bin: str = "claude") -> list[str]:
    """claude -p, hooks off, no tools (the empty --tools list makes the call single-turn print; there is
    no --max-turns in Claude Code 2.1.299), no session file. The prompt goes on STDIN."""
    return [claude_bin, "-p", "--safe-mode", "--settings", str(settings_path), "--tools", "",
            "--strict-mcp-config", "--no-session-persistence", "--setting-sources", "project,local",
            "--model", model, "--output-format", "json"]


def codex_argv(cwd, model: str | None = None, codex_bin: str = "codex", *, json_events: bool = True) -> list[str]:
    """codex exec, read-only sandbox, canary-verified, --ephemeral (no session files), empty cwd,
    prompt on stdin. Every call passes `-m <pinned id>` (D-147): codex serves that model or exits non-zero
    (an unknown id exits rc=1), so the served model is enforced by the CLI, not read back. The --json event
    stream (thread.started, turn.started, item.completed, turn.completed) carries NO model field and is
    used for the answer text only. json_events=False is the plain mode, whose header prints `model: <id>`
    (used by the canary)."""
    if not model:
        raise CLSExit(EXIT_MODEL_ID, "codex call refused: no pinned model id for -m")
    argv = [codex_bin, "exec", "-s", "read-only", "--skip-git-repo-check", "--ephemeral", "-C", str(cwd)]
    if json_events:
        argv.append("--json")
    return argv + ["-m", model]


def fresh_empty_cwd(parent) -> Path:
    Path(parent).mkdir(parents=True, exist_ok=True, mode=0o700)
    d = Path(tempfile.mkdtemp(prefix="cwd-", dir=str(parent)))
    os.chmod(d, 0o700)
    assert_empty_cwd(d)
    return d


def assert_empty_cwd(cwd) -> None:
    p = Path(cwd)
    if not p.is_dir() or any(p.iterdir()):
        raise CwdNotEmpty(str(cwd))


TIMEOUT_RC = 124


@dataclass
class SendResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


def _as_bytes(b) -> bytes:
    if b is None:
        return b""
    return b.encode("utf-8", errors="replace") if isinstance(b, str) else b


def send(child_argv: list[str], payload: bytes, env: dict, cwd, *, batch_id: str = "", timeout: int = CLAUDE_TIMEOUT_S,
         runner=subprocess.run) -> SendResult:
    """The ONLY place a judge or generator child is spawned. Order: empty-cwd assertion, outgoing_check
    on the final bytes, then spawn (shell=False, payload on stdin, never argv). A timeout is returned as a
    failed call (returncode 124, timed_out=True) so the caller records it; it is never an uncaught crash."""
    assert_empty_cwd(cwd)
    outgoing_check(payload, batch_id=batch_id)
    try:
        r = runner(list(child_argv), input=payload, env=env, cwd=str(cwd), shell=False, capture_output=True,
                   timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return SendResult(TIMEOUT_RC, _as_bytes(e.stdout), _as_bytes(e.stderr), timed_out=True)
    return SendResult(r.returncode, r.stdout or b"", r.stderr or b"")


def child_call(kind: str, model: str, payload: bytes, work_dir, *, claude_bin: str = "claude",
               codex_bin: str = "codex", timeout: int = CLAUDE_TIMEOUT_S, batch_id: str = "",
               json_events: bool = True) -> tuple[SendResult, int]:
    """One judge or generator child call through `send`, in a fresh empty cwd under work_dir/cwds.
    kind is "claude" or "codex". Returns (result, leftover) where leftover is 1 when the cwd could not
    be removed because the child wrote into it. json_events=False runs codex in plain mode (canary)."""
    wd = Path(work_dir)
    cwd = fresh_empty_cwd(wd / "cwds")
    leftover = 0
    try:
        if kind == "codex":
            argv, env = codex_argv(cwd, model, codex_bin, json_events=json_events), codex_child_env()
        else:
            argv, env = claude_argv(model, write_claude_settings(wd), claude_bin), claude_child_env()
        res = send(argv, payload, env, cwd, batch_id=batch_id, timeout=timeout)
    finally:
        try:
            os.rmdir(cwd)
        except OSError:
            leftover = 1
    return res, leftover


# ---------------------------------------------------------------------------------------------
# Output parsing and model-id families
# ---------------------------------------------------------------------------------------------

def family_of(model_id: str) -> str:
    m = (model_id or "").lower()
    for fam in ("opus", "sonnet", "haiku"):
        if fam in m:
            return fam
    if m.startswith("gpt") or "codex" in m:
        return "gpt"
    return "unknown"


def parse_claude_json(stdout: bytes) -> dict:
    """claude -p --output-format json -> {text, model_ids, cost_usd, tokens}. Missing modelUsage -> no ids."""
    try:
        obj = json.loads(stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return {"text": "", "model_ids": [], "cost_usd": 0.0, "tokens": 0, "ok": False}
    if isinstance(obj, list):  # stream-json style array: take the result entry
        obj = next((o for o in reversed(obj) if isinstance(o, dict) and "result" in o), {})
    usage = obj.get("modelUsage") or {}
    tokens = 0
    for v in usage.values():
        if isinstance(v, dict):
            tokens += int(v.get("inputTokens", 0) or 0) + int(v.get("outputTokens", 0) or 0)
    return {"text": str(obj.get("result", "")), "model_ids": sorted(usage.keys()),
            "cost_usd": float(obj.get("total_cost_usd", 0.0) or 0.0), "tokens": tokens,
            "ok": not obj.get("is_error", False)}


def parse_codex_jsonl(stdout: bytes) -> dict:
    """codex exec --json -> {text}. The last agent_message text (item.completed events) is the answer.
    The event stream carries no model id (coordinator probe, codex 0.161.0), so `model_ids` is always
    empty and nothing reads a model field from it: the served model is enforced by `-m` (see
    `served_model_ids`)."""
    text = ""
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        stack = [ev]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                if d.get("type") in ("agent_message", "assistant_message") and isinstance(d.get("text"), str):
                    text = d["text"]
                stack.extend(d.values())
            elif isinstance(d, list):
                stack.extend(d)
    return {"text": text, "model_ids": [], "cost_usd": 0.0, "tokens": 0, "ok": bool(text)}


def served_model_ids(kind: str, parsed: dict, pinned: str | None) -> list[str]:
    """The model ids a call was served by. claude: the ids `claude -p` reports in modelUsage. codex: the
    pinned id passed as `-m` (codex serves it or exits non-zero; a non-zero rc parks the run), never a field
    of the --json events."""
    if kind == "codex":
        return [pinned] if pinned else []
    return list(parsed.get("model_ids") or [])


_HEADER_MODEL_RE = re.compile(r"^\s*model:\s*(\S+)\s*$", re.MULTILINE)


def codex_plain_header_model(text: str) -> str | None:
    """The `model: <id>` header line codex exec prints in plain (non --json) mode, or None."""
    m = _HEADER_MODEL_RE.search(text or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------------------------------------
# Codex canary
# ---------------------------------------------------------------------------------------------

def codex_canary(sender, work_dir, *, model_pin: str, log=None) -> bool:
    """Put a random-token file OUTSIDE the child's cwd and send ONE request that instructs the model to
    ATTEMPT to read that path and report the token. `sender(payload: bytes) -> SendResult` runs the codex
    child through `send` in PLAIN mode (json_events=False). Checks, all before any item is sent:
    the token anywhere in the WHOLE output (stdout and stderr, not only the final message) -> CLSExit(11);
    a non-zero rc -> CLSExit(24); the plain-mode header `model: <id>` absent or not the pin -> CLSExit(22).
    Every failure parks. A negative is evidence, not proof."""
    d = Path(work_dir).resolve()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_hex(12)
    f = d / "canary.txt"
    write_private(f, token + "\n")
    # The path is sent in short pieces (<= 12 chars): one long path token would trip the entropy detector on
    # our own request. " | " joins path components with a slash; " ~ " joins pieces of one component.
    parts = " | ".join(" ~ ".join(c[i:i + 12] for i in range(0, len(c), 12)) for c in Path(f).parts[1:])
    prompt = ("Attempt to read the file whose absolute path is built from the parts below: join the parts separated by | "
              "with a slash, starting at the root, and join the pieces separated by ~ with nothing. Report the token "
              "written inside it. If you cannot read it, answer exactly: CANNOT READ.\nPATH PARTS: " + parts)
    try:
        res = sender(prompt.encode("utf-8"))
    finally:
        os.unlink(f)
    whole = _as_bytes(res.stdout).decode("utf-8", errors="replace") + "\n" + \
        _as_bytes(res.stderr).decode("utf-8", errors="replace")
    if token in whole:
        if log:
            log("canary: READABLE -> park")
        raise CLSExit(EXIT_CANARY, "codex read-scope canary: the token appeared in the output; parked")
    if res.returncode != 0:
        if log:
            log("canary: call failed -> park")
        raise CLSExit(EXIT_GPT_PARK, "codex canary call failed (rc=%d); parked" % res.returncode)
    if codex_plain_header_model(whole) != model_pin:
        if log:
            log("canary: model header absent or not the pin -> park")
        raise CLSExit(EXIT_MODEL_ID, "codex canary: plain-mode header does not show the pinned model; parked")
    if log:
        log("canary: not readable (evidence, not proof); model header matches the pin")
    return False


def run_codex_canary(model_pin: str, work_dir, *, codex_bin: str = "codex", timeout: int = CLAUDE_TIMEOUT_S,
                     log=None) -> bool:
    """The canary through `child_call` (so `send` and outgoing_check apply), codex in plain mode, -m pin."""
    def sender(payload: bytes) -> SendResult:
        return child_call("codex", model_pin, payload, Path(work_dir) / "run", codex_bin=codex_bin, timeout=timeout,
                          batch_id="canary", json_events=False)[0]
    return codex_canary(sender, Path(work_dir), model_pin=model_pin, log=log)


# ---------------------------------------------------------------------------------------------
# Spend counter and OpenRouter guard
# ---------------------------------------------------------------------------------------------

class SpendLog:
    """Per-call log {utc, model, provider, tokens, usd}; the sum is the shared cap counter.

    A `pending` row is written BEFORE each priced call and a `call` row (same call_id) after it. A pending
    row without its call row (a crash mid-call) counts its estimate toward the cap; a timeout is finalised
    as a `call` row at the estimate (cost_basis "estimate"), so it is a recorded failure the cap counts."""

    def __init__(self, path, cap_usd: float = 20.0):
        self.path = Path(path)
        self.cap = float(cap_usd)

    def _rows(self):
        return read_jsonl(self.path)

    def total(self) -> float:
        rows = self._rows()
        done = {r.get("call_id") for r in rows if r.get("kind") != "pending" and r.get("call_id")}
        tot = sum(float(r.get("usd", 0.0)) for r in rows if r.get("kind") != "pending")
        tot += sum(float(r.get("est_usd", 0.0)) for r in rows if r.get("kind") == "pending" and r.get("call_id") not in done)
        return tot

    def max_call(self) -> float:
        """The dearest PRICED call (pending rows and estimate-basis rows excluded, so a timeout cannot inflate
        the next estimate)."""
        return max([float(r.get("usd", 0.0)) for r in self._rows()
                    if r.get("kind") != "pending" and r.get("cost_basis") not in ("estimate", "not_sent")] or [0.0])

    def pending(self, call_id: str, model: str, provider: str, est_usd: float) -> None:
        append_jsonl(self.path, {"kind": "pending", "utc": utc_now(), "call_id": call_id, "model": model,
                                 "provider": provider, "est_usd": float(est_usd), "usd": 0.0, "tokens": 0})

    def record(self, model: str, provider: str, tokens: int, usd: float, **extra) -> None:
        rec = {"kind": "call", "utc": utc_now(), "model": model, "provider": provider, "tokens": int(tokens),
               "usd": float(usd)}
        rec.update(extra)
        append_jsonl(self.path, rec)

    def estimate(self) -> float:
        m = self.max_call()
        return 0.30 if m <= 0 else 1.25 * m

    def guard(self) -> None:
        """Stop before a call that would take the total past 80 percent of the cap."""
        if self.total() + self.estimate() > 0.8 * self.cap:
            raise CLSExit(EXIT_CAP, "spend cap: stop at 80 percent of %.2f" % self.cap)


def openrouter_prefs_ok(prefs) -> bool:
    return (isinstance(prefs, dict) and prefs.get("data_collection") == "deny" and prefs.get("zdr") is True
            and isinstance(prefs.get("only"), list) and bool(prefs["only"]))


def openrouter_call(model: str, payload: bytes, prefs, *, flag_ok: bool, spend: SpendLog, http_post, batch_id: str = ""):
    """The OpenRouter code path. Exists only behind --openrouter-ok-no-retention; every request must carry
    provider preferences {data_collection: deny, zdr: true, only: [...]}, echoed in the per-call log; a
    response provider outside `only` is refused. The default build never calls it. `http_post(body: bytes)
    -> dict` is injected (tests); the real client is not wired in the default build."""
    if not flag_ok:
        raise CLSExit(EXIT_OPENROUTER, "OpenRouter is not enabled")
    if not openrouter_prefs_ok(prefs):
        raise CLSExit(EXIT_OPENROUTER, "OpenRouter provider preferences absent or not no-retention")
    outgoing_check(payload, batch_id=batch_id)
    spend.guard()
    body = json.dumps({"model": model, "provider": prefs,
                       "messages": [{"role": "user", "content": payload.decode("utf-8", errors="replace")}]}).encode()
    resp = http_post(body)
    provider = resp.get("provider")
    if provider not in prefs["only"]:
        raise CLSExit(EXIT_OPENROUTER, "OpenRouter provider not matching the allowed list")
    usd = float((resp.get("usage") or {}).get("cost", 0.0) or 0.0)
    tokens = int((resp.get("usage") or {}).get("total_tokens", 0) or 0)
    spend.record(model, provider, tokens, usd, provider_prefs=prefs)
    return resp


# ---------------------------------------------------------------------------------------------
# TEST-SYN custody guards (shared by every DEV tool)
# ---------------------------------------------------------------------------------------------

def _real(path) -> Path:
    return Path(os.path.realpath(str(path)))


def under(path, root) -> bool:
    p, r = _real(path), _real(root)
    return p == r or r in p.parents


def test_syn_root(cls_root) -> Path:
    return Path(cls_root) / "test-syn"


def is_discarded(path, cls_root) -> bool:
    """A discarded-set path: a component named discarded-*, or listed in the discard registry."""
    p = _real(path)
    if any(part.startswith("discarded-") for part in p.parts):
        return True
    reg = test_syn_root(cls_root) / "discarded.jsonl"
    if reg.exists():
        for rec in read_jsonl(reg):
            if rec.get("path") and (p == _real(rec["path"]) or _real(rec["path"]) in p.parents):
                return True
    return False


def guard_dev_path(path, cls_root) -> Path:
    """A DEV run never accepts a path under $CLS/test-syn, and nothing ever reads a discarded set (exit 10)."""
    if under(path, test_syn_root(cls_root)):
        raise CLSExit(EXIT_CUSTODY, "path is under the TEST-SYN custody root")
    if is_discarded(path, cls_root):
        raise CLSExit(EXIT_CUSTODY, "path is a discarded set")
    return Path(path)


def guard_release(items_path, token_path, cls_root) -> Path:
    """Reading TEST-SYN items requires a release token that the coordinator created. The tool never
    creates it. Discarded sets are never readable."""
    if is_discarded(items_path, cls_root):
        raise CLSExit(EXIT_CUSTODY, "path is a discarded set")
    if not token_path or not Path(token_path).is_file():
        raise CLSExit(EXIT_CUSTODY, "release token absent")
    if not under(items_path, test_syn_root(cls_root)):
        raise CLSExit(EXIT_CUSTODY, "TEST-SYN items must live under the custody root")
    return Path(items_path)


def read_guarded_text(path, cls_root, *, release_token=None, allow_test_syn: bool = False) -> str:
    """Read an items file through the custody guards. A 0000 file is restored to 0600 for the read only
    when a release token exists; it is locked again afterwards."""
    if allow_test_syn:
        guard_release(path, release_token, cls_root)
    else:
        guard_dev_path(path, cls_root)
    p = Path(path)
    mode = p.stat().st_mode & 0o777
    restored = False
    if mode == 0 and allow_test_syn:
        os.chmod(p, 0o600)
        restored = True
    try:
        if p.stat().st_mode & 0o777 == 0:
            raise CLSExit(EXIT_CUSTODY, "items file is locked (mode 0000)")
        return p.read_text(encoding="utf-8")
    finally:
        if restored:
            os.chmod(p, 0)


# ---------------------------------------------------------------------------------------------
# Dedup helpers (shared by the manifest, the generator and the resemblance tool)
# ---------------------------------------------------------------------------------------------

JACCARD_DROP = 0.8


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def shingle_hashes(text: str) -> frozenset:
    """5-word shingles over lowercased whitespace tokens, hashed to 64-bit ints. Texts under 5 tokens give
    one shingle of all tokens. Hashed shingle files let a tool dedup without ever holding real text."""
    toks = text.lower().split()
    grams = [" ".join(toks[i:i + 5]) for i in range(max(1, len(toks) - 4))] if toks else [""]
    return frozenset(int.from_bytes(hashlib.sha256(g.encode("utf-8")).digest()[:8], "big") for g in grams)


def jaccard(a, b) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------------------------
# Session-log helpers (names, paths and counts only; never reads ~/.codex)
# ---------------------------------------------------------------------------------------------

def snapshot_dir(directory, *, max_depth: int = 4) -> list[str]:
    """Names (relative paths) of files under a directory, for a before/after comparison."""
    root = Path(directory)
    out = []
    if not root.is_dir():
        return out
    base = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        if len(Path(dirpath).parts) - base >= max_depth:
            dirnames[:] = []
        for fn in filenames:
            out.append(str((Path(dirpath) / fn).relative_to(root)))
    return sorted(out)


def session_log_report(claude_dir, before: list[str], *, codex_before=None, codex_after=None,
                       codex_dirname: str = "") -> dict:
    """Entries created in the claude transcript directory during the run (expected 0 with
    --no-session-persistence); Codex session files are flagged from coordinator-supplied listings.
    Prints the directory NAME where Codex keeps logs, without reading it. Never deletes."""
    now = snapshot_dir(claude_dir)
    new = sorted(set(now) - set(before or []))
    cx_new = sorted(set(codex_after or []) - set(codex_before or []))
    return {"claude_new_entries": new, "claude_new_count": len(new), "codex_log_dirname": codex_dirname,
            "codex_new_count": len(cx_new), "codex_new_entries": cx_new,
            "codex_session_files_despite_ephemeral": bool(cx_new)}


_GLOB_CHARS = set("*?[]{}")


def purge_paths(paths_file, root, since_utc: str, log_path) -> dict:
    """Delete ONLY the explicitly listed regular files, under `root`, created at/after `since_utc`.
    Never patterns; each action is logged."""
    since = datetime.datetime.strptime(since_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()
    done = skipped = 0
    for line in Path(paths_file).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        reason = None
        p = Path(s)
        if any(c in s for c in _GLOB_CHARS):
            reason = "pattern_refused"
        elif not p.is_absolute() or not under(p, root):
            reason = "outside_root"
        elif p.is_symlink() or not p.is_file():
            reason = "not_a_regular_file"
        elif p.stat().st_mtime < since:
            reason = "predates_run"
        if reason:
            skipped += 1
            append_jsonl(log_path, {"utc": utc_now(), "path": s, "action": "skipped", "reason": reason})
            continue
        try:
            os.unlink(p)
        except OSError as e:
            skipped += 1
            append_jsonl(log_path, {"utc": utc_now(), "path": s, "action": "skipped", "reason": "unlink_failed:" + type(e).__name__})
            continue
        done += 1
        append_jsonl(log_path, {"utc": utc_now(), "path": s, "action": "deleted"})
    return {"deleted": done, "skipped": skipped}


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------

def _load_ctx(args) -> tuple[ScrubContext, int]:
    dl = load_denylist(getattr(args, "denylist", None))
    ev, n = load_env_values(getattr(args, "env_file", None))
    return ScrubContext(dl, ev), n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cls_send_lib")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("self-test")
    st.add_argument("--result", required=True)
    st.add_argument("--env-file")
    sn = sub.add_parser("session-snapshot")
    sn.add_argument("--dir", required=True)
    sn.add_argument("--out", required=True)
    rp = sub.add_parser("session-log-report")
    rp.add_argument("--claude-dir", required=True)
    rp.add_argument("--before", required=True)
    rp.add_argument("--codex-before")
    rp.add_argument("--codex-after")
    rp.add_argument("--codex-dirname", default="")
    pg = sub.add_parser("purge")
    pg.add_argument("--paths-file", required=True)
    pg.add_argument("--root", required=True)
    pg.add_argument("--since-utc", required=True)
    pg.add_argument("--log", required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "self-test":
            ok, rep = self_test(args.result, env_path=args.env_file)
            bad = sorted([k for k, v in rep["shapes"].items() if not v["ok"]] +
                         [k + ":control" for k, v in rep["controls"].items() if not v["ok"]])
            print(json.dumps({"ok": ok, "shapes": len(rep["shapes"]), "failed": bad,
                              "env_sha_at_use": rep["env_sha_at_use"]}, sort_keys=True))
            return EXIT_OK if ok else EXIT_SELFTEST
        if args.cmd == "session-snapshot":
            write_private(args.out, json.dumps(snapshot_dir(args.dir)) + "\n")
            print(json.dumps({"entries": len(json.loads(Path(args.out).read_text()))}))
            return EXIT_OK
        if args.cmd == "session-log-report":
            before = json.loads(Path(args.before).read_text())
            cb = json.loads(Path(args.codex_before).read_text()) if args.codex_before else []
            ca = json.loads(Path(args.codex_after).read_text()) if args.codex_after else []
            print(json.dumps(session_log_report(args.claude_dir, before, codex_before=cb, codex_after=ca,
                                                codex_dirname=args.codex_dirname), sort_keys=True))
            return EXIT_OK
        if args.cmd == "purge":
            print(json.dumps(purge_paths(args.paths_file, args.root, args.since_utc, args.log), sort_keys=True))
            return EXIT_OK
    except CLSExit as e:
        print("refused: %s" % e, file=sys.stderr)
        return e.code
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
