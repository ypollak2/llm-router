"""Pre-registered P2.3-a synthetic replay; no providers, prompts or network."""
from __future__ import annotations

import itertools
import json
import random
import socket

import pytest

from llm_router import map_reader, qa_policy, resource_map as rm, router
from llm_router.types import TaskType
from tests.test_p23_quota_score import T0, base_chain, iso, row, stub_base

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("P2.3 replay must not use the network")

    monkeypatch.setattr(socket.socket, "connect", blocked)


SUBS = ("claude", "codex", "gemini_cli")
PROVIDER = {"claude": "anthropic", "codex": "codex", "gemini_cli": "gemini_cli"}
BY_PROVIDER = {v: k for k, v in PROVIDER.items()}
TT = {"code": TaskType.CODE, "qa": TaskType.QUERY}
INC = {"claude": .004, "codex": .005, "gemini_cli": .003}
WINDOW = {"claude": 5 * 3600, "codex": 5 * 3600, "gemini_cli": 24 * 3600}
PAIRS = [("code", a, b) for a, b in itertools.permutations(SUBS, 2)] + [
    ("qa", "codex", "gemini_cli"), ("qa", "gemini_cli", "codex"),
]


async def bases(monkeypatch, policy):
    stub_base(monkeypatch)
    result = {}
    with monkeypatch.context() as m:
        m.setattr(map_reader, "load_view", lambda *a, **k: None)
        for cls, tt in TT.items():
            result[cls] = {}
            for bucket, pressure in (("lo", .50), ("hi", .97)):
                m.setattr("llm_router.claude_usage.get_claude_pressure", lambda p=pressure: p)
                chain = await base_chain(policy, tt)
                missing = [s for s in SUBS if not any(x.startswith(PROVIDER[s] + "/") for x in chain)]
                assert not missing, (policy, cls, bucket, missing, chain)
                result[cls][bucket] = chain
    return result


def state():
    return {
        "claude": {"primary": .30, "weekly": .20, "reset": T0 + 5 * 3600},
        "codex": {"primary": .20, "weekly": .15, "reset": T0 + 3 * 3600},
        "gemini_cli": {"primary": .10, "weekly": None, "reset": T0 + 24 * 3600},
    }


def schedule():
    for pair in [*PAIRS, None]:
        yield "free", None, 40
        yield "pair" if pair else "unknown", pair, 80
    yield "free", None, 120


def make_map(st, policy, now, phase, pair, control):
    rows = []
    for sub in SUBS:
        s = st[sub]
        r = row(sub, s["primary"], reset_at=iso(s["reset"]))
        r["capabilities"] = {
            "claude": ["generate", "tools", "vision", "thinking"],
            "codex": ["generate", "tools"], "gemini_cli": ["generate", "tools", "vision"],
        }[sub]
        r["quota"]["pressure"] = rm.quota_value(s["primary"], "measured", now)
        if s["weekly"] is not None:
            r["quota"]["weekly_pressure"] = rm.quota_value(s["weekly"], "measured", now)
        if phase == "unknown" and sub == "codex":
            r["quota"] = {"pressure": rm.quota_value(0., "default", now)}
        rows.append(r)
    rm._prioritise(rows, policy, now)
    if control and pair is not None:
        a = pair[1]
        ordered = sorted(rows, key=lambda r: r["effective_priority"])
        preferred = next(r for r in rows if r["resource"] == a)
        for rank, r in enumerate([preferred] + [r for r in ordered if r is not preferred], 1):
            r["effective_priority"] = rank
    assert all(r["marginal_cost"]["value"] == 0.0 for r in rows)
    return {"schema": 1, "policy": policy, "resources": rows}


def indices(chain, provider):
    return [i for i, m in enumerate(chain) if m.startswith(provider + "/")]


def before(chain, a, b):
    return max(indices(chain, PROVIDER[a])) < min(indices(chain, PROVIDER[b]))


