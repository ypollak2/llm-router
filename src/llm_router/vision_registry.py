"""Which local models can actually SEE — decided by probing, not by a flag.

Images are the largest single cost in real use. Measured across 24 transcripts
over 5 days on this machine: 4,263,554 of 5,298,942 tokens of tool output —
**80.5%** — were images (screenshots read from disk, plus browser captures).
Bash was 14.3% and text file reads 3.1%. Routing image work locally is therefore
worth more than everything else in this tree combined.

But `ollama show` reporting a `vision` capability is the same kind of claim the
`tools` flag makes, and that one is already known to lie — see
`agentic_registry`, which exists because models advertise tool support they
cannot deliver. A model that decodes an image but cannot read a label in it is
worse than no local vision at all: it returns a confident wrong answer instead
of declining.

So capability is MEASURED. The probe renders an image containing a value no
model could guess, asks for that value, and checks the reply for it. Pass means
the model demonstrably read pixels it had never seen; fail means route the work
to Claude. Verdicts are cached with a TTL and keyed to the installed-model set,
so pulling a new model re-probes without a code change.

Nothing here is hardcoded per model. A future model that can see is picked up
automatically, and one that regresses is dropped the same way.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import time
import urllib.request
from pathlib import Path
from typing import Any

from llm_router.hooks.agent_loop import _get_ollama_url

_CACHE_NAME = "vision_models.json"
_TTL_S = 604800  # a week, matching agentic_registry


def cache_path() -> Path:
    """Resolved per call — a module-level Path.home() freezes $HOME at import,
    the defect class that has bitten this tree four times."""
    base = os.environ.get("LLM_ROUTER_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".llm-router"
    return root / _CACHE_NAME


def vision_routing_enabled() -> bool:
    """Default ON, but the probe still decides. `off` sends images to Claude
    regardless of what the local models can do."""
    return os.environ.get("LLM_ROUTER_LOCAL_VISION", "").strip().lower() not in (
        "0", "off", "false", "no",
    )


# ── the probe image ─────────────────────────────────────────────────────────

# A 5x7 bitmap for the digits 0-9. Drawing the probe image with the standard
# library rather than Pillow is deliberate: Pillow is an OPTIONAL dependency in
# this tree and is absent from the venv, so a PIL-based probe raised ImportError
# and returned False for every model — which silently meant "no local vision"
# on a machine where a model scores 8/8 on real screenshots. A capability probe
# that fails closed because of ITS OWN missing dependency is indistinguishable
# from a model that cannot see, and that is the worst kind of wrong.
_DIGITS = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
}

_SCALE = 18
_PAD = 36
# Edge softening radius. Hard-edged blocks are NOT what a screenshot looks like:
# probed with aliased glyphs, qwen3.5 read 8315 as "815" and 9062 as "962",
# dropping one digit every time, while scoring 8/8 on real antialiased UI
# screenshots. The probe was harder than the task it stands in for.
#
# Supersampling was tried first and does nothing here: the glyph blocks align
# exactly to the sample grid, so every downsampled box is pure black or pure
# white. A blur is what actually produces the partial-coverage greys that font
# rasterisers emit — and a test asserts the greys exist, because the
# supersampled version looked correct and was not.
_BLUR = 2


def _probe_image(code: str) -> bytes:
    """A PNG showing `code` as large antialiased digits on white.

    Written with zlib and struct so it has no third-party dependency — Pillow is
    optional in this tree and absent from the venv, and a PIL-based probe
    returned False for every model, which is indistinguishable from "no model
    can see". The code is random per probe, so a model cannot pass by memorising
    a fixture.
    """
    import struct
    import zlib

    glyph_w, glyph_h = 5, 7
    width = _PAD * 2 + len(code) * (glyph_w + 1) * _SCALE
    height = _PAD * 2 + glyph_h * _SCALE

    # One byte per pixel while drawing; 255 is white.
    grid = [bytearray([255]) * width for _ in range(height)]
    for index, char in enumerate(code):
        bitmap = _DIGITS.get(char)
        if not bitmap:
            continue
        ox = _PAD + index * (glyph_w + 1) * _SCALE
        for gy, line in enumerate(bitmap):
            for gx, bit in enumerate(line):
                if bit != "1":
                    continue
                x0 = ox + gx * _SCALE
                for py in range(_SCALE):
                    row = grid[_PAD + gy * _SCALE + py]
                    row[x0:x0 + _SCALE] = b"\x00" * _SCALE

    # Box blur -> grey edges, the partial coverage a font rasteriser produces.
    blurred = []
    for y in range(height):
        row = bytearray(width)
        for x in range(width):
            total = count = 0
            for dy in range(-_BLUR, _BLUR + 1):
                yy = y + dy
                if not 0 <= yy < height:
                    continue
                line = grid[yy]
                lo, hi = max(0, x - _BLUR), min(width, x + _BLUR + 1)
                total += sum(line[lo:hi])
                count += hi - lo
            row[x] = total // max(count, 1)
        blurred.append(row)

    rows = [bytearray(b"".join(bytes((v, v, v)) for v in row)) for row in blurred]

    raw = b"".join(b"\x00" + bytes(row) for row in rows)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


# Three independent codes, all of which must be read exactly.
#
# One trial is not a verdict. Measured: qwen3.5 reads 1 of 3 antialiased codes
# (8315 -> "815", 9062 -> "932") while qwen3.8 reads 3 of 3. A single-trial probe
# would therefore call qwen3.5 vision-capable one run in three, and the caller
# would have no way to know which run it got. For screenshots a nearly-right
# number is worse than no answer, so the bar is exactness, three times.
_PROBE_TRIALS = 3


def probe_model(model: str, timeout: int = 90) -> bool:
    """Can this model read values it has never seen, out of an image?

    Deliberately the easiest possible vision task: large dark digits on white.
    A model that fails THIS cannot be trusted with a UI screenshot; one that
    passes has proven it is reading pixels rather than guessing — the codes are
    random, so guessing all three is 1 in 9000^3.

    Never raises. An unreachable Ollama, a model that will not load, or any
    other failure is a False, which routes the work to Claude.
    """
    for _ in range(_PROBE_TRIALS):
        code = f"{random.randint(1000, 9999)}"
        try:
            png = _probe_image(code)
        except Exception:
            return False
        body = json.dumps({
            "model": model,
            "stream": False,
            "think": False,
            "options": {"temperature": 0.0},
            "messages": [{
                "role": "user",
                "content": "What number is written in this image? Reply with the digits only.",
                "images": [base64.b64encode(png).decode()],
            }],
        }).encode()
        try:
            request = urllib.request.Request(
                f"{_get_ollama_url().rstrip('/')}/api/chat",
                data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                reply = json.loads(response.read()).get("message", {}).get("content", "")
        except Exception:
            return False
        if code not in (reply or ""):
            return False
    return True


# ── candidates ──────────────────────────────────────────────────────────────

def advertised_vision_models() -> list[str]:
    """Models whose Ollama metadata CLAIMS vision.

    Used only to decide who is worth probing — probing every installed model
    would spend a request each on text-only models that obviously cannot. The
    claim narrows the candidates; the probe decides the answer.
    """
    try:
        url = f"{_get_ollama_url().rstrip('/')}/api/tags"
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return []

    names = [m["name"] for m in payload.get("models", [])
             if isinstance(m, dict) and isinstance(m.get("name"), str)]
    capable: list[str] = []
    for name in names:
        try:
            request = urllib.request.Request(
                f"{_get_ollama_url().rstrip('/')}/api/show",
                data=json.dumps({"model": name}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                info = json.loads(response.read())
            if "vision" in (info.get("capabilities") or []):
                capable.append(name)
        except Exception:
            continue
    return capable


def _models_hash(models: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(sorted(models), separators=(",", ":")).encode()).hexdigest()


def _read_cache() -> dict[str, Any] | None:
    try:
        payload = json.loads(cache_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if not isinstance(payload.get("probed_at"), (int, float)):
        return None
    verdicts = payload.get("verdicts")
    if not isinstance(verdicts, dict):
        return None
    if not all(isinstance(k, str) and isinstance(v, bool) for k, v in verdicts.items()):
        return None
    return payload


def _write_cache(payload: dict[str, Any]) -> None:
    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_registry(force: bool = False, ttl_seconds: int = _TTL_S,
                 allow_probe: bool = True) -> dict[str, bool]:
    """Model -> proven-vision verdict.

    `allow_probe=False` is the hot path: never block on a probe (each is a
    round-trip of seconds). It returns whatever the cache holds as a soft hint,
    else {} — and {} means "nothing proven", which routes images to Claude. The
    conservative direction is the one that cannot produce a confidently wrong
    answer about a screenshot.
    """
    try:
        candidates = sorted(advertised_vision_models())
        digest = _models_hash(candidates)
        cache = _read_cache()
        fresh = (
            cache is not None
            and cache.get("models_hash") == digest
            and time.time() - float(cache.get("probed_at", 0)) <= ttl_seconds
        )
        if not force and fresh:
            return dict(cache["verdicts"])
        if not allow_probe:
            return dict(cache["verdicts"]) if cache else {}

        verdicts = {name: probe_model(name) for name in candidates}
        _write_cache({"probed_at": time.time(), "models_hash": digest,
                      "verdicts": verdicts})
        return verdicts
    except Exception:
        return {}


def best_vision_model(allow_probe: bool = False) -> str | None:
    """The model to send an image to, or None to leave it with Claude.

    None is the correct answer whenever nothing has been PROVEN, including when
    Ollama is down or no probe has run. A wrong description of a screenshot is
    more expensive than the tokens it saves.
    """
    if not vision_routing_enabled():
        return None
    verdicts = get_registry(allow_probe=allow_probe)
    passed = [name for name, ok in verdicts.items() if ok]
    if not passed:
        return None
    # Prefer the model the caller pinned, if it passed.
    preferred = os.environ.get("LLM_ROUTER_VISION_MODEL", "").strip()
    if preferred in passed:
        return preferred
    return sorted(passed)[0]


def can_route_images(allow_probe: bool = False) -> bool:
    return best_vision_model(allow_probe=allow_probe) is not None
