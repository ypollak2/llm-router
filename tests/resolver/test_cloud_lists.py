"""P1.8 task 13 / MUST P1.8-g: per-account cloud model lists. Fake HTTP only."""

from __future__ import annotations

import io
import json
import urllib.error

from llm_router.model_registry import ModelRegistry
from llm_router.resolver import auth_ping
from llm_router.resolver import inventory as inv_mod
from llm_router.resolver.auth_ping import ListResult

from .fakes import NOW, make_probes, meta

KEY = "sk-live-AAAABBBBCCCCDDDD1234"
KEY2 = "sk-live-ZZZZYYYYXXXXWWWW9876"
SIX_H = 6 * 3600.0
REG = ModelRegistry.from_models([
    meta(f"openai/m{i}", "cheap") for i in range(1, 6)
] + [meta("anthropic/claude-haiku-4-5", "cheap")])


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def _probes(tmp_path, fetch, clock=None, key=KEY, env_name="OPENAI_API_KEY"):
    clock = clock or Clock()
    p = make_probes(env={env_name: key}, registry=REG, ping_cache=tmp_path / "c.json")
    p.ping_list = fetch
    p.now = clock
    return p


def _rows(inv, listing):
    return sorted(m.id for m in inv.models if m.listing == listing)


def _fetch(ids, calls=None, status=200, complete=True):
    def f(provider, key):
        if calls is not None:
            calls.append(provider)
        return ListResult(status, tuple(ids) if ids is not None else None, complete)
    return f


def test_a_key_listing_3_of_5_gives_3_accessible_and_2_not_listed(tmp_path):
    p = _probes(tmp_path, _fetch(["m1", "m2", "m3"]))
    inv = inv_mod.collect_inventory(p, profiles={}, verify=True)
    assert _rows(inv, "accessible") == ["openai/m1", "openai/m2", "openai/m3"]
    assert _rows(inv, "not_listed") == ["openai/m4", "openai/m5"]
    nl = inv.get("openai/m4")
    assert nl.authorized is False and nl.path_verified is False
    ok = inv.get("openai/m1")
    assert ok.authorized and ok.path_verified


def test_an_unknown_listed_id_is_discovered_with_unknown_metadata(tmp_path):
    p = _probes(tmp_path, _fetch(["m1", "brand-new-model"]))
    inv = inv_mod.collect_inventory(p, profiles={}, verify=True)
    row = inv.get("openai/brand-new-model")
    assert row.listing == "discovered"
    assert row.context_window is None and row.capabilities == {}
    assert row.tier_ceiling is None and row.tier_basis == "unmeasured"
    assert row.price_in_per_mtok is None and "metadata unknown" in row.notes


def test_a_dated_listed_id_matches_its_registry_name(tmp_path):
    p = _probes(tmp_path, _fetch(["claude-haiku-4-5-20251001"]), env_name="ANTHROPIC_API_KEY")
    inv = inv_mod.collect_inventory(p, profiles={}, verify=True)
    assert _rows(inv, "accessible") == ["anthropic/claude-haiku-4-5"]
    assert _rows(inv, "discovered") == []


class _Resp:
    status = 200

    def __init__(self, body: bytes):
        self._b = io.BytesIO(body)

    def read(self, n=-1):
        return self._b.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    def __init__(self, pages):
        self.pages, self.seen = pages, []

    def open(self, req, timeout):
        self.seen.append(req)
        out = self.pages[len(self.seen) - 1]
        if isinstance(out, int):
            raise urllib.error.HTTPError(req.full_url, out, "x", {}, None)
        return _Resp(out if isinstance(out, bytes) else json.dumps(out).encode())


def _patch(monkeypatch, pages):
    op = _Opener(pages)
    monkeypatch.setattr(auth_ping, "_opener", lambda: op)
    return op


def test_a_paginated_gemini_list_is_parsed_across_pages(monkeypatch):
    op = _patch(monkeypatch, [
        {"models": [{"name": "models/a"}, {"name": "models/b"}], "nextPageToken": "T2"},
        {"models": [{"name": "models/c"}]},
    ])
    res = auth_ping.http_list("gemini", KEY)
    assert res.ids == ("a", "b", "c") and res.complete and res.status == 200
    assert "pageToken=T2" in op.seen[1].full_url and KEY not in op.seen[1].full_url


def test_a_paginated_anthropic_list_is_parsed_across_pages(monkeypatch):
    op = _patch(monkeypatch, [
        {"data": [{"id": "x1"}], "has_more": True, "last_id": "x1"},
        {"data": [{"id": "x2"}], "has_more": False},
    ])
    res = auth_ping.http_list("anthropic", KEY)
    assert res.ids == ("x1", "x2") and res.complete
    assert "after_id=x1" in op.seen[1].full_url


