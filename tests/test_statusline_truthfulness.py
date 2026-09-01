"""The statusline must never present a placeholder as a measurement.

Audited live on 2026-09-01. The line read `🤖 50%/5h 50%/wk°` while the API,
queried one command later, returned 2% and 24%. The file on disk held
`{"session_pct": 50, "weekly_pct": 50, "sonnet_pct": 50, "is_fallback": true}` —
three identical 50s being the placeholder session-start.py writes when the OAuth
fetch fails.

`is_fallback` had exactly one consumer: session-start.py's own banner. Every
other reader — the statusline, load_pressure(), the session summary — took the
number at face value. A user pacing work against a five-hour window was shown
50% consumed when the truth was 2%.

The `°` marker did appear, but it means "stale", not "invented", and those are
different claims.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
STATUSLINE = REPO / "src" / "llm_router" / "hooks" / "statusline-command.sh"


def _src() -> str:
    return STATUSLINE.read_text()


# ── the fabricated-quota bug ──────────────────────────────────────────────────


def test_statusline_checks_the_fallback_flag():
    """Reading session_pct without checking is_fallback is the whole defect."""
    src = _src()
    quota_block = src.split("🤖 Claude subscription usage")[1].split("⏰")[0]
    assert "is_fallback" in quota_block, (
        "the quota segment reads session_pct without checking is_fallback, so a "
        "failed OAuth fetch renders 50% as though it were measured"
    )


def test_load_pressure_rejects_a_fallback_snapshot():
    """The same rule in the Python surface.

    load_pressure() was added for the missing-file case and did not cover the
    failed-fetch case, which is the more common one.
    """
    from llm_router.ui import status_premium as sp

    cmd = sp.PremiumStatusCommand()

    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "usage.json"
        p.write_text(json.dumps({
            "session_pct": 50, "weekly_pct": 50, "sonnet_pct": 50,
            "is_fallback": True, "updated_at": 9e9,
        }))
        cmd.usage_json = p
        assert cmd.load_pressure() is None, (
            "a snapshot flagged is_fallback was accepted as real pressure"
        )


def test_load_pressure_accepts_a_real_snapshot():
    from llm_router.ui import status_premium as sp
    import tempfile

    cmd = sp.PremiumStatusCommand()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "usage.json"
        p.write_text(json.dumps({
            "session_pct": 2.0, "weekly_pct": 24.0, "is_fallback": False,
            "updated_at": 9e9,
        }))
        cmd.usage_json = p
        pressure = cmd.load_pressure()
        assert pressure is not None
        assert pressure["session_pct"] == 2.0


def test_a_missing_flag_still_means_real():
    """The refresh hook's success path omits the key entirely.

    Defaulting to "assume fallback" would blank the quota for every healthy
    install, so absence must mean measured — matching session-start.py's own
    reader, which uses get("is_fallback", False).
    """
    from llm_router.ui import status_premium as sp
    import tempfile

    cmd = sp.PremiumStatusCommand()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "usage.json"
        p.write_text(json.dumps({"session_pct": 2.0, "weekly_pct": 24.0}))
        cmd.usage_json = p
        assert cmd.load_pressure() is not None


# ── one definition of stale ───────────────────────────────────────────────────


def test_staleness_uses_one_clock():
    """`°` compared updated_at against 300s while the health glyph at the other
    end of the same line compared file mtime against 1800s: two clocks, a 6x
    threshold gap, one file, one render."""
    src = _src()
    # Only the health probe's own python block — `getmtime` is used legitimately
    # further down to find the newest transcript file, which is a different file
    # and a different question.
    probe = src.split("Health (mirrors")[1].split("' 2>/dev/null)")[0]

    # `updated_at` must be PREFERRED. mtime survives only as the fallback for
    # snapshots written before that field existed — otherwise an old file would
    # read as infinitely stale, and mtime remains a usable test control.
    assert "updated_at" in probe, (
        "health still derives usage staleness from file mtime alone; it must "
        "prefer updated_at like the ° marker does, so the two ends of the line "
        "cannot contradict each other"
    )
    assert probe.index("updated_at") < probe.index("getmtime"), (
        "mtime is consulted before updated_at, so the two clocks still disagree "
        "whenever both are available"
    )

    # And the ° marker must be reading the same field.
    quota = src.split("🤖 Claude subscription usage")[1].split("Quota reset")[0]
    assert "updated_at" in quota


def test_health_treats_a_fallback_as_not_ok():
    """A green check beside an invented number is the worst combination."""
    src = _src()
    health_block = src.split("Health (mirrors")[1]
    assert "is_fallback" in health_block, (
        "health reports ok while the quota it sits beside is a placeholder"
    )


# ── refresh must not fail silently ────────────────────────────────────────────


def _refresh_src() -> str:
    return (REPO / "src" / "llm_router" / "hooks" / "usage-refresh.py").read_text()


def test_refresh_records_why_it_failed():
    """`except Exception: return` leaves stale data and says nothing.

    Observed: the file sat 148 minutes old carrying the placeholder. Running the
    refresh by hand fixed it instantly, so the mechanism works — it simply had
    no way to report that it had not run.
    """
    src = _refresh_src()
    assert "last_refresh_error" in src, (
        "refresh failures are still swallowed with no record, so a permanently "
        "stale quota is indistinguishable from a quiet one"
    )


def test_refresh_backs_off_on_rate_limit():
    """The statusline fires a refresh on every render past TTL, throttled to
    60s. Against a 429 that is a retry loop which can never succeed."""
    src = _refresh_src()
    assert "429" in src, "no rate-limit handling in the refresh path"
    assert "Retry-After" in src or "retry_after" in src, (
        "a 429 is retried on the same schedule as any other failure"
    )


# ── the dead reset segment ────────────────────────────────────────────────────


def test_no_segment_reads_a_key_nothing_writes():
    """The ⏰ segment read session_resets_at, which no writer produces.

    Twenty lines that have never executed their happy path. Either the refresh
    stops discarding the field or the branch goes; a permanently dead branch in
    a file this heavily commented misleads the next reader.
    """
    # Strip comments: the removal left an explanatory block naming the key, and
    # a comment mentioning a field is not a read of it.
    code = "\n".join(
        line for line in _src().splitlines() if not line.lstrip().startswith("#")
    )

    if "session_resets_at" not in code:
        return  # branch removed — the resolution actually taken

    writers = (REPO / "src" / "llm_router" / "hooks" / "usage-refresh.py").read_text()
    session_start = (REPO / "src" / "llm_router" / "hooks" / "session-start.py").read_text()
    produced = re.search(r'"session_resets_at"\s*:', writers) or re.search(
        r'"session_resets_at"\s*:', session_start
    )
    assert produced, (
        "the statusline reads session_resets_at but neither usage-refresh.py "
        "nor session-start.py ever writes it, so the ⏰ segment cannot render"
    )


def test_the_layout_comment_matches_what_can_render():
    """The header comment must describe the segments that actually exist.

    It listed ⏰ throughout the period the segment could not fire, so the file's
    own documentation asserted a feature no user had ever seen. Now that
    usage-refresh.py persists the timestamp the segment works — so the header
    listing it is correct, and this test holds the two in step either way.
    """
    src = _src()
    header = src.split("\n\n")[0]
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    renders_reset = "session_resets_at" in code

    assert ("⏰" in header) == renders_reset, (
        f"the layout comment {'lists' if '⏰' in header else 'omits'} ⏰ while the "
        f"segment {'exists' if renders_reset else 'does not exist'}"
    )


# ── backoff arithmetic ────────────────────────────────────────────────────────


def _refresh_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "usage_refresh_under_test",
        REPO / "src" / "llm_router" / "hooks" / "usage-refresh.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_zero_retry_after_still_backs_off():
    """Observed live: this endpoint returns `Retry-After: 0` on a 429.

    Honouring that literally gave a one-second backoff — which is no backoff at
    all, from a caller the statusline fires on every render past TTL. A 429
    means stop asking; a server saying otherwise in the same breath does not
    change that.
    """
    m = _refresh_module()
    assert m._parse_retry_after("0") >= m._MIN_BACKOFF_S
    assert m._parse_retry_after(0) >= m._MIN_BACKOFF_S


def test_a_missing_retry_after_uses_the_default():
    m = _refresh_module()
    assert m._parse_retry_after(None) == m._DEFAULT_BACKOFF_S


def test_a_wild_retry_after_cannot_blank_quota_for_hours():
    m = _refresh_module()
    assert m._parse_retry_after("999999") <= m._MAX_BACKOFF_S


def test_a_sane_retry_after_is_honoured():
    m = _refresh_module()
    assert m._parse_retry_after("300") == 300


def test_refresh_persists_the_reset_timestamp():
    """The field existed all along — session-end.py has always read
    data["five_hour"]["resets_at"]. This hook dropped it, which is why four
    surfaces consumed `session_resets_at` and none could ever get it."""
    src = (REPO / "src" / "llm_router" / "hooks" / "usage-refresh.py").read_text()
    assert '"session_resets_at"' in src, (
        "usage-refresh.py still discards the reset timestamp the endpoint returns"
    )
    assert "resets_at" in src


def test_session_end_rejects_a_fallback_snapshot():
    """The session summary reported `quota used 5h 50%/wk 50%` from the
    placeholder — the same bug as the statusline, in a third surface."""
    src = (REPO / "src" / "llm_router" / "hooks" / "session-end.py").read_text()
    block = src.split("def _get_cc_usage")[1].split("def ")[0]
    assert "is_fallback" in block, (
        "_get_cc_usage returns the cached snapshot without checking is_fallback, "
        "so the session summary quotes 50% as a measurement"
    )
