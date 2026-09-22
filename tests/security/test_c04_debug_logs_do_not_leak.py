"""C-04 — trace and intercept logs must not write secrets, and must not be readable.

`secret_scrubber.scrub_text` declares itself "the single source of truth every
content store should call" (CHZ-SEC-01). The 2026-09-21 audit found two writers
with **zero** references to it:

    trace.py              truncated to 600 chars and wrote the value verbatim
    hooks/tool_intercept  truncated to 200 chars and wrote the raw shell command

An injection test drove six secret classes through `trace.emit()` and all six
landed on disk in full plaintext. `intercepts.jsonl` was found on the audited
machine at **mode 644, 177 rows, written during the audit itself, with no TTL**.

The hardening already existed elsewhere in this repo and had simply not been
carried across: `auto-route.py` writes its transcript shards 0600 through
`paths.private_opener`, whose docstring measures why `open()`-then-`chmod` leaves
a window where the file is world-readable.

Two ordering facts these tests pin, because both are easy to get wrong:

  * **Scrub before clipping.** Truncating first can sever a key mid-token so the
    pattern no longer matches, and the prefix is written anyway. A truncated key
    is still a leaked key.
  * **Create 0600, do not chmod afterwards.** Permissions are checked at open
    time, so anything that opened the file during the 0644 window keeps a
    readable handle after the chmod.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

# Assembled at runtime rather than written as literals: GitHub push protection
# blocks a commit containing a token-shaped string, test fixture or not. This
# repo has already had two releases blocked that way.
def _fixture(prefix: str, body: str) -> str:
    return prefix + body


# CREDENTIALS -- what `secret_scrubber` actually covers, and what C-04 is about.
# These leaked because trace.py never called the scrubber, not because the
# scrubber lacked a pattern.
SECRETS = {
    "anthropic": _fixture("sk-ant-api03-", "A" * 40),
    "aws_key_id": _fixture("AKIA", "IOSFODNN7EXAMPLE"),
    "password_kv": _fixture("password=", "hunter2correcthorse"),
    "bearer": _fixture("Bearer ", "ZXhhbXBsZS10b2tlbi12YWx1ZQ"),
}

# PII -- a DIFFERENT finding, surfaced while fixing C-04.
#
# The audit reported "six secret types survived in full plaintext". Four did,
# because trace.py bypassed the scrubber. The other two survive because
# `secret_scrubber` has **no pattern for them at all** -- it is a credential
# scrubber and has never claimed otherwise. Verified directly:
#
#     scrub_text("alice@internal-example.com") -> unchanged
#     scrub_text("203.0.113.42")               -> unchanged
#
# That is not a trace.py bypass. It affects every one of the scrubber's six
# consumers -- result_cache, semantic_cache, session_store, context, envelope,
# prompt_capture -- so widening it here would be the wrong place and the wrong
# blast radius. Recorded, pinned below, not silently folded into C-04.
PII = {
    "email": _fixture("alice@", "internal-example.com"),
    "public_ip": _fixture("203.0.", "113.42"),
}


@pytest.fixture
def trace_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_TRACE", "1")
    monkeypatch.delenv("LLM_ROUTER_TRACE_FILE", raising=False)
    return tmp_path


def test_trace_scrubs_every_injected_secret(trace_env):
    """The audit's six injected classes must not survive to disk."""
    from llm_router import trace

    for name, secret in SECRETS.items():
        trace.emit("audit.c04", value=f"leading context {secret} trailing context")

    path = trace.trace_path()
    assert path.exists(), "trace wrote nothing — this test would pass vacuously"
    written = path.read_text(encoding="utf-8")

    survived = [n for n, sec in SECRETS.items() if sec in written]
    assert not survived, (
        f"{len(survived)} of {len(SECRETS)} secret classes reached trace.jsonl "
        f"in plaintext: {survived}"
    )


def test_trace_file_is_created_private(trace_env):
    """0600 at creation, not 0644-then-chmod."""
    from llm_router import trace

    trace.emit("audit.c04", value="anything")
    mode = stat.S_IMODE(trace.trace_path().stat().st_mode)
    assert mode == 0o600, f"trace.jsonl created mode {oct(mode)}, expected 0o600"


def test_trace_scrubs_before_clipping(trace_env):
    """A secret past the 600-char clip must still be scrubbed, not truncated into.

    If clipping ran first, the tail would be cut and whatever prefix survived
    would be written unscrubbed.
    """
    from llm_router import trace

    secret = SECRETS["anthropic"]
    trace.emit("audit.c04", value="x" * 590 + secret)

    written = trace.trace_path().read_text(encoding="utf-8")
    assert secret not in written
    assert secret[:20] not in written, (
        "a truncated prefix of the key survived — clipping ran before scrubbing"
    )


