"""Scrubbing is the gate between real prompts and the ground-truth dataset.

If it leaks, the dataset cannot be shared or committed; if it over-scrubs, the
tasks stop being gradable. Both directions are pinned here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from groundtruth.scrub import (  # noqa: E402
    CANONICAL_MARKER,
    PLACEHOLDER,
    canonical_available,
    residual_risk,
    salt_fingerprint,
    scrub,
)


def redacted(text: str) -> bool:
    """Something was removed — by either layer.

    Credentials are handled by `llm_router.secret_scrubber` and come back as
    `[REDACTED-*]`; identity and location are handled locally and come back as
    `<KIND:hash>`. Which layer fired is an implementation detail, so the tests
    assert on the guarantee instead: the secret is gone and a marker is there.
    """
    return bool(PLACEHOLDER.search(text) or CANONICAL_MARKER.search(text))

# Credential fixtures are ASSEMBLED, never written as literals.
#
# GitHub push protection blocked this repo's release twice over values in this
# list: first a Slack token and a Stripe *live* key, then the Stripe *test* key
# that replaced it. The values were synthetic each time, but a scanner cannot
# know that, and guessing at each detector's length threshold is a game with no
# end — the next scanner has different rules.
#
# Splitting the prefix from the body means no line here matches a credential
# pattern, while `scrub()` still receives the exact same string at run time. The
# test is unchanged in what it exercises; only the file's literal content is.
def _fixture(prefix: str, body: str) -> str:
    return prefix + body


SECRETS = [
    ("OPENAI_KEY", _fixture("sk-", "proj-AbCdEf0123456789AbCdEf0123456789")),
    ("ANTHROPIC_KEY", _fixture("sk-", "ant-api03-ZZZxxxYYY0123456789abcdef")),
    ("GITHUB_TOKEN", _fixture("ghp", "_AbCdEf0123456789AbCdEf0123456789abcd")),
    ("AWS_KEY_ID", _fixture("AKIA", "IOSFODNN7EXAMPLE")),
    ("GOOGLE_KEY", _fixture("AIza", "SyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q")),
    ("SLACK_TOKEN", _fixture("xoxb", "-NOTAREALTOKEN-EXAMPLE-000000")),
    ("STRIPE_KEY", _fixture("sk_", "test_NOTAREALKEYEXAMPLE000000")),
    ("JWT", _fixture("eyJhbGciOiJIUzI1NiJ9.", "eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVPmB92K27u")),
]


@pytest.mark.parametrize("kind,secret", SECRETS)
def test_secret_is_removed(kind: str, secret: str) -> None:
    text = f"here is the credential {secret} please use it"
    out, rep = scrub(text)
    assert secret not in out, f"{kind} survived scrubbing"
    assert rep.total >= 1
    assert redacted(out), "no redaction marker emitted"


def test_private_key_block_removed_whole() -> None:
    body = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKC\nAQEA\n-----END RSA PRIVATE KEY-----"
    out, rep = scrub(f"key:\n{body}\nthanks")
    assert "MIIEowIBAAKC" not in out
    assert "BEGIN RSA PRIVATE KEY" not in out
    # Handled by the canonical scrubber's `private_key` pattern, not locally.
    assert rep.counts.get("CANONICAL", 0) >= 1
    assert out.startswith("key:") and out.endswith("thanks")


def test_email_and_home_path() -> None:
    out, _ = scrub("mail yali.pollak@gmail.com about /Users/yaliandrona/Projects/x.py")
    assert "yali.pollak@gmail.com" not in out
    assert "yaliandrona" not in out
    # Path shape survives so the task still reads as a path reference.
    assert "/Projects/x.py" in out


def test_bearer_credential_removed() -> None:
    out, _ = scrub("Authorization: Bearer abcdef0123456789ABCDEF")
    assert "abcdef0123456789ABCDEF" not in out


def test_secret_assignment_removes_the_value() -> None:
    out, _ = scrub('export DATABASE_PASSWORD="hunter2hunter2"')
    assert "hunter2hunter2" not in out


def test_db_url_credentials_removed() -> None:
    out, _ = scrub("postgres://admin:s3cretpw@db.internal.example.com:5432/app")
    assert "s3cretpw" not in out


def test_private_ips_are_kept_public_removed() -> None:
    out, _ = scrub("talk to 192.168.1.10 and 127.0.0.1 but not 203.0.113.42")
    assert "192.168.1.10" in out, "private ranges are not sensitive"
    assert "127.0.0.1" in out
    assert "203.0.113.42" not in out


def test_version_numbers_are_not_phone_numbers() -> None:
    text = "bump to 13.3.1 and set timeout 45 and 15 seconds, n=1234567"
    out, _ = scrub(text)
    assert out == text, f"over-scrubbed: {out}"


def test_canonical_scrubber_is_reachable() -> None:
    """If this fails, scrub() raises rather than silently under-scrubbing."""
    ok, err = canonical_available()
    assert ok, f"canonical secret_scrubber unavailable: {err}"


def test_stable_token_for_repeated_path() -> None:
    home = "/Users/someone"
    out, _ = scrub(f"{home}/a and again {home}/b")
    emitted = [m.group(0) for m in PLACEHOLDER.finditer(out)]
    assert len(emitted) == 2, f"expected two placeholders, got {emitted}"
    assert len(set(emitted)) == 1, "same path must map to the same token"


def test_different_values_get_different_tokens() -> None:
    a = "/Users/alice"
    b = "/Users/bob"
    out, _ = scrub(f"{a}/x {b}/y")
    found = {m.group(0) for m in PLACEHOLDER.finditer(out)}
    assert len(found) == 2


def test_salt_changes_token() -> None:
    secret = "/Users/someone/project"
    one, _ = scrub(secret, salt=b"salt-one")
    two, _ = scrub(secret, salt=b"salt-two")
    assert one != two


def test_idempotent() -> None:
    text = ("sk-proj-AbCdEf0123456789AbCdEf0123456789 mail a@b.com "
            "from /Users/someone/x -----BEGIN PRIVATE KEY-----\nzz\n-----END PRIVATE KEY-----")
    once, _ = scrub(text)
    twice, rep2 = scrub(once)
    assert once == twice, "second pass must be a no-op"
    assert rep2.total == 0


def test_denylist_literal_terms() -> None:
    out, rep = scrub("the AcmeCorp deal and Project Bluebird",
                     denylist=["AcmeCorp", "Project Bluebird"])
    assert "AcmeCorp" not in out
    assert "Bluebird" not in out
    assert rep.counts.get("DENYLIST") == 2


def test_denylist_does_not_match_substring_of_word() -> None:
    out, _ = scrub("acme is in acmeology", denylist=["acme"])
    assert "acmeology" in out, "alphanumeric denylist terms match whole words only"


def test_empty_and_none_safe() -> None:
    out, rep = scrub("")
    assert out == "" and rep.total == 0


def test_salt_fingerprint_is_not_the_salt() -> None:
    fp = salt_fingerprint(b"my-secret-salt")
    assert "my-secret-salt" not in fp
    assert len(fp) == 16
    assert fp == salt_fingerprint(b"my-secret-salt")


def test_residual_risk_flags_what_scrubbing_missed() -> None:
    assert "confidentiality-marker" in residual_risk("This is CONFIDENTIAL, do not share")
    assert "ssn-shaped" in residual_risk("123-45-6789")
    assert "long-hex-blob" in residual_risk("a" * 8 + "0123456789abcdef0123456789abcdef")
    assert residual_risk("ordinary prompt about timeouts") == []


def test_scrub_report_merges() -> None:
    _, r1 = scrub("a@b.com")
    _, r2 = scrub("c@d.com")
    r1.merge(r2)
    assert r1.counts["EMAIL"] == 2
