"""Silent write failures must not multiply — T-14.

AST census of `src` + `scripts`: 1,046 broad excepts, 276 with a bare `pass`,
of which 84 wrapped a state mutation. A failed *write* at any of them reports
nothing — no error, no log, no counter. This is the structure that hid a real
`NameError` at `server.py:115`: found by ruff, invisible to 723 test files.

Eliminating all of them is not a remediation, it is a rewrite — most are
genuinely fail-open by design (a hook must never break routing). What this
file does is make the population VISIBLE and bounded:

* the four sites that lose money/ledger data now record a `failopen` counter,
  so "still fail-open" stops meaning "still silent";
* the total is ratcheted, so a new silent write cannot be added without either
  accounting for it or deliberately raising the number here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Sites counted at the time of the fix. LOWER THIS; do not raise it without a
#: reason in the commit message. 92 before the T-14 pass, 88 after.
MAX_SILENT_PERSISTENCE_SITES = 88


def _census_count() -> int:
    proc = subprocess.run(
        [sys.executable, "scripts/silent_mutation_census.py", "0"],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    first = proc.stdout.strip().split("\n")[0]
    return int(first.rsplit(":", 1)[1].strip())


def test_the_census_still_finds_the_population():
    """Anti-vacuity. A census returning 0 makes the ratchet below meaningless."""
    n = _census_count()
    assert n > 20, (
        f"the census found only {n} sites — it has stopped matching the source "
        "tree and the ratchet is no longer protecting anything"
    )


def test_silent_persistence_failures_have_not_multiplied():
    n = _census_count()
    assert n <= MAX_SILENT_PERSISTENCE_SITES, (
        f"{n} silent persistence sites, up from {MAX_SILENT_PERSISTENCE_SITES}. "
        "A new `except ...: pass` around a write means a failed write reports "
        "nothing. Either record a `failopen` counter in the handler, or raise "
        "this number deliberately and say why."
    )


@pytest.mark.parametrize("path, code", [
    ("src/llm_router/agentic/telemetry.py", "CHZ-FO-AGENTIC-TELEMETRY-WRITE"),
    ("src/llm_router/hooks/cc-usage-track.py", "CHZ-FO-HOOK-CC-USAGE-WRITE"),
    ("src/llm_router/hooks/auto-route.py", "CHZ-FO-HOOK-QUOTA-SNAPSHOT-WRITE"),
    ("src/llm_router/direct_diagnostics.py", "CHZ-FO-DIRECT-SAMPLE-WRITE"),
])
def test_the_data_losing_sites_now_account_for_the_loss(path, code):
    """The four where a swallowed write loses something a user would miss.

    Still fail-open — a hook must never break routing — but no longer silent.
    """
    src = (ROOT / path).read_text(encoding="utf-8")
    assert code in src, f"{path} no longer records {code} when its write fails"


def test_a_forced_write_failure_is_visible(tmp_path, monkeypatch):
    """The gate, exercised rather than grepped: break the write, see the counter.

    Observes `failopen.record` directly instead of reading the store back. The
    store is process-global and accumulates across a test session, so asserting
    on a snapshot makes this test depend on what ran before it — and a
    concurrency- or ordering-sensitive assertion about visibility would be a
    poor way to prove visibility.
    """
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    from llm_router import direct_diagnostics, failopen

    seen: list[str] = []
    monkeypatch.setattr(
        failopen, "record",
        lambda code, exc=None, **kw: seen.append(code),
    )

    def _explode(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", _explode)
    # Must not raise: the site is fail-OPEN, and that part must not change.
    direct_diagnostics.record_sample(2.5, timed_out=True)

    assert "CHZ-FO-DIRECT-SAMPLE-WRITE" in seen, (
        f"a write failure inside record_sample left no trace; recorded: {seen}"
    )


def test_the_accounted_sites_are_still_fail_open(tmp_path, monkeypatch):
    """Accounting for the loss must not start propagating it.

    These handlers exist because a diagnostic must never break the hook it runs
    in. Turning a silent swallow into a raised exception would be a worse
    regression than the one being fixed.
    """
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    from llm_router import direct_diagnostics

    def _explode(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", _explode)
    direct_diagnostics.record_sample(1.0, timed_out=False)   # must not raise
