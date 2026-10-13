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


def fetch_pinned(url: str, expected_sha256: str | None, timeout: int = 10) -> bytes | None:
    if not expected_sha256:
        return None
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = response.read()
    except Exception:
        return None
    actual = sha256_hex(data)
    if actual != expected_sha256:
        log.warning(
            "catalog rejected: sha256 mismatch for %s (expected %s..., got %s...)",
            url, expected_sha256[:12], actual[:12],
        )
        return None
    return data


def fetch_pinned_catalog(output_path: Path) -> bool:
    temporary: Path | None = None
    try:
        pin = load_pins().get("benchmarks", {})
        if not pin.get("sha256"):
            return False
        data = fetch_pinned(CATALOG_URL.format(tag=pin["tag"]), pin["sha256"])
        if data is None:
            return False
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
    except Exception as exc:
        log.warning("catalog not installed: %s", exc)
        return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
