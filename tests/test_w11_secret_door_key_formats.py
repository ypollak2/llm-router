"""W11: the secret door holds private-key blocks in every format a person pastes, and more provider keys.

INCIDENT: an offline census of the delegation gates (2026-11-04, 131 real prompts plus synthetic controls) found that the
agent-route secret door caught 3 of 4 fake secrets and missed a PEM private-key block. A prompt like that would have been
sent to Codex, that is to OpenAI. The cause was in ``engine._pem_block_is_real``: it asked for a 40-character body made of
nothing but base64, so these were all read as "a bare header in a question" and passed:

* a truncated paste (``-----BEGIN RSA PRIVATE KEY-----`` + the first line of the key),
* a key inside a JSON or YAML string, where the line breaks are the two characters backslash-n (a Google service-account file),
* a legacy encrypted PEM with its ``Proc-Type:`` / ``DEK-Info:`` armor headers,
and ``-----BEGIN PGP PRIVATE KEY BLOCK-----`` was never matched at all (the header regex wanted ``PRIVATE KEY-----``).

Every secret here is drawn from ``random.Random(seed)`` when a test runs; nothing real-looking is committed.
"""

from __future__ import annotations

import importlib.util
import random
import string
from pathlib import Path

import pytest

from llm_router import engine, local_privacy
from llm_router.secret_scrubber import scrub_text
from tests.secret_fixtures import placeholders

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "src" / "llm_router" / "hooks" / "agent-route.py"

B64 = string.ascii_letters + string.digits + "+/"
B62 = string.ascii_letters + string.digits
B64URL = B62 + "-_"
HEX = "0123456789abcdef"


def _draw(rng: random.Random, alphabet: str, n: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(n))


def _body(seed: int = 1, lines: int = 5) -> list[str]:
    r = random.Random(f"w11-body:{seed}")
    return [_draw(r, B64, 64) for _ in range(lines)] + [_draw(r, B64, 37) + "="]


def _pem(kind: str = "RSA PRIVATE KEY", seed: int = 1) -> str:
    return f"-----BEGIN {kind}-----\n" + "\n".join(_body(seed)) + f"\n-----END {kind}-----"


def key_formats() -> dict[str, str]:
    """Texts that hold a private key, by the way it reached the prompt."""
    r = random.Random("w11-formats")
    body = _body()
    lines = "\\n".join(body)
    return {
        "pem": _pem(),
        "pkcs8": _pem("PRIVATE KEY"),
        "openssh": _pem("OPENSSH PRIVATE KEY"),
        "ec": _pem("EC PRIVATE KEY"),
        "encrypted_pkcs8": _pem("ENCRYPTED PRIVATE KEY"),
        "census_truncated_paste": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA7\n-----END RSA PRIVATE KEY-----",
        "header_and_first_line_only": "-----BEGIN PRIVATE KEY-----\n" + body[0] + "\nthe rest is cut off",
        "one_line_newlines_as_spaces": "-----BEGIN PRIVATE KEY----- " + " ".join(body) + " -----END PRIVATE KEY-----",
        "windows_line_endings": "-----BEGIN PRIVATE KEY-----\r\n" + "\r\n".join(body) + "\r\n-----END PRIVATE KEY-----",
        "json_string_escaped_newlines": '{"type": "service_account", "private_key": "-----BEGIN PRIVATE KEY-----\\n'
                                        + lines + '\\n-----END PRIVATE KEY-----\\n"}',
        "json_in_json_double_escaped": '"-----BEGIN PRIVATE KEY-----\\\\n' + "\\\\n".join(body)
                                       + '\\\\n-----END PRIVATE KEY-----"',
        "python_string_concatenation": '"-----BEGIN PRIVATE KEY-----\\n"\n' + "\n".join(f'"{ln}\\n" +' for ln in body),
        "legacy_encrypted_pem": "-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\n"
                                "DEK-Info: AES-128-CBC,A1B2C3D4E5F60718A1B2C3D4E5F60718\n\n"
                                + "\n".join(body) + "\n-----END RSA PRIVATE KEY-----",
        "pgp_armor": "-----BEGIN PGP PRIVATE KEY BLOCK-----\nVersion: GnuPG v2\nComment: work key\n\n"
                     + "\n".join(body) + "\n=ab3Z\n-----END PGP PRIVATE KEY BLOCK-----",
        "ssh2_four_dashes": "---- BEGIN SSH2 ENCRYPTED PRIVATE KEY ----\nSubject: me\nComment: \"2048-bit rsa\"\n\n"
                            + "\n".join(body) + "\n---- END SSH2 ENCRYPTED PRIVATE KEY ----",
        "inside_a_question": "Why does this fail to load?\n\n" + _pem() + "\n\nIt worked yesterday.",
        "putty_ppk": "PuTTY-User-Key-File-3: ssh-ed25519\nEncryption: none\nComment: laptop\nPublic-Lines: 2\n"
                     + _draw(r, B64, 64) + "\n" + _draw(r, B64, 20) + "\nPrivate-Lines: 1\n" + _draw(r, B64, 64)
                     + "\nPrivate-MAC: " + _draw(r, HEX, 64),
    }


