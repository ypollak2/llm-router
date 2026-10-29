"""SECDET: the exemption belongs to the token, never to the text around it; glued and percent-encoded prefixes are found.

One detector: the door (``local_privacy.scan_secrets``) and the scrubber (``secret_scrubber.scrub_text``) agree on every
case. Every secret is generated at run time (tests/secret_fixtures.py); nothing realistic is a literal.
"""
from __future__ import annotations

import pytest

from llm_router import local_privacy
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