def simulate(monkeypatch, base, policy, *, control=False):
    rng, st = random.Random(2303), state()
    counters = {"/".join(p): {"crossings": 0, "exposure": 0, "cases": 0} for p in PAIRS}
    result = {
        "policy": policy, "BASE": base,
        "buckets_equal": {cls: b["hi"] == b["lo"] for cls, b in base.items()},
        "N": 0, "served": 0, "unserved": 0, "cases": 0, "pairs": counters,
        "unknown": {"below": 0, "high": 0, "holds": True},
    }
    previous = {s: None for s in SUBS}
    now = T0
    for phase, pair, length in schedule():
        if pair is not None:
            _, a, b = pair
            c = next(s for s in SUBS if s not in (a, b))
            st[a]["primary"], st[b]["primary"], st[c]["primary"] = .90, .10, .85
            for sub, ceiling in ((a, .50), (b, .30)):
                if st[sub]["weekly"] is not None:
                    st[sub]["weekly"] = min(st[sub]["weekly"], ceiling)
        elif phase == "unknown":
            st["claude"]["primary"], st["gemini_cli"]["primary"] = .94, .50
        for _ in range(length):
            # reset -> external -> route -> routed increment, exactly in that order.
            # Prereg §5: only a pair segment's A has its reset suspended.
            suspended = pair[1] if pair else None
            for sub in SUBS:
                if sub != suspended:
                    while st[sub]["reset"] <= now:
                        st[sub]["primary"] = 0.
                        st[sub]["reset"] += WINDOW[sub]
            if phase == "free":
                cls = "code" if rng.random() < .6 else "qa"
                st["claude"]["primary"] = min(1., st["claude"]["primary"] + rng.uniform(0, .004))
            else:
                cls = pair[0] if pair else "code"
                driven = pair[1] if pair else "claude"   # unknown segment drives claude
                st[driven]["primary"] = min(1., st[driven]["primary"] + .0015)
            data = make_map(st, policy, now, phase, pair, control)
            rows = {r["resource"]: r for r in data["resources"]}
            pressure = {s: rm._pmax(rows[s]) for s in SUBS}
            true_claude = max(st["claude"]["primary"], st["claude"]["weekly"])
            original = base[cls]["hi" if true_claude >= .95 else "lo"]
            with monkeypatch.context() as m:
                m.setattr(map_reader, "load_data", lambda: data)
                # Real score door uses wall time; provide the replay clock to score.
                m.setattr(rm.time, "time", lambda: now)
                scored = router._quota_score_chain(original, TT[cls], exempt=None)
            chain = qa_policy.strip_local_for_qa(scored, TT[cls])
            for provider in {x.split("/", 1)[0] for x in original}:
                assert [x for x in chain if x.startswith(provider + "/")] == [
                    x for x in original if x.startswith(provider + "/")
                ]
            if cls == "qa":
                assert indices(scored, "anthropic") == indices(original, "anthropic")
                local = indices(chain, "ollama")
                non_claude = [i for i, x in enumerate(chain)
                              if x.split("/", 1)[0] not in ("anthropic", "ollama")]
                assert local and non_claude
                assert local == list(range(max(non_claude) + 1, max(non_claude) + 1 + len(local)))
            if phase == "unknown":
                stage = "below" if pressure["claude"] < .95 else "high"
                result["unknown"][stage] += 1
                holds = (
                    before(scored, "claude", "codex") and before(scored, "gemini_cli", "codex")
                    if stage == "below" else
                    before(scored, "gemini_cli", "codex") and before(scored, "codex", "claude")
                )
                result["unknown"]["holds"] &= holds
            available = {
                s: (rm._eligible(rows[s], {"task_type": TT[cls].value, "needs": ()}) is None
                    and not (cls == "qa" and s == "claude")
                    and bool(indices(chain, PROVIDER[s]))
                    and st[s]["primary"] < 1.
                    and (st[s]["weekly"] is None or st[s]["weekly"] < 1.))
                for s in SUBS
            }
            # Serving permits Claude fallback on Q&A; only spread eligibility excludes it.
            serving = None
            for entry in chain:
                sub = BY_PROVIDER.get(entry.split("/", 1)[0])
                if sub is not None and st[sub]["primary"] < 1. and (
                    st[sub]["weekly"] is None or st[sub]["weekly"] < 1.
                ):
                    serving = sub
                    break
            for k, a, b in PAIRS:
                counter = counters["/".join((k, a, b))]
                if cls != k or not available[a] or not available[b]:
                    continue
                pa, pb = pressure[a], pressure[b]
                if pa is None or pb is None or pb >= .80:
                    continue
                if pa >= .95:
                    counter["exposure"] += 1
                    if previous[a] is not None and previous[a] < .95:
                        counter["crossings"] += 1
                if serving == a and (pa >= .95 or pa + INC[a] >= .95):
                    counter["cases"] += 1
            case = serving is not None and pressure[serving] is not None and (
                pressure[serving] >= .95 or pressure[serving] + INC[serving] >= .95
            ) and any(available[s] and s != serving and pressure[s] is not None
                      and pressure[s] < .80 for s in SUBS)
            result["cases"] += int(case)
            result["N"] += 1
            result["unserved" if serving is None else "served"] += 1
            previous = pressure
            if serving is not None:
                s = st[serving]
                s["primary"] = min(1., s["primary"] + INC[serving])
                if s["weekly"] is not None:
                    s["weekly"] = min(1., s["weekly"] + INC[serving] / 10)
            now += 120
    return result


