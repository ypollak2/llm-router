"""Ed25519 signing for control-plane policy bundles.

The control plane signs each policy bundle with an Ed25519 private key;
sidecars verify with the public key and never hold the signing secret.
Security posture: no function here places key or signature bytes into an
exception message or log — all error messages are generic.
"""
from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


class SigningKeyError(Exception):
    """Raised for malformed or missing signing keys (never echoes key bytes)."""


def generate_ed25519_keypair() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def private_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    return base64.b64encode(raw).decode("ascii")


def public_key_b64(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    public_key = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    raw = public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def load_private_key_b64(b64: str) -> Ed25519PrivateKey:
    try:
        raw = base64.b64decode(b64, validate=True)
        if len(raw) != 32:
            raise ValueError
        return Ed25519PrivateKey.from_private_bytes(raw)
    except Exception as exc:
        raise SigningKeyError("invalid Ed25519 private key") from exc


def load_signing_key() -> Ed25519PrivateKey:
    b64 = os.environ.get("LLM_ROUTER_CP_ED25519_PRIVATE_KEY")
    if b64 is None:
        raise SigningKeyError("LLM_ROUTER_CP_ED25519_PRIVATE_KEY not set")
    return load_private_key_b64(b64)


def sign_payload(key: Ed25519PrivateKey, payload_bytes: bytes) -> str:
    signature = key.sign(payload_bytes)
    return base64.b64encode(signature).decode("ascii")


def verify_payload(
    public_key_b64: str,
    payload_bytes: bytes,
    signature_b64: str,
) -> bool:
    try:
        public_raw = base64.b64decode(public_key_b64, validate=True)
        if len(public_raw) != 32:
            raise ValueError
        public_key = Ed25519PublicKey.from_public_bytes(public_raw)
    except Exception as exc:
        raise SigningKeyError("invalid Ed25519 public key") from exc

    try:
        signature = base64.b64decode(signature_b64, validate=True)
        if len(signature) != 64:
            raise ValueError
    except Exception as exc:
        raise SigningKeyError("invalid Ed25519 signature") from exc

    try:
        public_key.verify(signature, payload_bytes)
        return True
    except InvalidSignature:
        return False


__all__ = [
    "SigningKeyError",
    "generate_ed25519_keypair",
    "private_key_b64",
    "public_key_b64",
    "load_private_key_b64",
    "load_signing_key",
    "sign_payload",
    "verify_payload",
]
