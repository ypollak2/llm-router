"""F2: quota status labels the legacy order; quota_score explain list logs at DEBUG."""
import asyncio


class _Rec:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def f(*a, **k):
            self.calls.append((name, a[0] if a else ""))
        return f


def test_quota_score_chain_explain_not_logged_at_info(monkeypatch):
    from llm_router import router

    rec = _Rec()
    monkeypatch.setattr(router, "log", rec)
    router._quota_score_chain(["codex/gpt-5.5", "gemini/gemini-2.5-flash"], "query")
    scored = [c for c in rec.calls if str(c[1]).startswith("quota_score")]
    assert scored, "quota_score line was not logged at all"
    assert all(level == "debug" for level, _ in scored)


HEADER = (
    "Order shown: legacy quota_balance order, not the live P2.3 routing "
    "(live QUOTA_BALANCED routing ranks with resource_map.score)."
)


def _status(monkeypatch, pressures):
    from llm_router import quota_balance
    from llm_router.tools import admin

    async def fake_pressures():
        return dict(pressures)

    monkeypatch.setattr(quota_balance, "get_provider_pressures", fake_pressures)
    return asyncio.run(admin.llm_quota_status())


def _line(out, prefix):
    hits = [ln for ln in out.splitlines() if ln.startswith(prefix)]
    assert len(hits) == 1, (prefix, out)
    return hits[0]


def test_llm_quota_status_labels_legacy_order(monkeypatch):
    out = _status(monkeypatch, {"codex": 0.2, "claude": 0.5, "gemini_cli": 0.4})
    lines = [ln for ln in out.splitlines() if ln.strip()]

    # F9 (1): the whole output is labelled as the legacy order, first line.
    assert lines[0] == HEADER

    # F9 (2): table header and first-place marker no longer claim live routing.
    assert _line(out, "Provider ").endswith("│ Legacy order")
    assert _line(out, "Codex ").endswith("│ 1st (legacy order)")
    assert "Route Priority" not in out
    assert "← next" not in out

    # F9 (2): balance line names the legacy preference, not a routing target.
    assert _line(out, "Balance:") == (
        "Balance: spread=30% — legacy order prefers CODEX (exceeds ±10% band)"
    )
    assert "routing to" not in out

    # F2: model chain line keeps its legacy label.
    assert _line(out, "Model chain ").startswith(
        "Model chain (legacy quota_balance order, not the P2.3 live score): codex "
    )


def test_llm_quota_status_in_band_has_no_preference(monkeypatch):
    out = _status(monkeypatch, {"codex": 0.40, "claude": 0.45, "gemini_cli": 0.42})
    assert [ln for ln in out.splitlines() if ln.strip()][0] == HEADER
    assert _line(out, "Balance:") == "Balance: spread=5% — all providers in ±10% band"
    assert "prefers" not in out
