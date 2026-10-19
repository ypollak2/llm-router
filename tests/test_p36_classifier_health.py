"""P3.6-b: rules-vs-kNN agreement with n, the PSI line, and the conservative-mode trigger. Synthetic rows."""
import json

import pytest

from llm_router import classifier_health as ch

NOW = 2_000_000_000.0
DAY = 86400.0


def rows(n, agree, age_days, *, rules="moderate"):
    """n rules-vs-kNN rows, ``agree`` of them on the same side of the frontier boundary, ``age_days`` old.
    ``rules`` below the boundary agrees with knn "standard"; a disagreeing row says "frontier"."""
    other = "standard" if rules not in ch._FRONTIER_TIERS else "frontier"
    flip = "frontier" if other == "standard" else "standard"
    return [{"ts": int(NOW - age_days * DAY - i), "rules_tier": rules,
             "knn_tier": other if i < agree else flip, "source": "proxy"} for i in range(n)]


def shadow_rows(n, agree, age_days, *, rules="sonnet", other="opus"):
    """The killed LLM classifier's shadow rows (fallback source only)."""
    return [{"kind": "classifier_shadow", "ts": NOW - age_days * DAY - i, "source": "llm",
             "rules": {"tier": rules}, "llm": {"tier": rules if i < agree else other}} for i in range(n)]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv(ch.MODE_ENV, raising=False)


def test_trigger_fires_and_notifies_at_n_100():
    recs = rows(100, 80, 1) + rows(300, 270, 10)  # 80% now vs 90% before: -10 pts
    up = ch.evaluate_and_update(recs, now=NOW)
    assert up.active and up.changed == "entered" and up.health.drop_pts == pytest.approx(10.0)
    assert "conservative mode ON" in up.notice and "n=100" in up.notice and "\n" not in up.notice
    assert ch.is_active(now=NOW)
    assert json.loads(ch.state_path().read_text())["active"] is True


def test_exactly_five_points_triggers_four_does_not():
    assert ch.evaluate_and_update(rows(100, 85, 1) + rows(100, 90, 10), now=NOW).active
    ch.state_path().unlink()
    assert not ch.evaluate_and_update(rows(100, 86, 1) + rows(100, 90, 10), now=NOW).active


def test_does_not_fire_below_n_100():
    recs = rows(99, 40, 1) + rows(300, 270, 10)  # a 50-pt drop on 99 rows
    up = ch.evaluate_and_update(recs, now=NOW)
    assert not up.active and up.changed is None and up.notice is None and not up.health.measurable
    assert not ch.state_path().exists() and not ch.is_active(now=NOW)
    # and the baseline side: thin baseline never triggers either
    assert not ch.evaluate_and_update(rows(300, 100, 1) + rows(99, 99, 10), now=NOW).active


def test_thin_data_does_not_switch_an_active_mode_off():
    ch.evaluate_and_update(rows(100, 80, 1) + rows(300, 270, 10), now=NOW)
    up = ch.evaluate_and_update(rows(10, 10, 1) + rows(300, 270, 10), now=NOW + 1)
    assert up.active and ch.is_active(now=NOW + 1)


def test_recovery_switches_off():
    ch.evaluate_and_update(rows(100, 80, 1) + rows(300, 270, 10), now=NOW)
    up = ch.evaluate_and_update(rows(100, 90, 1) + rows(300, 270, 10), now=NOW + 1)
    assert not up.active and up.changed == "exited" and "conservative mode off" in up.notice
    assert not ch.is_active(now=NOW + 1)


def test_state_expires_and_env_off_drops_the_bias(monkeypatch):
    ch.evaluate_and_update(rows(100, 80, 1) + rows(300, 270, 10), now=NOW)
    assert not ch.is_active(now=NOW + ch.STATE_MAX_AGE_S + 1)
    monkeypatch.setenv(ch.MODE_ENV, "off")
    assert not ch.is_active(now=NOW)


