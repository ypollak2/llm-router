"""P1.8 map-discovery: seat status/reason/plan, key discovery, keychain backend. Fixtures only."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from llm_router import claude_creds, resource_map as rm, secrets_vault
from llm_router import seats as S
from llm_router.discovery import keys as K

CODEX_ERR = "Error loading configuration: /Users/x/.codex/config.toml:443:14: invalid transport"


def runner_for(table):
    """table: argv[0:2] tuple/str -> (rc, out) or None."""
    def run(argv, timeout):
        for k, v in table.items():
            if tuple(argv[: len(k)]) == k:
                return v
        return None
    return run


def detect(tmp_path, table, *, env=None, which=("gemini", "gh", "ollama"), ollama_up=False):
    def opener(req, timeout):
        if not ollama_up:
            raise OSError("down")
        return SimpleNamespace(read=lambda: b'{"models":[{"name":"a:1"}]}',
                               __enter__=lambda s: s, __exit__=lambda *a: None)
    class Op:
        def __call__(self, req, timeout):
            if not ollama_up:
                raise OSError("down")
            class R:
                def __enter__(s): return s
                def __exit__(s, *a): return False
                def read(s): return b'{"models":[{"name":"a:1"}]}'
            return R()
    return S.detect_seats(runner=runner_for(table), env=env or {}, home=tmp_path,
                          which=lambda n: f"/bin/{n}" if n in which else None,
                          opener=Op(), now=1_790_000_000.0)


# ── seats ───────────────────────────────────────────────────────────────────

def test_codex_failure_reason_is_first_stderr_line(tmp_path):
    seats = detect(tmp_path, {("codex", "login"): (1, CODEX_ERR + "\nsecond line\n")})
    assert seats.codex.status == "installed_not_connected"
    assert seats.codex.reason == CODEX_ERR


def test_codex_missing_and_connected(tmp_path):
    assert detect(tmp_path, {}).codex.status == "absent"
    ok = detect(tmp_path, {("codex", "login"): (0, "Logged in using ChatGPT\n")})
    assert ok.codex.status == "connected" and ok.codex.kind == "chatgpt"


def test_claude_statuses(tmp_path):
    out = detect(tmp_path, {("claude", "auth"): (1, '{"loggedIn": false}')}).claude
    assert out.status == "installed_not_connected" and out.reason
    assert detect(tmp_path, {}).claude.status == "absent"
    ok = detect(tmp_path, {("claude", "auth"): (0, json.dumps(
        {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}))}).claude
    assert ok.status == "connected" and ok.plan == "max"


def test_gemini_plan_from_settings_and_unknown(tmp_path):
    g = tmp_path / ".gemini"
    g.mkdir()
    (g / "oauth_creds.json").write_text("{}")
    unk = detect(tmp_path, {}).gemini
    assert unk.status == "connected" and unk.plan == "unknown" and "settings" in unk.reason
    (g / "settings.json").write_text(json.dumps({"security": {"auth": {"selectedType": "oauth-personal"}}}))
    assert detect(tmp_path, {}).gemini.plan == "google-account"
    (g / "settings.json").write_text(json.dumps({"selectedAuthType": "weird"}))
    s = detect(tmp_path, {}).gemini
    assert s.plan == "unknown" and "weird" in s.reason


def test_gemini_binary_without_creds_and_absent(tmp_path):
    assert detect(tmp_path, {}).gemini.status == "installed_not_connected"
    assert detect(tmp_path, {}, which=()).gemini.status == "absent"


def test_copilot_states(tmp_path):
    ok_cli = {("gh", "copilot"): (0, "gh copilot 1.0")}
    assert detect(tmp_path, {}, which=()).copilot.status == "absent"
    nc = detect(tmp_path, ok_cli).copilot
    assert nc.status == "installed_not_connected" and "hosts.json" in nc.reason
    h = tmp_path / ".config" / "github-copilot"
    h.mkdir(parents=True)
    (h / "hosts.json").write_text("{}")
    c = detect(tmp_path, ok_cli).copilot
    assert c.status == "connected" and c.kind == "github-copilot"
    # extension dir counts as installed
    (h / "hosts.json").unlink()
    ext = tmp_path / ".vscode" / "extensions" / "github.copilot-1.2.3"
    ext.mkdir(parents=True)
    assert detect(tmp_path, {}, which=()).copilot.status == "installed_not_connected"


def test_ollama_states(tmp_path):
    assert detect(tmp_path, {}, which=()).ollama.status == "absent"
    d = detect(tmp_path, {}).ollama
    assert d.status == "installed_not_connected" and "not reachable" in d.reason
    up = detect(tmp_path, {}, ollama_up=True).ollama
    assert up.status == "connected" and up.models == ("a:1",)


def test_old_seats_json_migrates(tmp_path):
    old = {"claude": {"kind": "claude.ai", "plan": "max", "plan_stale": False, "models": []},
           "codex": {"kind": None}, "gemini": {}, "ollama": {}, "api_keys": {}, "detected_at": "x"}
    p = tmp_path / ".llm-router"
    p.mkdir()
    (p / "seats.json").write_text(json.dumps(old))
    seats = S.load_seats(tmp_path)
    assert seats.claude.status == "connected" and seats.codex.status == "absent"
    assert seats.copilot.status == "absent"
    S.save_seats(seats, tmp_path)
    assert "status" in json.loads((p / "seats.json").read_text())["claude"]


# ── keys ────────────────────────────────────────────────────────────────────

def test_key_sources_and_no_values(tmp_path):
    envf = tmp_path / "proj.env"
    envf.write_text("# c\nexport GROQ_API_KEY='gsk-SECRET-A'\nDEEPSEEK_API_KEY=\nNOT_A_KEY=1\n")
    env = {"OPENAI_API_KEY": "sk-SECRET-B", "GROQ_API_KEY": "gsk-SECRET-A", "ANTHROPIC_API_KEY": "sk-ant-SECRET-C"}
    infos = K.discover(env=env, dotenv_paths=[envf, tmp_path / "missing.env"],
                       keychain=lambda v: v == "MOONSHOT_API_KEY")
    by = {i.var: i for i in infos}
    assert by["OPENAI_API_KEY"].source == "env"
    assert by["GROQ_API_KEY"].source == f"dotenv:{envf}"
    assert by["MOONSHOT_API_KEY"].source == "keychain"
    assert "DEEPSEEK_API_KEY" not in by              # empty value is not a key
    for i in infos:
        assert i.provider and i.usable_by and i.billing in ("paid", "free_tier", "unknown")
    blob = json.dumps([i.to_dict() for i in infos])
    for secret in ("SECRET-A", "SECRET-B", "SECRET-C"):
        assert secret not in blob
    assert by["ANTHROPIC_API_KEY"].billing == "paid" and by["GROQ_API_KEY"].billing == "unknown"


def test_real_env_differing_from_dotenv_is_env(tmp_path):
    f = tmp_path / ".env"
    f.write_text("OPENAI_API_KEY=file-value\n")
    i = K.discover(env={"OPENAI_API_KEY": "shell-value"}, dotenv_paths=[f], keychain=lambda v: False)
    assert i[0].source == "env"


def test_keychain_probe_exit_code_only():
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="SECRETPRINTED")
    assert K.keychain_has("OPENAI_API_KEY", platform="darwin", run=run)
    assert calls[0] == ["security", "find-generic-password", "-s", "llm-router-OPENAI_API_KEY"]
    assert K.keychain_has("OPENAI_API_KEY", platform="linux", run=run)
    assert calls[1][0] == "secret-tool"
    assert not K.keychain_has("OPENAI_API_KEY", platform="win32", run=run)
    assert not K.keychain_has("X", platform="darwin", run=lambda a, **k: SimpleNamespace(returncode=44, stdout=""))


def test_keychain_backend_registered_and_falls_back(monkeypatch):
    assert "keychain" in secrets_vault._BACKENDS
    got = SimpleNamespace(returncode=0, stdout="kc-secret\n")
    v = secrets_vault.KeychainSecretsVault(run=lambda a, **k: got, platform="darwin")
    assert v.get_provider_key("openai") == "kc-secret"
    miss = SimpleNamespace(returncode=44, stdout="")
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    v2 = secrets_vault.KeychainSecretsVault(run=lambda a, **k: miss, platform="darwin")
    assert v2.get_provider_key("openai") == "from-env"
    assert v2.get_provider_key("unknown-provider") is None


def test_keychain_query_never_returns_secret_unless_asked():
    r = SimpleNamespace(returncode=0, stdout="s3cret")
    assert claude_creds.keychain_query("svc", want_secret=False, platform="linux", run=lambda a, **k: r) == (True, None)
    assert claude_creds.keychain_query("svc", want_secret=True, platform="linux", run=lambda a, **k: r) == (True, "s3cret")


def test_find_generic_password_only_in_claude_creds_for_new_code():
    root = Path(__file__).resolve().parent.parent / "src" / "llm_router"
    for rel in ("discovery/keys.py", "secrets_vault.py", "seats.py", "resource_map.py"):
        assert "find-generic-password" not in (root / rel).read_text(), rel


# ── map integration ─────────────────────────────────────────────────────────

def test_map_shows_status_reason_key_fields_for_every_seat(tmp_path):
    seats = detect(tmp_path, {("codex", "login"): (1, CODEX_ERR)}, env={"OPENAI_API_KEY": "sk-x"})
    inp = rm.MapInputs(
        seats=seats, env={"OPENAI_API_KEY": "sk-x"},
        http_get=lambda u: None, http_post=lambda u, b: None,
        claude_reading=lambda: (None, "unknown", None), codex_counter=lambda: None,
        codex_sessions=tmp_path / "none", gemini_quota=lambda: None, policy="balanced", now=1.0,
        keys=lambda: K.discover(env={"OPENAI_API_KEY": "sk-x"}, dotenv_paths=[], keychain=lambda v: False),
    )
    data = rm.build(inp)
    names = {r["resource"] for r in data["resources"]}
    assert {"claude", "codex", "gemini_cli", "copilot", "ollama"} <= names
    codex = next(r for r in data["resources"] if r["resource"] == "codex")
    assert codex["status"] == "installed_not_connected" and codex["reason"] == CODEX_ERR
    ollama = next(r for r in data["resources"] if r["resource"] == "ollama")
    assert ollama["status"] == "installed_not_connected"
    for r in data["resources"]:
        assert r["status"] in ("connected", "installed_not_connected", "absent") and r["reason"], r["resource"]
    key = next(r for r in data["resources"] if r["resource"] == "api/OPENAI_API_KEY")
    assert set(key["key"]) == {"provider", "source", "usable_by", "billing"} and all(key["key"].values())