def emit(tmp_path, name, summary):
    text = json.dumps(summary, sort_keys=True, indent=2)
    (tmp_path / (name + ".json")).write_text(text)
    print(text)


@pytest.mark.parametrize("name,policy,threshold", [
    ("R1", "balanced", .95), ("R2", "cost", .95), ("R3", "cost", 2.),
])
async def test_replay(monkeypatch, tmp_path, name, policy, threshold):
    base = await bases(monkeypatch, policy)
    monkeypatch.setattr(rm, "SCORE_PMAX_HIGH", threshold)
    summary = simulate(monkeypatch, base, policy)
    if name != "R3":
        with monkeypatch.context() as m:
            for weight in ("SCORE_W1", "SCORE_W2", "SCORE_W3"):
                m.setattr(rm, weight, 0.)
            m.setattr(rm, "SCORE_PMAX_HIGH", 2.)
            control = simulate(m, base, "cost", control=True)
        summary["control"] = control
        assert all(c["cases"] >= 1 for c in control["pairs"].values())
    emit(tmp_path, name, summary)
    assert summary["N"] == 1200
    assert summary["served"] >= 1000
    assert summary["cases"] == 0
    for counter in summary["pairs"].values():
        assert counter["crossings"] >= 1
        assert counter["exposure"] >= 20
    if name != "R3":
        assert summary["unknown"]["below"] and summary["unknown"]["high"]
        assert summary["unknown"]["holds"]


async def test_negative_control(monkeypatch, tmp_path):
    # Each control reuses that policy run's BASE; only its map policy is cost.
    base_by_policy = {p: await bases(monkeypatch, p) for p in ("balanced", "cost")}
    for name in ("SCORE_W1", "SCORE_W2", "SCORE_W3"):
        monkeypatch.setattr(rm, name, 0.)
    monkeypatch.setattr(rm, "SCORE_PMAX_HIGH", 2.)
    summaries = {}
    for p, base in base_by_policy.items():
        summary = simulate(monkeypatch, base, "cost", control=True)
        summaries[p] = summary
        for counter in summary["pairs"].values():
            assert counter["cases"] >= 1
    emit(tmp_path, "negative-control", summaries)
    assert sum(s["cases"] for s in summaries.values()) >= 50