def provider_keys() -> dict[str, str]:
    r = random.Random("w11-provider")
    return {
        "stripe_live": "STRIPE_SECRET_KEY=sk_live_" + _draw(r, B62, 24),
        "stripe_restricted_live": "rk_live_" + _draw(r, B62, 32),
        "sendgrid": "SG." + _draw(r, B64URL, 22) + "." + _draw(r, B64URL, 43),
        "huggingface": "HF_TOKEN=hf_" + _draw(r, B62, 34),
        "npm": "//registry.npmjs.org/:_authToken=npm_" + _draw(r, B62, 36),
        "gitlab": "glpat-" + _draw(r, B64URL, 20),
        "pypi": "pypi-AgEIcHlwaS5vcmc" + _draw(r, B64URL, 70),
        "digitalocean": "dop_v1_" + _draw(r, HEX, 64),
        "slack_webhook": "https://hooks.slack.com/services/T" + _draw(r, string.ascii_uppercase + string.digits, 10)
                         + "/B" + _draw(r, string.ascii_uppercase + string.digits, 10) + "/" + _draw(r, B62, 24),
        "age": "AGE-SECRET-KEY-1" + _draw(r, "QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L", 58),
        "azure_storage": "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=" + _draw(r, B64, 86)
                         + "==;EndpointSuffix=core.windows.net",
    }


NOT_KEYS = {
    "bare_header_in_a_question": "How do I parse a block that starts with -----BEGIN RSA PRIVATE KEY----- in Python?",
    "header_then_prose_without_punctuation": "-----BEGIN PRIVATE KEY-----\nWhy does openssl say this header is malformed "
                                             "when parsing my PEM file and what should I check next",
    "header_then_elided_body": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC...\n-----END PRIVATE KEY-----",
    "x_filler_body": "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(["x" * 64] * 4) + "\n-----END RSA PRIVATE KEY-----",
    "angle_bracket_placeholder": "-----BEGIN PRIVATE KEY-----\n<your key here>\n-----END PRIVATE KEY-----",
    "pgp_header_only": "How do I import a file that starts with -----BEGIN PGP PRIVATE KEY BLOCK----- with gpg?",
    "public_certificate": "-----BEGIN CERTIFICATE-----\n" + "\n".join(_body(3)) + "\n-----END CERTIFICATE-----",
    "stripe_test_placeholder": "STRIPE_SECRET_KEY=sk_test_replace_me",
    "stripe_live_filler": "sk_live_" + "x" * 30,
    "huggingface_filler": "HF_TOKEN=hf_" + "x" * 34,
    "npm_filler": "//registry.npmjs.org/:_authToken=npm_" + "0" * 36,
    "age_public_recipient": "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p",
    "ppk_public_half_only": "PuTTY-User-Key-File-3: ssh-ed25519\nEncryption: none\nComment: laptop\nPublic-Lines: 2\n"
                            + "\n".join(_body(4)[:2]),
}


def _high(text: str) -> bool:
    return any(engine.secret_findings(text).values())


def test_the_fixtures_are_not_empty():
    assert len(key_formats()) >= 15 and len(provider_keys()) >= 10 and len(NOT_KEYS) >= 10


@pytest.mark.parametrize("name", sorted(key_formats()))
def test_every_private_key_format_is_a_high_precision_hit(name):
    text = key_formats()[name]
    assert _high(text), f"{name}: engine.secret_findings = {engine.secret_findings(text)}"


