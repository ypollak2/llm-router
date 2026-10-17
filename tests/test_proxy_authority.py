"""P2.2-A: authority route|advise at the proxy (door test through the real app, mocked upstream).

route is today's behaviour; advise forwards the client's own bytes, serves nothing
locally and keeps the policy's proposal in the ledger. The mode is read per request,
so flipping the state file needs no restart. Anthropic and the local model are fakes.
"""

from __future__ import annotations

import json

import pytest

from llm_router import authority
from llm_router.proxy import backends as pb
from llm_router.proxy import ledger
from llm_router.proxy import server as ps
from tests import test_p27_proxy_prefixes as pfx
from tests import test_proxy as tp
from tests import test_proxy_tiers as tt

OPUS, SONNET = tt.OPUS, tt.SONNET


@pytest.fixture(autouse=True)
def _scratch(monkeypatch, tmp_path):
    home = tmp_path / "state"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.delenv("LLM_ROUTER_AUTHORITY", raising=False)
    monkeypatch.setattr(authority, "_file_cache", None)
    monkeypatch.setattr(authority, "_reported", set())
    return home


@pytest.fixture
def moderate(monkeypatch):
    async def choose(text, pinned, *, anthropic=False):
        return {"task_type": "code", "complexity": "moderate", "chain_head": [], "model": None}
    monkeypatch.setattr(pb, "tier_classify", choose)


def _skip_rows(home) -> list[dict]:
    path = home / "skips.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def _two_calls(tmp_path):
    up = tt.Upstream()
    app = tt._app(tmp_path, up)
    await tt._post(app, tt._first())
    r = await tt._post(app, tt._req())
    assert r.status_code == 200
    return up, app


async def test_route_rewrites_as_before_and_rows_say_route(tmp_path, moderate):
    up, _ = await _two_calls(tmp_path)
    assert [json.loads(q.content)["model"] for q in up.requests] == [OPUS, SONNET]
    first, second = tt._rows(tmp_path)
    assert (second["tier_reason"], second["served_model"], second["tier_switch"]) == ("policy", SONNET, True)
    assert second["tier"] == "sonnet" and first["authority"] == second["authority"] == "route"


async def test_advise_forwards_the_original_bytes_and_logs_the_proposal(tmp_path, moderate):
    authority.set_mode("advise")
    up, _ = await _two_calls(tmp_path)
    sent = [q.content for q in up.requests]
    assert sent[1] == json.dumps(tt._req()).encode(), "the client's own bytes, not a re-serialised body"
    assert [json.loads(b)["model"] for b in sent] == [OPUS, OPUS]
    first, second = tt._rows(tmp_path)
    assert second["authority"] == "advise" and second["tier_reason"] == "advise"
    assert second["tier_reason_proposed"] not in (None, "advise"), "the would-be reason is kept apart"
    assert second["tier_proposed"] == "sonnet", "the proposal is still logged"
    assert second["requested_model"] == second["served_model"] == OPUS
    assert second["tier_switch"] is False and second["tier_switch_cost_usd"] is None
    assert second["tier"] is None and "tier_body_rewrite" not in second
    assert second["decision"] == ledger.DECISION_FORWARDED


async def test_route_and_advise_rows_have_the_same_keys_but_the_routing_off_reason(tmp_path, moderate, monkeypatch):
    route_dir, advise_dir = tmp_path / "r", tmp_path / "a"
    route_dir.mkdir()
    advise_dir.mkdir()
    await _two_calls(route_dir)
    authority.set_mode("advise")
    await _two_calls(advise_dir)
    r = tt._rows(route_dir)[-1]
    a = tt._rows(advise_dir)[-1]
    # steps are off in this app: route says why it served nothing locally, advise never tries.
    assert set(r) - set(a) == {"reason"} and set(a) - set(r) == {"tier_reason_proposed"}


