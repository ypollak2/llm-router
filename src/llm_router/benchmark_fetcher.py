"""SHA-256-pinned runtime catalogs; live generators belong in scripts/bench."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

CATALOG_URL = (
    "https://raw.githubusercontent.com/ypollak2/llm-router/"
    "{tag}/src/llm_router/data/benchmarks.json"
)
log = logging.getLogger("llm_router.benchmark_fetcher")


def load_pins() -> dict[str, Any]:
    return json.loads((Path(__file__).parent / "data/catalog_pins.json").read_text())


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PinnedFetchError(Exception):
    """The pinned download failed ("download failed") or was rejected ("sha256 mismatch")."""


def fetch_pinned(url: str, expected_sha256: str | None, timeout: int = 10) -> bytes:
    """Return the bytes at url iff their sha256 equals the pin; else raise PinnedFetchError."""
    if not expected_sha256:
        raise PinnedFetchError("no pin")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = response.read()
    except Exception as exc:
        raise PinnedFetchError(f"download failed: {exc}") from exc
    actual = sha256_hex(data)
    if actual != expected_sha256:
        log.warning(
            "catalog rejected: sha256 mismatch for %s (expected %s..., got %s...)",
            url, expected_sha256[:12], actual[:12],
        )
        raise PinnedFetchError("sha256 mismatch")
    return data


def fetch_pinned_catalog(output_path: Path) -> bool:
    temporary: Path | None = None
    try:
        pin = load_pins().get("benchmarks", {})
        if not pin.get("sha256"):
            return False
        if output_path.exists() and sha256_hex(output_path.read_bytes()) == pin["sha256"]:
            return True  # installed copy is already the pinned catalog; no download
        data = fetch_pinned(CATALOG_URL.format(tag=pin["tag"]), pin["sha256"])
        catalog = json.loads(data)
        if not isinstance(catalog, dict) or "version" not in catalog:
            return False
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=output_path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, output_path)
        temporary = None
        return True
    except PinnedFetchError as exc:
        if "mismatch" not in str(exc):  # a mismatch was already logged by fetch_pinned
            log.warning("catalog not installed: %s", exc)
        return False
    except Exception as exc:
        log.warning("catalog not installed: %s", exc)
        return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
