"""SECDET: the exemption belongs to the token, never to the text around it; glued and percent-encoded prefixes are found.

One detector: the door (``local_privacy.scan_secrets``) and the scrubber (``secret_scrubber.scrub_text``) agree on every
case. Every secret is generated at run time (tests/secret_fixtures.py); nothing realistic is a literal.
"""
from __future__ import annotations

import random
import time

import pytest

from llm_router import local_privacy, secret_scrubber
from llm_router.secret_scrubber import scrub_text
from tests import secret_fixtures as fx

KINDS = ("anthropic_key", "openai_key", "github_token", "google_key", "aws_key_id", "slack_token")


def _cases(key: str) -> dict[str, str]:
    return {
        "plain": f"key {key}",
        "glued": f"mykey{key}",
        "glued_assign": f"token=x{key}",
        "example_host": f"see https://x.example/?token={key}",
        "example_host_header": f"curl https://api.corp.example/v1 -H 'x-api-key: {key}'",
        "env_example_label": f".env.example\nANTHROPIC_API_KEY={key}",
        "sample_label_same_line": f"copy config.sample then use {key}",
        "template_label": f"# .env.template\n\nreal one: {key}",
    }


@pytest.mark.parametrize("kind", KINDS)
def test_door_and_scrubber_agree_on_every_surrounding(kind):
    key = fx.real_shaped(kind, 3)
    for name, text in _cases(key).items():
        assert local_privacy.scan_secrets(text), (kind, name)
        assert key[8:] not in scrub_text(text), (kind, name)


@pytest.mark.parametrize("kind", ("anthropic_key", "openai_key", "openrouter_key", "github_token"))
def test_percent_encoded_prefix_is_found_and_scrubbed(kind):
    key = fx.real_shaped(kind, 4)
    enc = key.replace("-", "%2D").replace("_", "%5f")
    assert enc != key
    for text in (f"key {enc}", f"https://h/?k={enc}&x=1"):
        assert local_privacy.scan_secrets(text), (kind, text[:12])
        assert key[8:20] not in scrub_text(text) and enc[8:30] not in scrub_text(text), kind


def test_scrubber_leaves_a_percent_run_without_a_secret_untouched():
    text = "GET /a%20b?q=100%25+x%2Dy done"
    assert scrub_text(text) == text


def test_placeholders_next_to_a_label_stay_placeholders():
    # the exemption is the token's own placeholder look: these stay unrefused with or without a label
    for ph in ("sk-ant-" + "x" * 40, "ghp_" + "x" * 36, "sk-" + "your-key-here-" * 3, "sk-<paste-your-key-here-1234567890abcd>",
               "AKIA" + "IOSFODNN7" + "EXAMPLE"):
        for text in (f"key {ph}", f".env.example\nK={ph}", f"https://x.example/?token={ph}"):
            assert not local_privacy.scan_secrets(text), text


def test_label_exemption_counter_is_gone():
    key = fx.real_shaped("openai_key", 5)
    local_privacy.secret_counts.clear()
    local_privacy.scan_secrets(f".env.example\nK={key}")
    assert not any(k.startswith("exempted_by_label:") for k in local_privacy.secret_counts)
    local_privacy.secret_counts.clear()


# ---- repair 1 -------------------------------------------------------------------------------------------------
def _slug(rng: random.Random) -> str:
    alpha = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "-".join("".join(rng.choice(alpha) for _ in range(rng.randint(4, 9))) for _ in range(6)) + "-4"


def test_ordinary_words_ending_in_a_key_prefix_are_not_keys():
    rng = random.Random(11)
    shapes = []
    for word in "task disk risk ask desk mask brisk whisk flask bask tusk husk kiosk".split():
        slug = _slug(rng)
        shapes += [f"{word}-{slug}", f".{word}-card__item--active-3-{slug}", f"/usr/share/{word}-{slug}",
                   f"{word}-{fx.real_shaped('openai_key', 1)[8:24]}-{slug}"]
    alnum = "abcdefghijklmnopqrstuvwxyz0123456789"
    shapes += ["highp_" + "".join(rng.choice(alnum) for _ in range(40)),
               "MAIza" + "".join(rng.choice(alnum) for _ in range(40)),
               "XAKIA" + "".join(rng.choice("ABCDEFGH23456") for _ in range(16))]
    assert len(shapes) >= 55
    assert [t[:24] for t in shapes if local_privacy.scan_secrets(t)] == []


@pytest.mark.parametrize("kind", ("anthropic_key", "openai_key", "openrouter_key", "github_token", "slack_token",
                                  "google_key", "aws_key_id"))