def test_windows_do_not_overlap_and_ignore_non_answers():
    recs = rows(100, 100, 1) + rows(100, 0, 40)  # older than 35 days: in neither window
    h = ch.compute(recs, now=NOW)
    assert (h.recent.n, h.baseline.n) == (100, 0) and h.drop_pts is None and not h.measurable
    junk = [{"ts": NOW, "rules_tier": "moderate", "knn_tier": "maybe"}, {"ts": NOW, "kind": "other"},
            {"ts": NOW, "rules_tier": 3, "knn_tier": "frontier"}]
    assert ch.compute(junk, now=NOW).recent.n == 0


def test_agreement_is_the_frontier_boundary_not_tier_equality():
    recs = [{"ts": NOW - 5, "rules_tier": t, "knn_tier": k} for t, k in (
        ("simple", "standard"), ("moderate", "standard"), ("complex", "frontier"),
        ("deep_reasoning", "frontier"), ("simple", "frontier"), ("complex", "standard"))]
    h = ch.compute(recs, now=NOW)
    assert (h.recent.n, h.recent.agree) == (6, 4) and h.source == ch.SOURCE_KNN


def test_record_agreement_writes_a_text_free_row_and_compute_reads_it():
    assert ch.record_agreement("complex", False, now=NOW - 60)
    assert ch.record_agreement(type("C", (), {"value": "simple"})(), False, now=NOW - 30)
    assert not ch.record_agreement(None, True)
    raw = ch.agreement_path().read_text()
    assert set(json.loads(raw.splitlines()[0])) == {"ts", "rules_tier", "knn_tier", "source"}
    h = ch.compute(None, now=NOW)
    assert (h.recent.n, h.recent.agree, h.source) == (2, 1, ch.SOURCE_KNN)