async def test_flipping_the_state_file_changes_the_next_request_without_a_restart(tmp_path, moderate):
    up = tt.Upstream()
    app = tt._app(tmp_path, up)
    await tt._post(app, tt._first())
    authority.set_mode("advise")
    await tt._post(app, tt._req())
    authority.set_mode("route")
    await tt._post(app, tt._req())
    models = [json.loads(q.content)["model"] for q in up.requests]
    assert models == [OPUS, OPUS, SONNET]
    assert [r["authority"] for r in tt._rows(tmp_path)] == ["route", "advise", "route"]


async def test_unknown_mode_value_falls_back_to_route(tmp_path, moderate, _scratch, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_AUTHORITY", "turbo")
    up, _ = await _two_calls(tmp_path)
    assert json.loads(up.requests[1].content)["model"] == SONNET
    assert tt._rows(tmp_path)[-1]["authority"] == "route"


async def test_env_override_wins_over_the_file(tmp_path, moderate, monkeypatch):
    authority.set_mode("route")
    monkeypatch.setenv("LLM_ROUTER_AUTHORITY", "advise")
    up, _ = await _two_calls(tmp_path)
    assert json.loads(up.requests[1].content)["model"] == OPUS


async def test_advise_does_not_serve_locally_but_route_does(tmp_path, monkeypatch):
    async def choose(text, pinned, **_kw):
        return {"task_type": "code", "complexity": "moderate", "chain_head": ["ollama/fake:1"],
                "model": "ollama/fake:1"}
    monkeypatch.setattr(ps, "choose_model", choose)
    reply = tp._ollama("Running it.", [tp._call("Bash", {"command": "python3 tests/test_mod01.py"})])

    route_up, route_backend = tp.Upstream(), tp.FakeBackend(reply)
    r = await tp._post(tp._app(tmp_path, route_up, route_backend), tp._req())
    assert r.status_code == 200 and len(route_backend.calls) == 1 and route_up.requests == []

    authority.set_mode("advise")
    adv_dir = tmp_path / "adv"
    adv_dir.mkdir()
    up, backend = tp.Upstream(), tp.FakeBackend(reply)
    r = await tp._post(tp._app(adv_dir, up, backend), tp._req())
    assert r.status_code == 200
    assert backend.calls == [], "advise must not try the local model"
    assert len(up.requests) == 1 and up.requests[0].content == json.dumps(tp._req()).encode()
    (row,) = ledger.read_rows(adv_dir / "proxy_calls.jsonl")
    assert row["decision"] == ledger.DECISION_FORWARDED and row["authority"] == "advise"


async def test_the_proxy_writes_no_skip_rows_the_ledger_row_carries_the_reason(tmp_path, moderate, _scratch):
    """D-150: a per-call proxy skip row would let coverage_check explain a call within +-120 s as
    "skipped" and hide a proxy bypass. routing_off / tiers_off live on the ledger row only."""
    up = tt.Upstream()
    await tt._post(tt._app(tmp_path, up), tt._first())                       # steps off, tiers on: routing_off
    assert tt._rows(tmp_path)[0]["reason"] == "routing_off"
    off = tp.Upstream()
    await tp._post(tp._app(tmp_path, off,
                           steps=frozenset({"continuation"}), tiers=ps.TIERS_OFF), tp._first_call())
    authority.set_mode("advise")
    await tp._post(tp._app(tmp_path, tp.Upstream(), tiers=ps.TIERS_OFF), tp._req())  # advise, no policy
    assert _skip_rows(_scratch) == [] and not (_scratch / "skips.jsonl").exists()


async def test_advise_without_a_tier_policy_still_passes_with_reason_advise(tmp_path, _scratch):
    authority.set_mode("advise")
    up = tp.Upstream()
    app = tp._app(tmp_path, up, tiers=ps.TIERS_OFF)
    await tp._post(app, tp._req())
    (row,) = ledger.read_rows(tmp_path / "proxy_calls.jsonl")
    assert row["tier_reason"] == "advise" and row["authority"] == "advise" and "tier_reason_proposed" not in row


# ── D-150: advise is shadow under the P2.7 precedence ────────────────────────

@pytest.mark.parametrize("name", ["opus", "sonnet", "haiku", "claude"])
async def test_advise_with_a_claude_prefix_forwards_the_original_unstripped_bytes(tmp_path, monkeypatch, name):
    post, seen, backend, policy = await pfx.door(tmp_path, monkeypatch)
    authority.set_mode("advise")
    body = pfx.request(f"{name}: explain this")
    response, raw, row = await post(body)
    assert response.status_code == 200 and not backend.calls
    assert seen == [raw], "bytes_equal: the prefix is NOT stripped and no model is rewritten"
    assert row["prefix"] == name and row["prefix_outcome"] == "advise:not_applied"
    assert row["authority"] == "advise" and row["tier_reason"] == "advise"
    assert row["tier_reason_proposed"] is not None and row["tier_switch"] is False
    assert row["served_model"] == row["requested_model"] == body["model"]


@pytest.mark.parametrize("name", ["local", "codex"])
async def test_advise_with_a_local_or_codex_prefix_forwards_the_original_bytes(tmp_path, monkeypatch, name):
    post, seen, backend, _ = await pfx.door(tmp_path, monkeypatch, local=True)
    authority.set_mode("advise")
    response, raw, row = await post(pfx.request(f"{name}: explain this"))
    assert response.status_code == 200 and not backend.calls, "advise never serves locally"
    assert seen == [raw]
    assert row["prefix_outcome"] == "advise:not_applied" and row["tier_reason"] == "advise"


async def test_route_with_an_opus_prefix_is_unchanged(tmp_path, monkeypatch):
    post, seen, backend, policy = await pfx.door(tmp_path, monkeypatch)
    response, raw, row = await post(pfx.request("opus: explain this"))
    sent = json.loads(seen[0])
    assert sent["messages"][0]["content"] == "explain this", "route still strips the prefix"
    assert sent["model"] == policy.by_name["opus"].model
    assert row["authority"] == "route" and row["prefix_outcome"] != "advise:not_applied"
    assert row["tier_reason"] != "advise" and "tier_reason_proposed" not in row


async def test_advise_never_updates_the_stickiness_but_route_does(tmp_path, moderate, monkeypatch):
    from llm_router.proxy.cache_cost import Stickiness

    records: list[str] = []
    real = Stickiness.record

    def spy(self, key, model, complexity, reason):
        records.append(reason)
        return real(self, key, model, complexity, reason)

    monkeypatch.setattr(Stickiness, "record", spy)
    authority.set_mode("advise")
    await _two_calls(tmp_path)
    assert records == [], "a proposal that is never applied must not pin later turns"
    authority.set_mode("route")
    d = tmp_path / "route"
    d.mkdir()
    await _two_calls(d)
    assert records, "route records its decisions as before"


async def test_advise_forwards_non_default_json_bytes_unchanged(tmp_path, moderate):
    """F12-C / mutant M14: ``raw`` must reach upstream untouched. A body with non-default
    separators, non-sorted keys and non-ASCII text differs from ``json.dumps(body)`` in every
    one of those ways, so a re-serialisation cannot pass."""
    import httpx

    authority.set_mode("advise")
    up = tt.Upstream()
    app = tt._app(tmp_path, up)
    body = tt._req()
    body = {"zz_last_first": "naïve – 日本語 – ✓", **dict(reversed(list(body.items())))}
    body["messages"][-1]["content"] = "héllo wörld – 日本語 ✓"
    raw = json.dumps(body, separators=(", ", ": "), ensure_ascii=False).encode("utf-8")
    assert raw != json.dumps(body).encode() and raw != json.dumps(body, sort_keys=True).encode()
    assert list(json.loads(raw)) != sorted(json.loads(raw))
    h = {"authorization": "Bearer sk-ant-oat01-" + "Zq9" * 20, "anthropic-version": "2023-06-01",
         "content-type": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as c:
        assert (await c.post("/v1/messages?beta=true", content=tt_first_bytes(), headers=h)).status_code == 200
        r = await c.post("/v1/messages?beta=true", content=raw, headers=h)
    assert r.status_code == 200
    assert up.requests[-1].content == raw, "advise sent something other than the client's own bytes"
    assert tt._rows(tmp_path)[-1]["authority"] == "advise"


def tt_first_bytes() -> bytes:
    return json.dumps(tt._first()).encode()
