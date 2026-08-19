"""Iteration 4 acceptance — control-plane Ed25519 signing."""
from __future__ import annotations

import base64

import pytest

from llm_router.control_plane import signing as s


def test_sign_and_verify_roundtrip() -> None:
    key = s.generate_ed25519_keypair()
    pub = s.public_key_b64(key)
    payload = b"policy-bundle-bytes"
    sig = s.sign_payload(key, payload)
    assert s.verify_payload(pub, payload, sig) is True


def test_tampered_payload_fails() -> None:
    key = s.generate_ed25519_keypair()
    pub = s.public_key_b64(key)
    sig = s.sign_payload(key, b"original")
    assert s.verify_payload(pub, b"tampered", sig) is False


def test_tampered_signature_fails() -> None:
    key = s.generate_ed25519_keypair()
    pub = s.public_key_b64(key)
    sig = s.sign_payload(key, b"data")
    # Flip a byte in the signature (keep it 64 bytes / valid b64).
    raw = bytearray(base64.b64decode(sig))
    raw[0] ^= 0xFF
    bad_sig = base64.b64encode(bytes(raw)).decode()
    assert s.verify_payload(pub, b"data", bad_sig) is False


def test_wrong_key_fails() -> None:
    signer = s.generate_ed25519_keypair()
    other = s.generate_ed25519_keypair()
    sig = s.sign_payload(signer, b"data")
    assert s.verify_payload(s.public_key_b64(other), b"data", sig) is False


def test_malformed_private_key_raises_clean() -> None:
    with pytest.raises(s.SigningKeyError) as ei:
        s.load_private_key_b64("not-valid-base64!!!")
    # The error message must NOT contain the offending input.
    assert "not-valid-base64" not in str(ei.value)


def test_malformed_public_key_and_signature_raise() -> None:
    key = s.generate_ed25519_keypair()
    good_sig = s.sign_payload(key, b"x")
    with pytest.raises(s.SigningKeyError):
        s.verify_payload("bad!!", b"x", good_sig)  # bad pubkey
    with pytest.raises(s.SigningKeyError):
        s.verify_payload(s.public_key_b64(key), b"x", "bad!!")  # bad sig


def test_load_signing_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    key = s.generate_ed25519_keypair()
    monkeypatch.setenv("LLM_ROUTER_CP_ED25519_PRIVATE_KEY", s.private_key_b64(key))
    loaded = s.load_signing_key()
    # Loaded key produces a verifiable signature under the original public key.
    sig = s.sign_payload(loaded, b"m")
    assert s.verify_payload(s.public_key_b64(key), b"m", sig) is True


def test_load_signing_key_unset_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_ROUTER_CP_ED25519_PRIVATE_KEY", raising=False)
    with pytest.raises(s.SigningKeyError):
        s.load_signing_key()


def test_key_material_never_in_exceptions() -> None:
    # A private key b64 that decodes but is the wrong length must not appear
    # in the raised message.
    secret_looking = base64.b64encode(b"A" * 16).decode()  # 16 bytes != 32
    with pytest.raises(s.SigningKeyError) as ei:
        s.load_private_key_b64(secret_looking)
    assert secret_looking not in str(ei.value)
    assert "AAAA" not in str(ei.value)
