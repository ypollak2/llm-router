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


def test_llm_quota_status_labels_legacy_order(monkeypatch):
    from llm_router import quota_balance
    from llm_router.tools import admin

    async def fake_pressures():
        return {"codex": 0.2, "claude": 0.5, "gemini_cli": 0.4}

    monkeypatch.setattr(quota_balance, "get_provider_pressures", fake_pressures)
    out = asyncio.run(admin.llm_quota_status())
    assert "legacy" in out.lower()
    assert "Model chain" in out
