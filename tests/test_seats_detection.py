"""Seat and host detection: which subscriptions this machine is logged in to.

Every probe is injected -- no real CLI, no network, no real $HOME -- so the
suite runs the same on a dev laptop and in CI.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from llm_router import seats as S
from llm_router.host_detect import detect_hosts, present_hosts

NOW = 1_790_000_000.0  # fixed clock


# ── helpers ─────────────────────────────────────────────────────────────────

def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.sig"


def _codex_auth(home: Path, plan: str = "plus", until: str | None = None) -> None:
    auth = {"https://api.openai.com/auth": {"chatgpt_plan_type": plan}}
    if until:
        auth["https://api.openai.com/auth"]["chatgpt_subscription_active_until"] = until
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "auth.json").write_text(json.dumps({
        "tokens": {"id_token": _jwt(auth), "access_token": "x", "refresh_token": "y"},
    }))


def _runner(table: dict[str, tuple[int, str] | None]):
    """Fake CLI runner keyed by the first two argv words."""
    def run(argv, timeout):
        return table.get(" ".join(argv[:2]))
    return run


class _Resp:
    def __init__(self, payload: dict):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(models: list[str] | None):
    def open_(req, timeout):
        if models is None:
            raise OSError("connection refused")
        return _Resp({"models": [{"name": m} for m in models]})
    return open_


def _detect(tmp_path, *, runner=None, env=None, models=None, which=lambda b: None, now=NOW):
    return S.detect_seats(
        runner=runner or _runner({}),
        env=env or {},
        home=tmp_path,
        which=which,
        opener=_opener(models),
        now=now,
    )


CLAUDE_MAX = (0, json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}))
CLAUDE_CONSOLE = (0, json.dumps({"loggedIn": True, "authMethod": "console", "apiProvider": "firstParty"}))
CLAUDE_OUT = (0, json.dumps({"loggedIn": False}))
CODEX_CHATGPT = (0, "Logged in using ChatGPT\n")
CODEX_KEY = (0, "Logged in using an API key\n")
CODEX_OUT = (1, "Not logged in\n")


# ── claude ──────────────────────────────────────────────────────────────────

def test_claude_max_seat_from_auth_status(tmp_path):
    seats = _detect(tmp_path, runner=_runner({"claude auth": CLAUDE_MAX}))
    assert seats.claude == S.Seat(kind="claude.ai", plan="max")
    assert seats.claude.present


def test_claude_console_login_is_an_api_key_not_a_seat(tmp_path):
    seats = _detect(tmp_path, runner=_runner({"claude auth": CLAUDE_CONSOLE}))
    assert seats.claude.kind == "api-key"
    assert not seats.claude.present


def test_claude_logged_out_falls_back_to_env_key(tmp_path):
    seats = _detect(tmp_path, runner=_runner({"claude auth": CLAUDE_OUT}), env={"ANTHROPIC_API_KEY": "k"})
    assert seats.claude.kind == "api-key"


def test_claude_cli_missing_and_no_key_is_no_seat(tmp_path):
    seats = _detect(tmp_path)
    assert seats.claude == S.Seat()


def test_claude_status_with_leading_noise_still_parses(tmp_path):
    noisy = (0, "warning: something\n" + CLAUDE_MAX[1])
    seats = _detect(tmp_path, runner=_runner({"claude auth": noisy}))
    assert seats.claude.plan == "max"


# ── codex ───────────────────────────────────────────────────────────────────

def test_codex_chatgpt_seat_with_plan_from_token(tmp_path):
    _codex_auth(tmp_path, plan="pro", until="2099-01-01T00:00:00+00:00")
    seats = _detect(tmp_path, runner=_runner({"codex login": CODEX_CHATGPT}))
    assert seats.codex == S.Seat(kind="chatgpt", plan="pro", plan_stale=False)


def test_codex_plan_claim_past_its_window_is_marked_stale_but_still_a_seat(tmp_path):
    """Observed in the wild: the id_token said the plan ended weeks ago while
    `codex exec` kept working. Login status is the fact, the claim a hint."""
    _codex_auth(tmp_path, plan="plus", until="2020-01-01T00:00:00+00:00")
    seats = _detect(tmp_path, runner=_runner({"codex login": CODEX_CHATGPT}))
    assert seats.codex.kind == "chatgpt"
    assert seats.codex.plan == "plus"
    assert seats.codex.plan_stale is True
    assert seats.codex.present
    assert "codex" in seats.free_bucket()
    assert "stale" in seats.codex.label()


def test_codex_chatgpt_without_auth_file_has_no_plan(tmp_path):
    seats = _detect(tmp_path, runner=_runner({"codex login": CODEX_CHATGPT}))
    assert seats.codex == S.Seat(kind="chatgpt", plan=None)


def test_codex_garbage_auth_file_does_not_raise(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{not json")
    seats = _detect(tmp_path, runner=_runner({"codex login": CODEX_CHATGPT}))
    assert seats.codex.kind == "chatgpt" and seats.codex.plan is None


def test_codex_api_key_login_is_not_a_seat(tmp_path):
    seats = _detect(tmp_path, runner=_runner({"codex login": CODEX_KEY}))
    assert seats.codex.kind == "api-key"
    assert "codex" not in seats.free_bucket()


def test_codex_logged_out_then_env_key(tmp_path):
    seats = _detect(tmp_path, runner=_runner({"codex login": CODEX_OUT}), env={"OPENAI_API_KEY": "k"})
    assert seats.codex.kind == "api-key"


def test_codex_cli_hang_returns_none_and_is_no_seat(tmp_path):
    seats = _detect(tmp_path, runner=_runner({"codex login": None}))
    assert seats.codex == S.Seat()


# ── gemini / ollama / keys ──────────────────────────────────────────────────

def test_gemini_google_seat_needs_binary_and_creds(tmp_path):
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "oauth_creds.json").write_text("{}")
    assert _detect(tmp_path, which=lambda b: "/usr/bin/gemini" if b == "gemini" else None).gemini.kind == "google"
    assert _detect(tmp_path).gemini.kind is None  # no binary


def test_gemini_env_key_without_login(tmp_path):
    assert _detect(tmp_path, env={"GEMINI_API_KEY": "k"}).gemini.kind == "api-key"


def test_ollama_seat_lists_models(tmp_path):
    seats = _detect(tmp_path, models=["qwen3-coder:30b", "lfm2.5:8b"])
    assert seats.ollama.kind == "local"
    assert seats.ollama.models == ("qwen3-coder:30b", "lfm2.5:8b")
    assert seats.ollama.label() == "local(2 models)"


def test_ollama_down_is_no_seat(tmp_path):
    assert _detect(tmp_path, models=None).ollama == S.Seat()


def test_api_keys_are_booleans_never_values(tmp_path):
    seats = _detect(tmp_path, env={"OPENAI_API_KEY": "sk-secret", "GROQ_API_KEY": ""})
    assert seats.api_keys["OPENAI_API_KEY"] is True
    assert seats.api_keys["GROQ_API_KEY"] is False
    assert "sk-secret" not in json.dumps(seats.to_dict())


# ── derived routing facts ───────────────────────────────────────────────────

def test_free_bucket_is_local_plus_every_seat(tmp_path):
    _codex_auth(tmp_path)
    seats = _detect(
        tmp_path,
        runner=_runner({"claude auth": CLAUDE_MAX, "codex login": CODEX_CHATGPT}),
        models=["m"],
    )
    assert seats.free_bucket() == frozenset({"ollama", "codex", "claude"})
    assert seats.subscription_provider() == "claude"


def test_no_seats_means_empty_bucket_and_no_subscription_default(tmp_path):
    seats = _detect(tmp_path, env={"OPENAI_API_KEY": "k"})
    assert seats.free_bucket() == frozenset()
    assert seats.subscription_provider() is None


def test_summary_line_names_every_seat(tmp_path):
    seats = _detect(tmp_path, runner=_runner({"claude auth": CLAUDE_MAX}), models=["m"])
    assert seats.summary_line() == "claude=claude.ai(max) · codex=none · gemini=none · ollama=local(1 models)"


# ── persistence ─────────────────────────────────────────────────────────────

def test_save_load_roundtrip_and_no_secrets_on_disk(tmp_path):
    _codex_auth(tmp_path, plan="team", until="2099-01-01T00:00:00+00:00")
    seats = _detect(
        tmp_path,
        runner=_runner({"claude auth": CLAUDE_MAX, "codex login": CODEX_CHATGPT}),
        env={"OPENAI_API_KEY": "sk-secret"},
        models=["m"],
    )
    path = S.save_seats(seats, tmp_path)
    assert path == tmp_path / ".llm-router" / "seats.json"
    text = path.read_text()
    assert "sk-secret" not in text and "id_token" not in text and "eyJ" not in text
    assert S.load_seats(tmp_path) == seats


def test_load_missing_or_corrupt_returns_none(tmp_path):
    assert S.load_seats(tmp_path) is None
    S.seats_path(tmp_path).parent.mkdir(parents=True)
    S.seats_path(tmp_path).write_text("nope")
    assert S.load_seats(tmp_path) is None


def test_staleness_is_24h(tmp_path):
    seats = _detect(tmp_path, now=NOW)
    assert not seats.is_stale(now=NOW + 3600)
    assert seats.is_stale(now=NOW + S.STALE_AFTER_SECONDS + 1)
    assert S.Seats().is_stale()  # never detected


def test_refresh_detects_and_persists(tmp_path):
    seats = S.refresh_seats(
        home=tmp_path, runner=_runner({"claude auth": CLAUDE_MAX}), env={},
        which=lambda b: None, opener=_opener(None), now=NOW,
    )
    assert seats.claude.plan == "max"
    assert S.load_seats(tmp_path) == seats


# ── default runner ──────────────────────────────────────────────────────────

def test_default_runner_returns_none_for_missing_binary():
    assert S._default_runner(["definitely-not-a-real-binary-xyz", "x"], 1.0) is None


def test_default_runner_captures_output():
    code, out = S._default_runner(["python3", "-c", "print('hi')"], 10.0)
    assert code == 0 and "hi" in out


# ── hosts ───────────────────────────────────────────────────────────────────

def test_host_present_by_binary_or_config_dir(tmp_path):
    (tmp_path / ".codex").mkdir()
    hosts = detect_hosts(home=tmp_path, which=lambda b: "/usr/local/bin/claude" if b == "claude" else None)
    assert hosts["claude-code"].present and hosts["claude-code"].binary == "/usr/local/bin/claude"
    assert hosts["claude-code"].config_dir is None
    assert hosts["codex"].present and hosts["codex"].binary is None
    assert hosts["codex"].config_dir == str(tmp_path / ".codex")
    assert not hosts["gemini-cli"].present
    assert present_hosts(home=tmp_path, which=lambda b: None) == ["codex"]


def test_host_config_file_is_not_a_config_dir(tmp_path):
    (tmp_path / ".gemini").write_text("")  # a file, not a dir
    assert not detect_hosts(home=tmp_path, which=lambda b: None)["gemini-cli"].present


def test_host_info_to_dict_carries_present(tmp_path):
    d = detect_hosts(home=tmp_path, which=lambda b: None)["codex"].to_dict()
    assert d == {"host": "codex", "binary": None, "config_dir": None, "present": False}


@pytest.mark.parametrize("age", [0, 100])
def test_age_seconds_uses_detected_at(tmp_path, age):
    seats = _detect(tmp_path, now=NOW)
    assert seats.age_seconds(now=NOW + age) == pytest.approx(age)
    assert S.Seats().age_seconds() is None


def test_real_time_default_is_now(tmp_path):
    before = time.time()
    seats = S.detect_seats(runner=_runner({}), env={}, home=tmp_path, which=lambda b: None, opener=_opener(None))
    assert 0 <= (seats.age_seconds() or 0) <= max(5.0, time.time() - before + 1)