def test_filler_glued_after_or_spliced_into_a_real_token_does_not_make_it_a_placeholder(kind):
    key = fx.real_shaped(kind, 7)
    texts = [key + "xxxxxxxx", key + "abcdefgh", key + "-example", key + "01234567"]
    if kind != "aws_key_id":  # an AWS id is upper-case + digits: lower-case filler spliced in breaks its shape (SECURITY.md)
        mid = len(key) // 2
        texts += [key[:mid] + "xxxxxxxx" + key[mid:], key[:mid] + "01234567" + key[mid:]]
    for text in texts:
        assert local_privacy.scan_secrets(text), (kind, text[-12:])
        assert scrub_text(text) != text


def _legacy_openai_key(seed: int) -> str:
    """``sk-`` + 48 base62, the pre-project OpenAI shape; drawn at run time like every fixture."""
    return "sk-" + fx._with_digit(fx._draw(random.Random(f"legacy-openai:{seed}"), fx.B62, 48))


@pytest.mark.parametrize("kind", KINDS + ("openrouter_key", "legacy_openai_key"))
def test_a_key_glued_after_letters_digits_underscore_or_cjk_is_found(kind):
    # requirement G; repair 1 lost the plain ``sk-`` (behind a word boundary) and AWS after a digit
    for seed in range(5):
        key = _legacy_openai_key(seed) if kind == "legacy_openai_key" else fx.real_shaped(kind, 20 + seed)
        for pre in ("mykey", "9", "123", "my_", "_", "\u952e", "\uff4b\uff45\uff59", "task"):
            assert local_privacy.scan_secrets(pre + key), (kind, pre)
            assert key[-16:] not in scrub_text(pre + key), (kind, pre)


@pytest.mark.parametrize("kind", ("github_token", "google_key"))
def test_text_glued_after_an_unglued_key_never_hides_it(kind):
    # repair 1 matched GitHub/Google bodies at exactly 36/35 characters, so one appended character hid a real key,
    # and the cls self-test fakes (``ghp_`` + 40, ``AIza`` + 36) stopped firing (tests/test_cls_dev_run.py red)
    key = fx.real_shaped(kind, 9)
    for text in (key + "Q", key + "Zq9AbC", f"key {key}Q7 tail", f"url=https://h/?k={key}AbCd&x=1"):
        assert local_privacy.scan_secrets(text), (kind, text[-10:])
        assert key[-16:] not in scrub_text(text), kind


def test_a_glued_jwt_and_bearer_are_found():
    from tests.test_lc12h_repair3 import real_jwt
    assert local_privacy.scan_secrets("x" + real_jwt())
    bearer = "authBearer " + fx.real_shaped("openai_key", 2)[3:]
    assert local_privacy.scan_secrets(bearer)


def test_the_scrubber_redacts_only_the_secret_span_of_a_percent_run():
    key = fx.real_shaped("anthropic_key", 2)
    url = "https://h/a%20b/c%2Fd?q=%26%3D&k=" + key.replace("-", "%2D") + "&z=%E2%82%AC"
    assert scrub_text(url) == "https://h/a%20b/c%2Fd?q=%26%3D&k=[REDACTED-ANTHROPIC_API_KEY]&z=%E2%82%AC"


def test_a_long_run_with_a_stray_percent_is_scrubbed_once_and_unchanged(monkeypatch):
    calls = []
    real = secret_scrubber._scrub_percent_run
    monkeypatch.setattr(secret_scrubber, "_scrub_percent_run", lambda run: calls.append(len(run)) or real(run))
    text = "x" * 300_000 + "%zz"
    assert scrub_text(text) == text
    assert calls == [len(text)]          # one pass over the run, no per-position restart


@pytest.mark.timing
@pytest.mark.parametrize("text", ("a-" * 50_000, "a.b-" * 25_000, "sk%2Dant-" * 12_000), ids=("a-", "a.b-", "pct-sk-ant"))
def test_scrubber_is_linear_on_a_long_scheme_like_run(text):
    # db_url_credentials had an unbounded scheme ``[a-z0-9+.-]*``: 100k chars of ``a-a-...`` took 23 s on main (200k: 93 s)
    t0 = time.perf_counter()
    scrub_text(text)
    assert time.perf_counter() - t0 < 3.0


@pytest.mark.timing
@pytest.mark.parametrize("n", (200_000, 1_000_000))
def test_scrubber_is_linear_on_a_long_run_with_a_stray_percent(n):
    text = "x" * n + "%zz"
    t0 = time.perf_counter()
    assert scrub_text(text) == text
    assert time.perf_counter() - t0 < 3.0       # main: 0.06 s at 1M; the quadratic version took 26 s at 200k
    t0 = time.perf_counter()
    assert not local_privacy.scan_secrets(text)
    assert time.perf_counter() - t0 < 3.0
