"""doctor and the session banner surface the detected seats."""
from __future__ import annotations

import json
import pathlib

from llm_router import seats as S
from llm_router.commands import doctor as doc


def _patch_home(monkeypatch, tmp_path):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))


def _fake_refresh(seats: S.Seats):
    def refresh(home=None, **kw):
        S.save_seats(seats, home)
        return seats
    return refresh


def test_doctor_lists_every_seat_and_the_free_bucket(monkeypatch, tmp_path):
    _patch_home(monkeypatch, tmp_path)
    monkeypatch.delenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", raising=False)
    seats = S.Seats(
        claude=S.Seat(kind="claude.ai", plan="max"),
        codex=S.Seat(kind="chatgpt", plan="plus", plan_stale=True),
        gemini=S.Seat(kind="api-key"),
        ollama=S.Seat(kind="local", models=("a", "b")),
        detected_at="2026-09-04T00:00:00+00:00",
    )
    monkeypatch.setattr(S, "refresh_seats", _fake_refresh(seats))
    text = "\n".join(doc._seats_report())
    assert "claude.ai(max)" in text
    assert "chatgpt(plus,stale)" in text and "past its window" in text
    assert "Gemini CLI api key only" in text
    assert "local(2 models)" in text
    assert "free bucket: claude, codex, ollama" in text
    assert "defaults to 'anthropic'" in text
    # persisted for the session hook
    assert (tmp_path / ".llm-router" / "seats.json").exists()


def test_doctor_warns_when_no_seat_at_all(monkeypatch, tmp_path):
    _patch_home(monkeypatch, tmp_path)
    monkeypatch.setattr(S, "refresh_seats", _fake_refresh(S.Seats(detected_at="2026-09-04T00:00:00+00:00")))
    text = "\n".join(doc._seats_report())
    assert "no seat found" in text
    assert "not logged in" in text


def test_doctor_flags_env_override_that_disagrees_with_the_seat(monkeypatch, tmp_path):
    _patch_home(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "gemini")
    seats = S.Seats(claude=S.Seat(kind="claude.ai", plan="pro"), detected_at="2026-09-04T00:00:00+00:00")
    monkeypatch.setattr(S, "refresh_seats", _fake_refresh(seats))
    text = "\n".join(doc._seats_report())
    assert "LLM_ROUTER_SUBSCRIPTION_PROVIDER=gemini but the detected seat is 'anthropic'" in text


def test_doctor_never_crashes_on_a_probe_error(monkeypatch, tmp_path):
    _patch_home(monkeypatch, tmp_path)

    def boom(**kw):
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(S, "refresh_seats", boom)
    text = "\n".join(doc._seats_report())
    assert "seat detection failed: probe exploded" in text


def test_doctor_run_includes_the_seats_section(monkeypatch, tmp_path, capsys):
    _patch_home(monkeypatch, tmp_path)
    monkeypatch.setattr(S, "refresh_seats", _fake_refresh(S.Seats(detected_at="2026-09-04T00:00:00+00:00")))
    doc._run_doctor()
    out = capsys.readouterr().out
    assert "Seats (subscriptions this machine is logged in to)" in out


def _load_session_start():
    import importlib.util
    path = pathlib.Path(doc.__file__).resolve().parent.parent / "hooks" / "session-start.py"
    spec = importlib.util.spec_from_file_location("llm_router_session_start_hook", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_session_banner_uses_fresh_cache_without_redetecting(monkeypatch, tmp_path):
    _patch_home(monkeypatch, tmp_path)
    seats = S.Seats(claude=S.Seat(kind="claude.ai", plan="max"))
    fresh = S.Seats.from_dict({**seats.to_dict(), "detected_at": S.datetime.now(S.timezone.utc).isoformat()})
    S.save_seats(fresh, tmp_path)

    def must_not_run(**kw):
        raise AssertionError("re-detected despite a fresh cache")
    monkeypatch.setattr(S, "refresh_seats", must_not_run)
    hook = _load_session_start()
    assert hook._seats_hint() == "\n💺 Seats: " + fresh.summary_line()


def test_session_banner_redetects_a_stale_cache(monkeypatch, tmp_path):
    _patch_home(monkeypatch, tmp_path)
    S.save_seats(S.Seats(detected_at="2000-01-01T00:00:00+00:00"), tmp_path)
    seats = S.Seats(codex=S.Seat(kind="chatgpt", plan="pro"), detected_at="2026-09-04T00:00:00+00:00")
    calls = []

    def refresh(**kw):
        calls.append(kw)
        return seats
    monkeypatch.setattr(S, "refresh_seats", refresh)
    hook = _load_session_start()
    assert "codex=chatgpt(pro)" in hook._seats_hint()
    assert calls and calls[0].get("timeout") == 2.0


def test_session_banner_is_silent_on_error(monkeypatch, tmp_path):
    _patch_home(monkeypatch, tmp_path)

    def boom(**kw):
        raise OSError("nope")
    monkeypatch.setattr(S, "refresh_seats", boom)
    assert _load_session_start()._seats_hint() == ""


def test_seats_json_shape_is_stable(tmp_path):
    """The hook, doctor, and install all read this file; pin its keys."""
    S.save_seats(S.Seats(detected_at="2026-09-04T00:00:00+00:00"), tmp_path)
    data = json.loads((tmp_path / ".llm-router" / "seats.json").read_text())
    assert set(data) == {"claude", "codex", "gemini", "ollama", "api_keys", "detected_at"}
    assert set(data["claude"]) == {"kind", "plan", "plan_stale", "models"}