def test_off_with_no_rows_reads_not_measured_knn_off(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_COMPLEXITY_KNN", raising=False)
    h = ch.compute(None, now=NOW)
    assert ch.lines(h)[0] == "classifier agreement (rules vs kNN): not measured (kNN off, n=0)"
    monkeypatch.setenv("LLM_ROUTER_COMPLEXITY_KNN", "on")
    assert "not measured (n=0 in the last 35 days)" in ch.lines(ch.compute(None, now=NOW))[0]


def test_llm_shadow_file_is_only_a_labelled_fallback(monkeypatch):
    from llm_router.proxy import llm_shadow

    sp = llm_shadow.shadow_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("\n".join(json.dumps(r) for r in shadow_rows(5, 4, 1)) + "\n")
    monkeypatch.setattr("time.time", lambda: NOW)
    h = ch.compute(None, now=NOW)
    assert h.source == ch.SOURCE_FALLBACK and (h.recent.n, h.recent.agree) == (5, 4)
    assert "LLM-shadow fallback" in ch.lines(h)[0]
    ch.record_agreement("moderate", False, now=NOW - 5)  # a kNN row wins over the fallback
    assert ch.compute(None, now=NOW).source == ch.SOURCE_KNN
    sp.unlink()
    ch.agreement_path().unlink()
    assert ch.compute(None, now=NOW).recent.n == 0  # no file, no fallback


def test_local_counts_as_haiku():
    recs = [{"kind": "classifier_shadow", "ts": NOW - 5, "source": "llm", "rules": {"tier": "haiku"},
             "llm": {"tier": "local"}}]
    assert ch.compute(recs, now=NOW).recent.agree == 1


def test_status_lines_show_n_and_psi():
    shifted = rows(60, 60, 1, rules="complex") + rows(60, 60, 10, rules="simple")
    text = "\n".join(ch.lines(ch.compute(shifted, now=NOW)))
    assert "n=60" in text and "PSI" in text and "large shift" in text and "not accuracy" in text
    empty = "\n".join(ch.lines(ch.compute([], now=NOW)))
    assert "not measured (kNN off, n=0" in empty and "PSI" in empty and "0.0%" not in empty
    thin = "\n".join(ch.lines(ch.compute(rows(10, 10, 1) + rows(10, 10, 10), now=NOW)))
    assert "too few to trigger" in thin and "not measured" in thin


def test_psi_identical_is_zero_and_shift_is_large():
    assert ch.psi({"a": 50, "b": 50}, {"a": 100, "b": 100}) == pytest.approx(0.0)
    assert ch.psi({"a": 100}, {"b": 100}) > 0.25
    assert ch.psi({"a": 10}, {"a": 100}) is None


def test_bias_moves_one_level_when_active_only():
    assert ch.bias_complexity("moderate") == "moderate"
    import time

    ch._write_state({"active": True, "checked_at": time.time()})
    assert [ch.bias_complexity(c) for c in ("simple", "moderate", "complex", "deep_reasoning", "weird", None)] == [
        "moderate", "complex", "deep_reasoning", "deep_reasoning", "weird", None]


def test_status_renders_the_lines(monkeypatch):
    monkeypatch.setattr(ch, "read_agreement", lambda path=None, days=None: rows(120, 90, 1) + rows(300, 270, 10))
    import time

    monkeypatch.setattr(time, "time", lambda: NOW)
    from llm_router.ui.status_premium import PremiumStatusCommand

    out = PremiumStatusCommand().render_classifier_health().plain
    assert "n=120" in out and "PSI" in out and "conservative mode ON" in out


async def test_conservative_mode_moves_the_proxy_tier_up_one_level():
    import time

    import yaml

    from llm_router.proxy import tiers as pt
    from llm_router.proxy.cache_cost import Stickiness, conversation_key
    from tests.test_proxy_tiers import OPUS, SID, SONNET, _classify, _first, _req

    async def tier(mode_on):
        ch._write_state({"active": mode_on, "checked_at": time.time()})
        raw = yaml.safe_load(pt.DEFAULT_POLICY_PATH.read_text())
        raw["stickiness"] = dict(raw.get("stickiness") or {}, switch_after_first_call=True)
        policy = pt.ClaudeTierPolicy.from_dict(raw)
        sticky = Stickiness()
        await policy.decide(_first(), SID, sticky, classify=_classify("moderate"))
        sticky.record_usage(conversation_key(_first(), SID),
                            {"input_tokens": 10, "cache_read_input_tokens": 30_000, "cache_creation_input_tokens": 9_990})
        d = await policy.decide(_req(), SID, sticky, classify=_classify("moderate"))
        return d.served_model, d.complexity

    assert await tier(False) == (SONNET, "moderate")
    assert await tier(True) == (OPUS, "moderate")  # served a tier up; the ledger keeps the measured class


def test_is_active_reads_the_state_file_once_per_version(monkeypatch):
    import time

    ch._write_state({"active": True, "checked_at": time.time()})
    ch._STATE_CACHE.clear()
    reads = []
    real = json.loads
    monkeypatch.setattr(ch.json, "loads", lambda *a, **k: reads.append(1) or real(*a, **k))
    assert all(ch.is_active() for _ in range(50))
    assert len(reads) == 1  # 50 decisions, one parse
    ch._write_state({"active": False, "checked_at": time.time()})
    assert not ch.is_active() and len(reads) == 2  # a new file version is seen at once
    ch.state_path().unlink()
    assert not ch.is_active()


async def test_with_complexity_knn_records_the_agreement_row(monkeypatch):
    from llm_router import complexity_knn
    from llm_router.proxy import tiers as pt

    async def knn(text):
        return complexity_knn.ComplexityScore(0.9, True, 0.8, 0.5)

    async def base(text):
        return {"task_type": "query", "complexity": "simple"}

    monkeypatch.setattr(complexity_knn, "complexity_score", knn)
    choice = await pt.with_complexity_knn(base)("a private prompt")
    assert choice["complexity"] == "complex"
    row, = [json.loads(x) for x in ch.agreement_path().read_text().splitlines()]
    assert (row["rules_tier"], row["knn_tier"], row["source"]) == ("simple", "frontier", "proxy")
    assert "private" not in ch.agreement_path().read_text()
    h = ch.compute(None, now=row["ts"] + 5)
    assert (h.recent.n, h.recent.agree) == (1, 0)
