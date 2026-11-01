"""SECDETb: a legacy OpenAI key (``sk-`` + 48 base62) at a token start with a long base64url run glued after it is
refused again.

SECDET (#792) made an unmarked ``sk-`` end within 256 key characters, so an ``sk-`` inside a long base64url blob is
not read as a key. That cap also let ``sk-`` + 48 + 209 or more base64url characters (one glued run) through the
door; the parent commit 26948da7 refused it and the scrubber still redacted it (rv-792 review). The door now decides
by the character before ``sk-``: at a token start the key is read at any run length; after a base64url character
(``-``, inside a blob) the 256 end cap still applies. Every value is drawn at run time (tests/secret_fixtures.py).
"""
from __future__ import annotations

import random
import time

import pytest

from llm_router import local_privacy
from llm_router.secret_scrubber import scrub_text
from tests import secret_fixtures as fx


def _legacy_key(seed: int) -> str:
    return "sk-" + fx._with_digit(fx._draw(random.Random(f"secdetb-legacy:{seed}"), fx.B62, 48))


def _run(seed: int, n: int) -> str:
    return fx._draw(random.Random(f"secdetb-run:{seed}:{n}"), fx.B64URL, n)


@pytest.mark.parametrize("tail", (209, 210, 300, 1_000, 20_000))
def test_a_legacy_key_at_a_token_start_glued_to_a_long_run_is_refused(tail):
    for seed in range(3):
        key = _legacy_key(seed)
        glued = key + _run(seed, tail)
        for text in (glued, f"key {glued}", f"OPENAI_API_KEY={glued}", f'"{glued}"', f"line1\n{glued} end"):
            assert local_privacy.scan_secrets(text), (tail, seed, text[:8])
            assert key[-16:] not in scrub_text(text), (tail, seed)


def test_an_sk_inside_a_long_base64url_blob_is_still_not_read():
    # the SECDET improvement this keeps: after ``-`` (a base64url character) an unmarked ``sk-`` must end within 256
    rng = random.Random("secdetb-blob")
    texts = [f"payload: {_draw_blob(rng, 400)}-sk-{_draw_blob(rng, n)}" for n in (300, 1_000, 5_000) for _ in range(20)]
    assert [t[:24] for t in texts if local_privacy.scan_secrets(t)] == []


def _draw_blob(rng: random.Random, n: int) -> str:
    return fx._draw(rng, fx.B64URL, n)


@pytest.mark.timing
@pytest.mark.parametrize("text", ("sk-" + "a" * 100_000, "xsk-" * 25_000, "-sk-" * 25_000, " sk-" * 25_000),
                         ids=("sk-a", "xsk-", "-sk-", "space-sk-"))
def test_the_door_is_linear_on_a_100k_sk_run(text):
    t0 = time.perf_counter()
    local_privacy.scan_secrets(text)
    assert time.perf_counter() - t0 < 0.5