def test_trace_scrubs_inside_nested_structures(trace_env):
    """Dicts and lists are json-dumped; the dump must be scrubbed too."""
    from llm_router import trace

    trace.emit("audit.c04", value={"headers": {"authorization": SECRETS["bearer"]}})
    written = trace.trace_path().read_text(encoding="utf-8")
    assert SECRETS["bearer"] not in written


def test_intercept_log_scrubs_and_is_private(tmp_path, monkeypatch):
    """The live one. Mode 644 with raw shell commands, found in use."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    import importlib

    ti = importlib.import_module("llm_router.hooks.tool_intercept")

    secret = SECRETS["bearer"]
    ti._log_intercept("bash", f'curl -H "Authorization: {secret}" https://example.com', 100, 10)

    path = tmp_path / "intercepts.jsonl"
    assert path.exists(), "nothing was written — this test would pass vacuously"

    written = path.read_text(encoding="utf-8")
    assert secret not in written, "raw bearer token reached intercepts.jsonl"

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"intercepts.jsonl created mode {oct(mode)}, expected 0o600"

    # still a usable record, not just an empty one
    row = json.loads(written.strip().splitlines()[-1])
    assert row["kind"] == "bash" and row["saved_tokens"] == 90


def test_intercept_repairs_a_legacy_world_readable_file(tmp_path, monkeypatch):
    """An existing 0644 file from an older version must be tightened on next write."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    import importlib

    ti = importlib.import_module("llm_router.hooks.tool_intercept")

    path = tmp_path / "intercepts.jsonl"
    path.write_text('{"legacy": true}\n', encoding="utf-8")
    os.chmod(path, 0o644)

    ti._log_intercept("bash", "ls -la", 50, 20)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600, (
        "a pre-existing world-readable intercepts.jsonl was left at 0644"
    )


def test_both_logs_are_swept_by_gc():
    """Neither log had any retention. `gc` must now recognise both."""
    from llm_router.commands import gc

    assert "trace.jsonl" in gc.SWEEPABLE_LOGS
    assert "intercepts.jsonl" in gc.SWEEPABLE_LOGS


def test_gc_actually_collects_a_stale_log(tmp_path):
    """Membership in a set is not sweeping. Prove `collect_stale` returns it."""
    from llm_router.commands import gc

    old = tmp_path / "trace.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    ancient = 1_000_000.0
    os.utime(old, (ancient, ancient))

    stale = gc.collect_stale(tmp_path, ttl_days=7)
    assert old in stale, f"stale trace.jsonl not collected: {stale}"


def test_scrubber_failure_withholds_rather_than_writes_raw(trace_env, monkeypatch):
    """Fail closed. A debugging aid is not worth a credential on disk."""
    from llm_router import trace

    import llm_router.secret_scrubber as ss

    def _boom(_text):
        raise RuntimeError("scrubber unavailable")

    monkeypatch.setattr(ss, "scrub_text", _boom)

    trace.emit("audit.c04", value=SECRETS["anthropic"])
    written = trace.trace_path().read_text(encoding="utf-8")
    assert SECRETS["anthropic"] not in written
    assert "SCRUB-FAILED" in written


def test_fixtures_are_recognisable_to_the_scrubber():
    """Anti-vacuity: the scrubber must actually match these fixtures.

    If `scrub_text` had no pattern for one of them, the leak tests above would
    pass for that class because the value never needed scrubbing in the first
    place. This check is what separated the four real C-04 leaks from the two
    PII coverage gaps below.
    """
    from llm_router.secret_scrubber import scrub_text

    unmatched = [n for n, sec in SECRETS.items() if scrub_text(sec) == sec]
    assert not unmatched, (
        f"the scrubber does not recognise these fixtures, so the leak tests above "
        f"prove nothing for them: {unmatched}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "secret_scrubber is a credential scrubber and has no PII patterns. "
        "Surfaced while fixing C-04; affects all six scrubber consumers, not "
        "just trace.py. Strict so that adding PII coverage FAILS this test and "
        "forces it to be promoted to a real assertion rather than left stale."
    ),
)
def test_pii_is_not_yet_scrubbed_anywhere():
    """Pins the known gap so it cannot be forgotten or quietly discovered twice."""
    from llm_router.secret_scrubber import scrub_text

    for name, value in PII.items():
        assert scrub_text(value) != value, f"{name} is now scrubbed"
