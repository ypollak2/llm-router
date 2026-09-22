"""Credentials must be redacted BEFORE they are written — R1.

The audit's shortest path to a durable leak needed no attacker and no
non-default flag: a provider call fails, the exception text is stringified
into a `reason` field, and one line writes it to disk.

Measured against `attempt_log` before this fix — a file created at **0644,
world-readable**:

    github PAT  LEAKED IN FULL      slack       LEAKED IN FULL
    AWS key id  LEAKED IN FULL      bearer      LEAKED IN FULL
    AWS secret  LEAKED IN FULL      postgres DSN (not matched by any pattern)

TRUNCATION IS NOT REDACTION, and this is worth stating because it defeated the
first probe of this very defect. `reason[:80]` shortened a 100-character
Anthropic key enough that an exact-match search reported "clean" while 80
characters of the key sat on disk. Scrub first, truncate second.
"""

from __future__ import annotations

import json
import pathlib
import stat

import pytest

from llm_router import attempt_log
from llm_router.secret_scrubber import scrub_text

#: Realistic shapes. Lengths matter: a 40-char PAT and a 20-char AWS key id
#: both fit inside the truncation window that hid the longer Anthropic key.
CANARIES = {
    "anthropic":   "sk-ant-api03-AUDITcanary00000000000000000000000000000000",
    "openai":      "sk-proj-AUDITcanary000000000000000000000000000000000000",
    "github_pat":  "ghp_AUDITcanary0000000000000000000000000",
    "aws_key_id":  "AKIAAUDITCANARY00000",
    "slack":       "xoxb-1234-5678-AUDITcanaryTOKENvalue",
    "google":      "AIzaSyAUDITcanary00000000000000000000000",
    "bearer":      "Bearer AUDITcanaryBEARERtoken123456",
    "postgres_dsn": "postgresql://user:AUDITcanaryPW@db.example:5432/prod",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    return tmp_path


def _log_path(home: pathlib.Path) -> pathlib.Path:
    """Ask the module where it writes, rather than assuming.

    Under pytest `attempt_log` splits to `attempts.test.jsonl`. Hardcoding
    the production name made every assertion below read a file that never
    existed — which fails, but for the wrong reason, and would have passed
    silently had the assertions been phrased as "secret not in text".
    """
    return attempt_log._path()


# ── the leak ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, secret", sorted(CANARIES.items()))
def test_a_provider_error_carrying_a_credential_does_not_persist_it(
    home, name, secret
):
    """The exact shape: `direct_executor` passes f"call raised: {exc}"."""
    attempt_log.record(
        model="ollama/qwen", outcome="failed", latency_ms=1,
        reason=f"call raised: AuthError({secret})",
    )
    text = _log_path(home).read_text(encoding="utf-8")

    # A FRAGMENT is a leak. Checking only for the whole string is what made a
    # first probe of this defect report "no leak" while 80 characters of an
    # Anthropic key were on disk.
    fragment = secret[:24]
    assert fragment not in text, (
        f"{name}: a {len(secret)}-char credential survived into "
        f"attempts.jsonl as {fragment!r}"
    )


def test_the_file_is_private_at_creation(home):
    """0600 when CREATED, not 0644-then-chmod.

    `open` creates with 0666 & ~umask. Anything that opens the file inside
    that window keeps a readable handle after a later chmod, because
    permissions are checked at open time.
    """
    attempt_log.record(model="m", outcome="ok", latency_ms=1, reason="fine")
    mode = stat.S_IMODE(_log_path(home).stat().st_mode)
    assert mode == 0o600, f"attempts.jsonl created at {oct(mode)}"


def test_a_legacy_0644_file_is_repaired(home):
    """An opener only sets the mode when it CREATES.

    Without the repair, the fix would protect new installs and leave every
    existing one exposed.
    """
    p = _log_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"ts": 0}\n', encoding="utf-8")
    p.chmod(0o644)

    attempt_log.record(model="m", outcome="ok", latency_ms=1, reason="fine")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


# ── anti-vacuity ─────────────────────────────────────────────────────────────

def test_an_ordinary_reason_survives_intact(home):
    """Scrubbing everything would pass every test above and destroy the field.

    A `reason` that says nothing is not a diagnostic.
    """
    attempt_log.record(
        model="ollama/qwen", outcome="failed", latency_ms=1,
        reason="connection refused to 127.0.0.1:11434",
    )
    row = json.loads(_log_path(home).read_text(encoding="utf-8").splitlines()[-1])
    assert "connection refused" in row["reason"]
    assert "11434" in row["reason"]


def test_a_plain_url_is_not_redacted():
    """The DSN pattern must not eat ordinary URLs."""
    plain = "https://api.example.com/v1/models"
    assert scrub_text(plain) == plain


def test_scrubbing_happens_before_truncation(home):
    """The ordering that the first probe of this defect got wrong.

    A long credential must not be merely shortened into the record.
    """
    long_secret = "sk-ant-api03-" + ("A" * 120)
    attempt_log.record(model="m", outcome="failed", latency_ms=1,
                       reason=f"err {long_secret}")
    text = _log_path(home).read_text(encoding="utf-8")
    assert "sk-ant-api03-AAAA" not in text


# ── the known, deliberate gap ────────────────────────────────────────────────

def test_a_bare_aws_secret_is_a_documented_gap():
    """NOT a bug. Pinned so it is a decision rather than a surprise.

    A bare 40-character base64 string is indistinguishable from a commit SHA,
    a sha256 digest or any base64 blob. Matching it would redact large amounts
    of legitimate diagnostic text, and a scrubber that destroys the field it is
    protecting gets turned off.

    The LABELLED form is redacted. If this ever changes, this test should fail
    and the tradeoff be re-argued rather than silently reversed.
    """
    bare = "wJalrXUtnFEMIAUDITCANARYbPxRfiCYEXAMPLEKE"
    assert bare in scrub_text(f"err {bare}"), (
        "bare AWS secrets are now redacted — verify the false-positive cost "
        "on commit SHAs and base64 before accepting this"
    )
    assert bare not in scrub_text(f"aws_secret_access_key={bare}")