def test_openai_style_list_and_openrouter_has_no_list(monkeypatch):
    _patch(monkeypatch, [{"data": [{"id": "gpt-a"}, {"id": "gpt-b"}]}])
    assert auth_ping.http_list("openai", KEY).ids == ("gpt-a", "gpt-b")
    _patch(monkeypatch, [{"data": {"label": "k"}}])
    r = auth_ping.http_list("openrouter", KEY)
    assert r.status == 200 and r.ids is None


def test_caps_2000_ids_and_1mb_leave_the_list_incomplete(monkeypatch):
    _patch(monkeypatch, [{"data": [{"id": f"m{i}"} for i in range(2500)]}])
    r = auth_ping.http_list("openai", KEY)
    assert len(r.ids) == auth_ping.MAX_IDS and r.complete is False
    _patch(monkeypatch, [b"x" * (auth_ping.MAX_BODY_BYTES + 1)])
    r = auth_ping.http_list("openai", KEY)
    assert r.status == 200 and r.ids is None and r.complete is False


def test_a_failed_later_page_is_incomplete_and_marks_nothing_not_listed(monkeypatch, tmp_path):
    _patch(monkeypatch, [{"models": [{"name": "models/m1"}], "nextPageToken": "T"}, 500])
    r = auth_ping.http_list("gemini", KEY)
    assert r.ids == ("m1",) and r.complete is False
    p = _probes(tmp_path, lambda prov, key: r)
    inv = inv_mod.collect_inventory(p, profiles={}, verify=True)
    assert _rows(inv, "not_listed") == [] and _rows(inv, "accessible") == ["openai/m1"]


def test_a_revoked_key_makes_every_provider_model_inaccessible_in_one_refresh(tmp_path):
    clock, answers = Clock(), [ListResult(200, ("m1", "m2", "m3", "m4", "m5"), True), ListResult(401)]
    p = _probes(tmp_path, lambda prov, key: answers.pop(0), clock)
    inv = inv_mod.collect_inventory(p, profiles={}, verify=True)
    assert len(_rows(inv, "accessible")) == 5
    clock.t += SIX_H + 1
    inv = inv_mod.collect_inventory(p, profiles={}, verify=True)
    openai = [m for m in inv.models if m.provider == "openai"]
    assert len(openai) == 5
    assert all(m.listing == "key_rejected" and not m.authorized and not m.path_verified for m in openai)


def test_a_403_also_marks_the_provider_key_rejected(tmp_path):
    p = _probes(tmp_path, _fetch(None, status=403))
    inv = inv_mod.collect_inventory(p, profiles={}, verify=True)
    assert _rows(inv, "key_rejected") == [f"openai/m{i}" for i in range(1, 6)]


def test_at_most_one_list_call_per_provider_per_6h_absent_a_key_change(tmp_path):
    clock, calls = Clock(), []
    p = _probes(tmp_path, _fetch(["m1"], calls), clock)
    for _ in range(3):
        inv_mod.collect_inventory(p, profiles={}, verify=True)
        clock.t += 3600
    assert calls == ["openai"]
    clock.t += SIX_H
    inv_mod.collect_inventory(p, profiles={}, verify=True)
    assert len(calls) == 2


def test_a_changed_key_refreshes_at_once(tmp_path):
    calls = []
    p = _probes(tmp_path, _fetch(["m1"], calls))
    inv_mod.collect_inventory(p, profiles={}, verify=True)
    p.environ = {"OPENAI_API_KEY": KEY2}
    inv_mod.collect_inventory(p, profiles={}, verify=True)
    assert len(calls) == 2


def test_a_transport_error_never_marks_not_listed_and_is_not_cached(tmp_path):
    calls = []
    p = _probes(tmp_path, lambda prov, key: (calls.append(1), ListResult(None))[1])
    inv = inv_mod.collect_inventory(p, profiles={}, verify=True)
    assert _rows(inv, "not_listed") == [] and _rows(inv, "discovered") == []
    inv_mod.collect_inventory(p, profiles={}, verify=True)
    assert len(calls) == 2


def test_without_verify_no_call_is_made_and_the_cached_list_is_used(tmp_path):
    calls = []
    p = _probes(tmp_path, _fetch(["m1", "m2"], calls))
    cold = inv_mod.collect_inventory(p, profiles={}, verify=False)
    assert calls == [] and _rows(cold, "not_listed") == []
    inv_mod.collect_inventory(p, profiles={}, verify=True)
    warm = inv_mod.collect_inventory(p, profiles={}, verify=False)
    assert len(calls) == 1 and len(_rows(warm, "accessible")) == 2 and len(_rows(warm, "not_listed")) == 3


def test_the_cache_holds_ids_and_a_fingerprint_but_never_the_key(tmp_path):
    p = _probes(tmp_path, _fetch(["m1"]))
    inv_mod.collect_inventory(p, profiles={}, verify=True)
    text = (tmp_path / "c.json").read_text()
    assert KEY not in text and '"m1"' in text
