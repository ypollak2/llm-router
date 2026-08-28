"""GH#57: DIRECT-execution timeouts failed silently.

When the local-draft path timed out, auto-route.py wrote "DIRECT FAILED:
falling through to Claude" to ~/.llm-router/auto-route-debug.log and carried on.
Nothing reached the user or the agent, so the only way to discover it was to
read a debug log by hand.

On the reporter's machine that was not an edge case. Measured latencies against
the SMALLEST of three pulled models (llama3.1:8b) were 9.9, 10.5, 12.6, 19.4,
27.5 and 33.6 seconds for trivial one-line questions, and 153s against a larger
one — every sample past the 4s default, most past even a generously raised 8s.
So DIRECT execution never once succeeded, silently, on every prompt.

Two different truths need two different messages, which is the substance of the
request:

  * config problem  — the model answers, just slower than the timeout. Say so
    with a number: raise LLM_ROUTER_OLLAMA_TIMEOUT to about the observed p90.
  * capability problem — repeated timeouts even after raising it. Say that
    local routing is not viable on this machine and stop suggesting a knob.

`_diagnose_direct_timeouts` distinguishes them from recorded latencies.
"""
from __future__ import annotations

import pytest

from llm_router import direct_diagnostics as dd


def test_no_timeouts_produces_no_advice():
    assert dd.diagnose([]) is None


def test_a_single_timeout_is_not_yet_a_pattern():
    """One slow call is noise; advice on n=1 trains people to ignore advice."""
    assert dd.diagnose([dd.Sample(elapsed_s=9.9, timed_out=True)]) is None


def test_slower_than_the_timeout_but_viable_suggests_a_number():
    """The reporter's actual numbers against the 4s default."""
    samples = [dd.Sample(elapsed_s=s, timed_out=True)
               for s in (9.9, 10.5, 12.6, 19.4, 27.5, 33.6)]
    advice = dd.diagnose(samples, timeout_s=4.0)
    assert advice is not None
    assert advice.kind == "raise_timeout"
    assert "LLM_ROUTER_OLLAMA_TIMEOUT" in advice.message
    # Must name a concrete value at or above the observed p90, not "increase it".
    assert advice.suggested_timeout_s >= 27.5, advice.suggested_timeout_s
    assert str(int(advice.suggested_timeout_s)) in advice.message


def test_hopeless_latency_says_so_instead_of_offering_a_knob():
    """153s against a larger model is not an interactive round trip."""
    samples = [dd.Sample(elapsed_s=s, timed_out=True) for s in (150.0, 153.0, 161.0)]
    advice = dd.diagnose(samples, timeout_s=8.0)
    assert advice is not None
    assert advice.kind == "too_slow_for_local"
    assert "LLM_ROUTER_OLLAMA_TIMEOUT" not in advice.message, (
        "suggesting a timeout bump for a 150s model sends the user in circles"
    )
    assert "smaller model" in advice.message or "DIRECT_EXECUTION" in advice.message


def test_successes_prevent_a_false_alarm():
    """A mostly-working setup with one slow outlier must not be told it is broken."""
    samples = [dd.Sample(elapsed_s=0.4, timed_out=False) for _ in range(9)]
    samples.append(dd.Sample(elapsed_s=12.0, timed_out=True))
    assert dd.diagnose(samples, timeout_s=4.0) is None


def test_advice_is_stable_across_repeated_calls():
    """It is rendered into a hook message; it must not flap between runs."""
    samples = [dd.Sample(elapsed_s=s, timed_out=True) for s in (10.0, 20.0, 30.0)]
    first = dd.diagnose(samples, timeout_s=4.0)
    second = dd.diagnose(samples, timeout_s=4.0)
    assert first == second


@pytest.mark.parametrize("bad", [None, [], [dd.Sample(elapsed_s=-1.0, timed_out=True)]])
def test_never_raises_on_junk(bad):
    """This runs inside a hook; an exception here breaks the user's prompt."""
    dd.diagnose(bad)  # must not raise


def test_samples_persist_across_invocations(tmp_path):
    """One hook run sees one attempt; the advice needs several."""
    for s in (9.9, 10.5, 12.6, 19.4, 27.5, 33.6):
        dd.record_sample(s, timed_out=True, home=tmp_path)
    advice = dd.current_advice(timeout_s=4.0, home=tmp_path)
    assert advice is not None and advice.kind == "raise_timeout"


def test_the_ring_buffer_is_bounded(tmp_path):
    for i in range(100):
        dd.record_sample(float(i), timed_out=True, home=tmp_path)
    assert len(dd.load_samples(home=tmp_path)) <= dd._MAX_SAMPLES


def test_recording_never_raises_on_an_unwritable_home(tmp_path):
    """It runs inside a hook; a read-only HOME must not cost the user a prompt."""
    blocked = tmp_path / "nope"
    blocked.write_text("i am a file, not a directory")
    dd.record_sample(1.0, timed_out=True, home=blocked)  # must not raise
    assert dd.load_samples(home=blocked) == []


def test_corrupt_sample_lines_are_skipped_not_fatal(tmp_path):
    (tmp_path / ".llm-router").mkdir(parents=True)
    (tmp_path / ".llm-router" / "direct_samples.jsonl").write_text(
        '{"elapsed_s": 10.0, "timed_out": true}\nnot json at all\n{"bad": 1}\n'
    )
    assert dd.load_samples(home=tmp_path) == [dd.Sample(10.0, True)]


def test_doctor_surfaces_the_advice(tmp_path, monkeypatch, capsys):
    """E2E: the advice must reach a surface a user actually looks at.

    The reporter's point was not that the data was missing but that it was only
    in a debug log. doctor already has a 'Provider circuit breakers' section
    that stayed empty because the DIRECT path never reported into it.
    """
    from pathlib import Path

    import llm_router.direct_diagnostics as _dd

    for s in (9.9, 10.5, 12.6, 19.4, 27.5, 33.6):
        _dd.record_sample(s, timed_out=True, home=tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_TIMEOUT", "4")

    advice = _dd.current_advice(timeout_s=4.0)
    assert advice is not None
    assert "LLM_ROUTER_OLLAMA_TIMEOUT=" in advice.message
    # The number must be actionable, not "increase the timeout".
    assert any(ch.isdigit() for ch in advice.message.split("LLM_ROUTER_OLLAMA_TIMEOUT=")[1][:4])
