"""P3.6-b: shadow agreement with n, the PSI line, and the conservative-mode trigger. Synthetic rows."""
import json

import pytest

from llm_router import classifier_health as ch

NOW = 2_000_000_000.0
DAY = 86400.0


def rows(n, agree, age_days, *, rules="sonnet", other="opus"):
    """n shadow rows, ``agree`` of them matching, ``age_days`` old."""
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
    recs = [dict(r, source="error") for r in rows(50, 50, 1)] + [{"kind": "other", "ts": NOW}]
    assert ch.compute(recs, now=NOW).recent.n == 0


def test_local_counts_as_haiku():
    recs = [{"kind": "classifier_shadow", "ts": NOW - 5, "source": "llm", "rules": {"tier": "haiku"},
             "llm": {"tier": "local"}}]
    assert ch.compute(recs, now=NOW).recent.agree == 1


def test_status_lines_show_n_and_psi():
    shifted = rows(60, 60, 1, rules="opus") + rows(60, 60, 10, rules="haiku")
    text = "\n".join(ch.lines(ch.compute(shifted, now=NOW)))
    assert "n=60" in text and "PSI" in text and "large shift" in text
    empty = "\n".join(ch.lines(ch.compute([], now=NOW)))
    assert "not measured (n=0" in empty and "PSI" in empty and "0.0%" not in empty
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
    from llm_router.proxy import llm_shadow

    monkeypatch.setattr(llm_shadow, "read_records", lambda path=None, days=None: rows(120, 90, 1) + rows(300, 270, 10))
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
