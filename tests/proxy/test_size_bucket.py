"""P1: prompt-size bucket on local-decision ledger rows and the stats cut."""
import pytest

from llm_router.proxy import ledger


@pytest.mark.parametrize("est,label", [
    (0, "<=8k"), (8_000, "<=8k"), (8_001, "8-25k"), (25_000, "8-25k"), (25_001, "25-32k"),
    (32_768, "25-32k"), (32_769, "32-64k"), (65_536, "32-64k"), (65_537, "64-128k"),
    (131_072, "64-128k"), (131_073, ">128k"), (10_000_000, ">128k"), (None, "unknown"),
])
def test_bucket_boundaries(est, label):
    assert ledger.size_bucket(est) == label


def _row(decision, bucket=None, reason=None):
    r = {"decision": decision, "reason": reason}
    if bucket:
        r["size_bucket"] = bucket
    return r


def test_stats_cut_and_old_rows():
    rows = [_row("served", "<=8k"), _row("served", "<=8k"), _row("forwarded", "<=8k"),
            _row("fallback", "32-64k"), _row("served", "32-64k"),
            _row("served"), _row("forwarded")]  # old rows: no field
    sb = ledger.stats(rows)["size_buckets"]
    assert list(sb) == ["<=8k", "8-25k", "25-32k", "32-64k", "64-128k", ">128k", "unknown"]
    assert sb["<=8k"] == {"calls": 3, "served": 2, "fallbacks": 0, "routed_share": 0.6667}
    assert sb["32-64k"] == {"calls": 2, "served": 1, "fallbacks": 1, "routed_share": 0.5}
    assert sb["unknown"] == {"calls": 2, "served": 1, "fallbacks": 0, "routed_share": 0.5}
    assert sb["8-25k"]["routed_share"] is None
    assert sum(b["calls"] for b in sb.values()) == len(rows)


def test_loop_guard_not_counted_served_and_unrecognised_label_is_unknown():
    rows = [_row("served", "<=8k", reason=ledger.REASON_LOOP_GUARD), _row("served", "bogus")]
    sb = ledger.stats(rows)["size_buckets"]
    assert sb["<=8k"]["served"] == 0
    assert sb["unknown"]["calls"] == 1


def test_format_stats_shows_buckets():
    out = ledger.format_stats(ledger.stats([_row("served", "<=8k"), _row("forwarded", "8-25k"), _row("served")]))
    assert "routed share by est. prompt size: <=8k 1/1 (100.0%); 8-25k 0/1 (0.0%); unknown 1/1 (100.0%)" in out


def test_empty_stats():
    assert all(b["calls"] == 0 for b in ledger.stats([])["size_buckets"].values())
    assert "no calls" in ledger.format_stats(ledger.stats([]))
