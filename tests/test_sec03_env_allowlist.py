"""Regression: CHZ-SEC-03 — get_safe_env() leaked secrets to child CLIs.

The blocklist missed AWS_ACCESS_KEY_ID, GH_PAT and DATABASE_URL (none end in
_API_KEY/_TOKEN or contain SECRET), so they reached spawned codex/gemini/claude
CLI subprocesses. The blocklist is now broadened to the credential classes.
"""
from llm_router.safe_subprocess import get_safe_env

SECRETS = {
    "AWS_ACCESS_KEY_ID": "AKIA123", "AWS_SECRET_ACCESS_KEY": "s",
    "AWS_SESSION_TOKEN": "t", "GH_PAT": "ghp_x", "GITHUB_TOKEN": "ghp_y",
    "DATABASE_URL": "postgres://u:p@h/db", "REDIS_URL": "redis://u:p@h",
    "MY_CONNECTION_STRING": "Server=x;Pwd=y", "SIGNING_PRIVATE_KEY": "k",
    "OPENAI_API_KEY": "sk-x",
}
BENIGN = {"PATH": "/usr/bin", "HOME": "/h", "LANG": "en", "OLLAMA_BUDGET_MODELS": "q"}


def test_secrets_stripped_benign_kept(monkeypatch):
    for k, v in {**SECRETS, **BENIGN}.items():
        monkeypatch.setenv(k, v)
    env = get_safe_env()
    leaked = [k for k in SECRETS if k in env]
    dropped = [k for k in BENIGN if k not in env]
    assert not leaked, f"CHZ-SEC-03: secrets leaked to child env: {leaked}"
    assert not dropped, f"benign vars wrongly dropped: {dropped}"
