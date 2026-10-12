"""P0.15-a (AUTO-PROFILE-OVERWRITE-1): the router never overwrites a profile.yaml it did not write.

Before: ``rescan_and_update`` rewrote profile.yaml every hour through ``save_profile`` with
no check, so any edit (``routing.model_priority``, ``quotas``, the ``overrides:`` block) was
erased. Now ``save_profile`` records the whole-file sha256 in ``profile_hash.json`` and a
rescan writes profile.yaml only while the file still hashes to that value.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from llm_router import auto_profile as ap


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setattr("llm_router.discover.get_cached_ollama_models", lambda: [])
    return tmp_path


@pytest.fixture
def services(monkeypatch):
    """Drive ``detect_services``: every ``next()`` is a different world, so every rescan sees a change."""
    state = {"n": 0}

    def detect():
        return {
            "claude_subscription": True,
            "claude_quota_remaining": 10.0 * state["n"],   # >5 apart: a quota change every rescan
            "ollama_available": state["n"] % 2 == 0,
        }

    monkeypatch.setattr(ap, "detect_services", detect)

    def advance():
        state["n"] += 1

    # Baseline cache, as a first scan would have left it.
    ap.save_detection_cache(detect())
    return advance


def _rescan(advance):
    advance()
    return ap.rescan_and_update()


# ── the unmodified file is refreshed, twice in a row ─────────────────────────

def test_unmodified_file_is_refreshed_on_two_consecutive_rescans(home, services):
    ap.save_profile(ap.generate_profile_yaml(ap.detect_services()))
    seen = [_sha(home / "profile.yaml")]
    for _ in range(3):
        updated, changes = _rescan(services)
        assert updated is True and changes, "an unmodified router-written file must be refreshed"
        seen.append(_sha(home / "profile.yaml"))
        # the hash survived save_detection_cache (the rescan wrote profile_cache.json after it)
        assert json.loads((home / "profile_hash.json").read_text())["sha256"] == seen[-1]
        assert ap.profile_state() == "owned"
    assert len(set(seen)) == 4, "every rescan changed the content, so every rescan must have rewritten it"
    assert not (home / "profile.generated.yaml").exists()


def test_the_hash_is_not_kept_in_the_detection_cache(home, services):
    ap.save_profile("routing: {}\n")
    ap.save_detection_cache(ap.detect_services())
    assert "sha256" not in (home / "profile_cache.json").read_text()
    assert ap.profile_state() == "owned"


# ── 6 fixtures x 3 forced rescans ────────────────────────────────────────────

def _fixture_owned(home):
    ap.save_profile(ap.generate_profile_yaml(ap.detect_services()))


def _fixture_edited(home):
    _fixture_owned(home)
    p = home / "profile.yaml"
    p.write_text(p.read_text() + "overrides:\n  code:\n    force_quality: true  # mine\n")


def _fixture_legacy(home):
    (home / "profile.yaml").write_text(ap.generate_profile_yaml(ap.detect_services()))  # no hash recorded


def _fixture_crlf(home):
    _fixture_owned(home)
    p = home / "profile.yaml"
    p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))


def _fixture_empty(home):
    (home / "profile.yaml").write_bytes(b"")


def _fixture_nonyaml(home):
    (home / "profile.yaml").write_bytes(b"{{ not: [yaml\n\t- : :\n\x00binary")


PRESERVED = {
    "edited": _fixture_edited,
    "legacy-no-hash": _fixture_legacy,
    "crlf": _fixture_crlf,
    "empty": _fixture_empty,
    "non-yaml": _fixture_nonyaml,
}


@pytest.mark.parametrize("name", sorted(PRESERVED))
def test_a_file_the_router_did_not_last_write_is_byte_identical_after_three_rescans(home, services, name):
    PRESERVED[name](home)
    before = (home / "profile.yaml").read_bytes()
    for i in range(3):
        updated, changes = _rescan(services)
        assert changes, "the fixture must present a change, or this test proves nothing"
        assert updated is False
        assert (home / "profile.yaml").read_bytes() == before, f"rescan {i + 1} changed {name}"
    generated = home / "profile.generated.yaml"
    assert generated.exists(), "the regeneration goes to profile.generated.yaml"
    assert generated.read_text().startswith("# Auto-detected LLM Router Profile")
    assert ap.profile_state() == "edited"


def test_six_fixtures_are_covered():
    assert len(PRESERVED) + 1 == 6   # + the unmodified file above


# ── one note per change of the regenerated content ───────────────────────────

def test_one_note_per_change_of_the_regenerated_content(home, services):
    _fixture_edited(home)
    notes = []
    for _ in range(3):
        _rescan(services)
        notes.append(ap.last_rescan_note())
    assert notes[0] == ap.PROFILE_EDITED_NOTE
    assert "run `llm-router profile auto`" in notes[0] and "a backup is kept" in notes[0]
    # every world differs in the quota text, so each regeneration is new content: one note each
    assert notes == [ap.PROFILE_EDITED_NOTE] * 3


def test_an_identical_regeneration_produces_no_second_note(home, monkeypatch):
    _fixture_edited(home)
    # `huggingface_available` flips: a detected change that is not in the YAML

    def detect():
        return {"claude_subscription": True, "claude_quota_remaining": 50.0,
                "huggingface_available": next_world[0]}

    next_world = [True]
    monkeypatch.setattr(ap, "detect_services", detect)
    ap.save_detection_cache(detect())
    notes = []
    for flag in (False, True, False):
        next_world[0] = flag
        updated, changes = ap.rescan_and_update()
        assert changes and updated is False
        notes.append(ap.last_rescan_note())
    assert notes == [ap.PROFILE_EDITED_NOTE, None, None]


def test_doctor_and_map_lines_say_the_file_is_not_auto_updated(home, services):
    assert ap.profile_status_lines() == []
    _fixture_edited(home)
    lines = ap.profile_status_lines()
    assert lines and ap.PROFILE_EDITED_NOTE in lines[0]


# ── the explicit command backs up first and records the hash ─────────────────

def test_profile_auto_makes_a_timestamped_backup_and_records_the_hash(home, services):
    _fixture_edited(home)
    edited = (home / "profile.yaml").read_bytes()
    out = ap.auto_generate_profile()
    baks = sorted(home.glob("profile.yaml.*.bak"))
    assert len(baks) == 1 and baks[0].read_bytes() == edited
    assert out.read_bytes() != edited
    assert json.loads((home / "profile_hash.json").read_text())["sha256"] == _sha(home / "profile.yaml")
    assert ap.profile_state() == "owned"
    # a second run in the same second never overwrites the first backup
    ap.auto_generate_profile()
    assert len(sorted(home.glob("profile.yaml.*.bak"))) == 2
    assert baks[0].read_bytes() == edited


def test_profile_auto_without_a_file_makes_no_backup(home, services):
    ap.auto_generate_profile()
    assert list(home.glob("*.bak")) == []
    assert ap.profile_state() == "owned"


def test_an_absent_file_is_created_by_a_rescan(home, services):
    updated, changes = _rescan(services)
    assert updated and (home / "profile.yaml").exists() and ap.profile_state() == "owned"


# ── EMPTY-ENVIRONMENT-1 (P0.15-d) ────────────────────────────────────────────

@pytest.mark.parametrize("cache", [[], None, Exception])
def test_an_empty_cache_writes_no_model_name_no_adapter_observed(home, monkeypatch, cache):
    def fake():
        if cache is Exception:
            raise RuntimeError("cache unreadable")
        return cache or []

    monkeypatch.setattr("llm_router.discover.get_cached_ollama_models", fake)
    text = ap.generate_profile_yaml({"ollama_available": True})
    for ghost in ("qwen3.5:latest", "kimi-k2.6:cloud", "qwen3-coder-next", "qwen2.5:latest", "gemma4:latest"):
        assert ghost not in text
    assert "ollama/" not in text
    assert "no installed model has been observed" in text


def test_observed_models_are_still_written(home, monkeypatch):
    monkeypatch.setattr("llm_router.discover.get_cached_ollama_models", lambda: ["ollama/seen:7b"])
    assert "ollama/seen:7b" in ap.generate_profile_yaml({"ollama_available": True})