@pytest.mark.parametrize("name", sorted(provider_keys()))
def test_provider_keys_with_a_distinctive_prefix_are_hits(name):
    text = provider_keys()[name]
    assert _high(text), f"{name}: engine.secret_findings = {engine.secret_findings(text)}"


@pytest.mark.parametrize("name", sorted(NOT_KEYS))
def test_questions_placeholders_and_public_material_are_not_hits(name):
    text = NOT_KEYS[name]
    assert not _high(text), f"{name} was held: {engine.secret_findings(text)}"


def test_the_existing_placeholder_corpus_still_passes():
    held = [p[:60] for p in placeholders() if _high(p)]
    assert not held, held


@pytest.mark.parametrize("name", sorted(set(key_formats()) | set(provider_keys())))
def test_the_secret_door_reports_the_category_and_never_the_value(name):
    text = {**key_formats(), **provider_keys()}[name]
    hits = local_privacy.scan_secrets(prompt="Explain why this fails: " + text)
    assert hits, name
    assert all(h.where == "current prompt" for h in hits)
    assert all(h.label in engine.SECRET_LABELS.values() for h in hits)
    assert not any(part in h.label for h in hits for part in text.split() if len(part) > 20)


def test_a_key_far_into_a_long_prompt_is_still_found():
    pad = "Explain this module. " * 3000  # past the 16 KiB scan window
    assert _high(pad + _pem("PGP PRIVATE KEY BLOCK") + "\n" + pad)


def test_a_thousand_headers_do_not_cost_a_second():
    import time

    text = "-----BEGIN PGP PRIVATE KEY BLOCK-----" * 1500
    started = time.monotonic()
    found = engine.secret_findings(text)
    assert time.monotonic() - started < 2.0
    assert not any(found.values())


def _load_hook():
    spec = importlib.util.spec_from_file_location("agent_route_w11_secret", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", ["census_truncated_paste", "json_string_escaped_newlines", "pgp_armor", "legacy_encrypted_pem",
                                  "pem", "openssh", "putty_ppk"])
def test_agent_route_holds_a_prompt_with_a_private_key_before_any_cloud_path(name):
    """The census called the hook's own ``_secret_door``; a hold means Codex is never started for the prompt."""
    hook = _load_hook()
    _prompt, hold = hook._secret_door("Review the deploy script. Its key is below.\n\n" + key_formats()[name])
    assert hold is True


def test_agent_route_lets_a_prompt_without_a_key_through_the_door():
    hook = _load_hook()
    _prompt, hold = hook._secret_door(NOT_KEYS["bare_header_in_a_question"])
    assert hold is False


@pytest.mark.parametrize("name", ["pgp_armor", "ssh2_four_dashes", "pem", "openssh"])
def test_the_scrubber_redacts_the_same_block_shapes_the_door_holds(name):
    text = "before " + key_formats()[name] + " after"
    out = scrub_text(text)
    assert out != text and out.startswith("before ") and out.endswith(" after")
    assert not any(line in out for line in _body())


def test_a_short_prefix_inside_a_long_random_blob_is_not_a_key():
    """``hf_`` and ``npm_`` are 3 and 4 characters; one turns up by chance in about one 100 KB base64url blob in ten. The
    first version of these shapes refused two of four such blobs (tests/test_secdet_token_not_surroundings.py)."""
    import base64

    rng = random.Random("w11-blobs")
    blob = base64.urlsafe_b64encode(rng.randbytes(768)).decode()
    for prefix in ("hf_", "npm_"):
        glued = blob[:300] + prefix + _draw(rng, B62, 40) + blob[300:]
        assert not _high(glued), prefix
    blobs = [base64.urlsafe_b64encode(rng.randbytes(75_000)).decode() for _ in range(6)]
    assert [i for i, b in enumerate(blobs) if _high("payload: " + b)] == []


def test_a_short_prefix_that_starts_a_run_is_a_key():
    r = random.Random("w11-run-start")
    assert _high("export HF_TOKEN=hf_" + _draw(r, B62, 34))
    assert _high("hf_" + _draw(r, B62, 34) + " is the token")
    assert _high("npm_" + _draw(r, B62, 36))
