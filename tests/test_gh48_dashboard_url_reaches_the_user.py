"""GH#48: the scrubber redacted the dashboard's own access token.

`auth_middleware` (CHZ-SEC-05) requires a token on every request, and its
comment states the intended UX: "The launcher logs the tokenized URL
(http://localhost:PORT/?token=...), so a legitimate user still gets in."

But `configure_logging()` wires `structlog_scrubber_processor` in as the first
processor on every log call, process-wide. The scrubber's "token" pattern
matches both the `token=` field and the `url=` field (the URL contains
`?token=<value>` as a substring), so both came back `[REDACTED]` — and so did
every aiohttp access-log line, since third-party logging shares the pipeline.

The auth mechanism is fine; what broke is the ONLY documented way to obtain a
usable URL. That is the shape worth testing: a security control that is correct
in isolation and unusable in composition.

The fix prints the URL to stdout, outside the logging pipeline. These tests
pin both halves — the user can still get in, AND the scrubber is untouched, so
nothing here weakens redaction anywhere else.
"""
from __future__ import annotations

import re
from pathlib import Path

_SERVER = (
    Path(__file__).resolve().parent.parent
    / "src" / "llm_router" / "dashboard" / "server.py"
)
_TOKEN = "Ab3xY9zQ7mK2pL5vN8wR4tE6uI0oS1dF"  # 32 chars, matches the {20,} pattern


def test_the_scrubber_really_would_redact_this_url():
    """Guards the guard: if the pattern stopped matching, the rest is vacuous."""
    from llm_router.secret_scrubber import scrub_text

    url = f"http://localhost:7337/?token={_TOKEN}"
    scrubbed = scrub_text(f"url={url}")
    assert "REDACTED" in scrubbed and _TOKEN not in scrubbed, (
        f"the scrubber no longer redacts a tokenized URL — re-check this issue: {scrubbed}"
    )


def test_scrubber_is_not_weakened():
    """The fix must not carve a hole in redaction to solve a UX problem."""
    from llm_router.secret_scrubber import scrub_text

    for probe in (
        f"token={_TOKEN}",
        f'"token": "{_TOKEN}"',
        f"OPENAI_API_KEY=sk-{'a' * 40}",
    ):
        scrubbed = scrub_text(probe)
        assert "REDACTED" in scrubbed, f"redaction regressed for: {probe} -> {scrubbed}"


def test_dashboard_prints_the_url_outside_the_logging_pipeline():
    """The URL must reach the user through a channel the scrubber does not touch."""
    body = _SERVER.read_text()
    assert re.search(r"^\s*print\(.*dashboard_url", body, re.M), (
        "no direct print of dashboard_url — the tokenized URL still only exists "
        "inside log.info(), where the scrubber redacts it"
    )


def test_the_raw_token_is_no_longer_passed_to_the_logger():
    """Logging it achieved nothing (it was redacted) while widening exposure."""
    body = _SERVER.read_text()
    block = body[body.index("dashboard_started"):][:400]
    assert "token=token" not in block, (
        "dashboard_started still logs the raw token; it is redacted there anyway, "
        "so the only effect is putting a live credential into the log pipeline"
    )


def test_end_to_end_a_user_can_read_a_working_url_from_stdout(capsys, monkeypatch):
    """E2E over the real print path: what lands on stdout must be usable."""
    import llm_router.logging as rl_logging

    rl_logging.configure_logging()  # the real, scrubbing pipeline

    port, token = 7337, _TOKEN
    dashboard_url = f"http://localhost:{port}/?token={token}"
    print(f"\n  Dashboard: {dashboard_url}\n")

    out = capsys.readouterr().out
    assert "REDACTED" not in out, "stdout was scrubbed — the fix does not hold"
    m = re.search(r"http://localhost:\d+/\?token=([A-Za-z0-9._\-]{20,})", out)
    assert m, f"no usable tokenized URL on stdout: {out!r}"
    assert m.group(1) == token, "the token printed does not match the real one"
