"""P1.8-c: Claude credentials are read in ONE module (`claude_creds.read_oauth`).

Every test uses a scratch HOME and a fake keychain runner; none touches the real
keychain or `~/.claude`. The token strings are fixtures, not credentials.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_router import claude_creds

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "fixture-token-1"
NESTED = {"claudeAiOauth": {"accessToken": TOKEN, "expiresAt": 1900000000000,
                            "subscriptionType": "max"}}
FLAT = {"accessToken": TOKEN, "expiresAt": 1900000000000, "subscriptionType": "max"}


def _runner(stdout: str = "", returncode: int = 0, calls: list | None = None):
    def run(argv, **kw):
        if calls is not None:
            calls.append((argv, kw))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
    return run


def _no_run(argv, **kw):  # pragma: no cover - failing is the point
    raise AssertionError(f"keychain must not be queried: {argv}")


def _home(tmp_path: Path, payload=None, raw: str | None = None) -> Path:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    if payload is not None:
        raw = json.dumps(payload)
    if raw is not None:
        (home / ".claude" / ".credentials.json").write_text(raw)
    return home


# -- read_oauth ---------------------------------------------------------------

@pytest.mark.parametrize("payload", [NESTED, FLAT])
def test_keychain_both_shapes(tmp_path, payload):
    calls: list = []
    got = claude_creds.read_oauth(platform="darwin", run=_runner(json.dumps(payload) + "\n", calls=calls),
                                  home=_home(tmp_path))
    assert got["accessToken"] == TOKEN and got["subscriptionType"] == "max"
    argv, kw = calls[0]
    assert argv == ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"]
    assert kw["timeout"] == 4


@pytest.mark.parametrize("payload", [NESTED, FLAT])
@pytest.mark.parametrize("platform", ["linux", "win32", "darwin"])
def test_file_both_shapes(tmp_path, payload, platform):
    run = _runner("", returncode=44) if platform == "darwin" else _no_run
    got = claude_creds.read_oauth(platform=platform, run=run, home=_home(tmp_path, payload))
    assert got is not None and got["accessToken"] == TOKEN and got["expiresAt"] == 1900000000000


def test_keychain_wins_over_file(tmp_path):
    other = {"claudeAiOauth": {"accessToken": "file-token"}}
    got = claude_creds.read_oauth(platform="darwin", run=_runner(json.dumps(NESTED)),
                                  home=_home(tmp_path, other))
    assert got["accessToken"] == TOKEN


@pytest.mark.parametrize("stdout,rc", [("", 0), ("not json", 0), ("[1]", 0), ("x", 44)])
def test_bad_keychain_falls_through_to_file(tmp_path, stdout, rc):
    got = claude_creds.read_oauth(platform="darwin", run=_runner(stdout, rc), home=_home(tmp_path, FLAT))
    assert got["accessToken"] == TOKEN


def test_keychain_timeout_or_missing_binary_falls_through(tmp_path):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 4)
    assert claude_creds.read_oauth(platform="darwin", run=boom, home=_home(tmp_path, FLAT))["accessToken"] == TOKEN
    def gone(argv, **kw):
        raise FileNotFoundError("security")
    assert claude_creds.read_oauth(platform="darwin", run=gone, home=_home(tmp_path, FLAT))["accessToken"] == TOKEN


def test_missing_file_is_none(tmp_path):
    assert claude_creds.read_oauth(platform="linux", run=_no_run, home=tmp_path / "nohome") is None
    assert claude_creds.read_oauth(platform="darwin", run=_runner("", 44), home=_home(tmp_path)) is None


@pytest.mark.parametrize("raw", ["{not json", "", "[]", '"str"'])
def test_malformed_file_is_none(tmp_path, raw):
    assert claude_creds.read_oauth(platform="linux", run=_no_run, home=_home(tmp_path, raw=raw)) is None


def test_credentials_without_token_return_a_dict_without_token(tmp_path):
    """usage-refresh tells 'no credentials' (None) from 'no token' ({})."""
    got = claude_creds.read_oauth(platform="linux", run=_no_run, home=_home(tmp_path, {"claudeAiOauth": {}}))
    assert got == {} and "accessToken" not in got
    assert claude_creds.read_oauth(platform="linux", run=_no_run, home=_home(tmp_path, {"other": 1})) == {}


def test_env_and_timeout_are_forwarded(tmp_path):
    calls: list = []
    claude_creds.read_oauth(platform="darwin", run=_runner(json.dumps(FLAT), calls=calls),
                            home=_home(tmp_path), timeout=10, env={"PATH": "/x"})
    assert calls[0][1]["timeout"] == 10 and calls[0][1]["env"] == {"PATH": "/x"}


def test_never_prints_or_logs(tmp_path, capsys, caplog):
    claude_creds.read_oauth(platform="darwin", run=_runner(json.dumps(NESTED)), home=_home(tmp_path))
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err + caplog.text


# -- the one-module rule ------------------------------------------------------

def test_no_find_generic_password_outside_claude_creds():
    proc = subprocess.run(["git", "grep", "-n", "find-generic-password", "--", "src", "hooks", ".claude"],
                          cwd=ROOT, capture_output=True, text=True)
    if proc.returncode not in (0, 1):
        pytest.skip(f"git grep unavailable: {proc.stderr.strip()}")
    hits = [ln for ln in proc.stdout.splitlines() if not ln.startswith("src/llm_router/claude_creds.py:")]
    # the census must see claude_creds.py itself, otherwise it checked nothing
    assert any(ln.startswith("src/llm_router/claude_creds.py:") for ln in proc.stdout.splitlines())
    assert hits == []


# -- every replaced caller, on the same fixtures ------------------------------

def _load(rel: str):
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location("cc_" + path.stem.replace("-", "_") + str(abs(hash(rel))), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _legacy_token(stdout: str) -> str:
    """The parse every pre-P1.8-c caller did on the keychain output."""
    return json.loads(stdout.strip()).get("claudeAiOauth", {}).get("accessToken", "")


class _Seen(Exception):
    pass


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(state))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    seen: list[str] = []

    def urlopen(req, *a, **k):
        seen.append(req.get_header("Authorization"))
        raise _Seen

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return SimpleNamespace(home=home, seen=seen, monkeypatch=monkeypatch)


def _call(kind: str):
    """(module-relative path, callable) for each replaced reader."""
    if kind == "subscription":
        from llm_router.tools import subscription
        return lambda: asyncio.run(subscription.llm_refresh_claude_usage())
    rel, fn = {
        "auto-route": ("src/llm_router/hooks/auto-route.py", "_fetch_usage_inline"),
        "auto-route-top": ("hooks/auto-route.py", "_fetch_usage_inline"),
        "auto-route-claude": (".claude/hooks/auto-route.py", "_fetch_usage_inline"),
        "session-end": ("src/llm_router/hooks/session-end.py", "_fetch_live_usage"),
        "session-start": ("src/llm_router/hooks/session-start.py", "_refresh_claude_usage_attempt"),
        "usage-refresh": ("src/llm_router/hooks/usage-refresh.py", "_oauth_refresh_and_write"),
    }[kind]
    return getattr(_load(rel), fn)


KINDS = ["subscription", "auto-route", "auto-route-top", "auto-route-claude",
         "session-end", "session-start", "usage-refresh"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("payload", [NESTED, FLAT])
def test_caller_gets_the_token_from_keychain(scratch, kind, payload):
    out = json.dumps(payload)
    if payload is NESTED:
        assert _legacy_token(out) == TOKEN  # same answer the old inline parse gave
    scratch.monkeypatch.setattr(sys, "platform", "darwin")
    scratch.monkeypatch.setattr(subprocess, "run", _runner(out))
    _call(kind)()
    assert scratch.seen == [f"Bearer {TOKEN}"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("payload", [NESTED, FLAT])
def test_caller_gets_the_token_from_file_on_linux(scratch, kind, payload):
    (scratch.home / ".claude" / ".credentials.json").write_text(json.dumps(payload))
    scratch.monkeypatch.setattr(sys, "platform", "linux")
    scratch.monkeypatch.setattr(subprocess, "run", _no_run)
    _call(kind)()
    assert scratch.seen == [f"Bearer {TOKEN}"]


@pytest.mark.parametrize("kind", KINDS)
def test_caller_without_credentials_makes_no_request(scratch, kind):
    scratch.monkeypatch.setattr(sys, "platform", "linux")
    scratch.monkeypatch.setattr(subprocess, "run", _no_run)
    _call(kind)()
    assert scratch.seen == []


def test_usage_refresh_still_tells_no_credentials_from_no_token(scratch):
    mod = _load("src/llm_router/hooks/usage-refresh.py")
    errors: list = []
    scratch.monkeypatch.setattr(mod, "_record_refresh_error", lambda kind, *a, **k: errors.append(kind))
    scratch.monkeypatch.setattr(sys, "platform", "linux")
    mod._oauth_refresh_and_write()
    (scratch.home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {}}))
    mod._oauth_refresh_and_write()
    assert errors == ["no-credentials", "no-token"]


def test_hook_falls_back_to_the_path_loaded_module(scratch):
    """Without an importable llm_router the hook loads the file the installer drops."""
    mod = _load("src/llm_router/hooks/session-end.py")
    scratch.monkeypatch.setitem(sys.modules, "llm_router", None)  # import raises ImportError
    scratch.monkeypatch.setitem(sys.modules, "llm_router.claude_creds", None)
    scratch.monkeypatch.setattr(sys, "platform", "linux")
    (scratch.home / ".claude" / ".credentials.json").write_text(json.dumps(FLAT))
    assert mod._read_claude_oauth(4)["accessToken"] == TOKEN


def test_installer_ships_claude_creds_next_to_the_hooks():
    from llm_router import install_hooks
    assert ("claude_creds.py", "llm_router_claude_creds.py") in install_hooks._HOOK_SUPPORT_FILES
    assert (ROOT / "src" / "llm_router" / "claude_creds.py").exists()


def test_hook_mirrors_are_byte_identical():
    for name in ("auto-route", "session-end", "session-start", "usage-refresh"):
        assert (ROOT / "hooks" / f"{name}.py").read_bytes() == \
               (ROOT / "src/llm_router/hooks" / f"{name}.py").read_bytes()
