"""P2.3-b: 500 seeded adversarial cases and an independent score/filter oracle."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import math
import random

from llm_router import resource_map as rm

T0 = 1_760_000_000
MISSING = object()


class Poison:
    touches = 0

    def trap(self, *args, **kwargs):
        type(self).touches += 1
        raise AssertionError("unknown value touched")


for _name in (
    "__float__", "__int__", "__index__", "__bool__", "__add__", "__radd__",
    "__sub__", "__rsub__", "__mul__", "__rmul__", "__truediv__", "__rtruediv__",
    "__lt__", "__le__", "__gt__", "__ge__", "__neg__", "__abs__", "__round__",
):
    setattr(Poison, _name, Poison.trap)


def number(x, lower=None, upper=None):
    if type(x) not in (int, float):
        return None
    if not math.isfinite(x):
        return None
    if lower is not None and x < lower:
        return None
    if upper is not None and x > upper:
        return None
    return float(x)


def oracle(r, d):
    d = d if isinstance(d, dict) else {}
    terms = {k: "unknown" for k in ("priority", "pressure", "reset", "marginal_cost")}
    inputs = {k: "unknown" for k in
              ("effective_priority", "pressure", "reset_in_h", "marginal_cost")}
    priority = r.get("effective_priority")
    if type(priority) is int and priority >= 1 and d.get("priority_ordered") is True:
        inputs["effective_priority"] = float(priority)
        terms["priority"] = 1 / priority
    pressures = []
    q = r.get("quota")
    if isinstance(q, dict):
        for name in ("pressure", "weekly_pressure"):
            value = q.get(name)
            if isinstance(value, dict):
                provenance = value.get("provenance")
                if isinstance(provenance, str) and provenance in ("measured", "estimated"):
                    p = number(value.get("value"), 0, 1)
                    if p is not None:
                        pressures.append(p)
    pmax = max(pressures) if pressures else None
    if pmax is not None:
        inputs["pressure"], terms["pressure"] = pmax, -10. * pmax
    now = number(d.get("now"))
    if now is None:
        now = T0
    reset = r.get("reset_at")
    if isinstance(reset, str):
        try:
            parsed = datetime.fromisoformat(reset)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            hours = (parsed.timestamp() - now) / 3600
            if hours > 0 and math.isfinite(hours):
                inputs["reset_in_h"], terms["reset"] = hours, .05 / max(hours, .25)
        except (ValueError, TypeError, OverflowError, OSError):
            pass
    cost_data = r.get("marginal_cost")
    cost = number(cost_data.get("value"), 0) if isinstance(cost_data, dict) else None
    if cost is not None:
        inputs["marginal_cost"], terms["marginal_cost"] = cost, -cost
    value = sum(v for v in terms.values() if type(v) is float)
    status, caps = r.get("status"), r.get("capabilities")
    eligible = isinstance(status, str) and status == "connected"
    if not isinstance(caps, (list, tuple)):
        eligible = False
        capabilities = set()
    else:
        capabilities = {c for c in caps if isinstance(c, str)}
    eligible = eligible and "generate" in capabilities
    needs = d.get("needs")
    if isinstance(needs, (list, tuple, set, frozenset)):
        if any(n not in capabilities for n in needs if isinstance(n, str)):
            eligible = False
    competence, tt = r.get("competence"), d.get("task_type")
    if isinstance(competence, dict) and isinstance(tt, str):
        entry = competence.get(tt)
        if isinstance(entry, dict):
            tiers = [t for t in entry.values() if isinstance(t, dict)]
            if tiers and all(t.get("eligible") is False for t in tiers):
                eligible = False
    kind = r.get("kind")
    subscription = isinstance(kind, str) and kind == "subscription"
    cg = int(not subscription and cost is None)
    pg = 0 if not subscription else (1 if pmax is None else (2 if pmax >= .95 else 0))
    return eligible, (cg, pg, -value), terms, inputs, float(value)


def draw(rng, pool, valid):
    return valid if rng.random() < .5 else rng.choice(pool)


def fields(rng, pool, values):
    result = {}
    for key, valid in values.items():
        value = draw(rng, pool, valid)
        if value is not MISSING:
            result[key] = value
    return result


def generate(rng, pool, index):
    def quota():
        return fields(rng, pool, {
            "value": rng.uniform(0, 1), "provenance": rng.choice(("measured", "estimated")),
            "as_of": T0,
        })

    q = fields(rng, pool, {"pressure": quota(), "weekly_pressure": quota()})
    cost = fields(rng, pool, {"value": rng.uniform(0, .5)})
    return fields(rng, pool, {
        "resource": "r" + str(index), "kind": rng.choice(("subscription", "api", "local")),
        "chain_id": "p" + str(index) + "/m",
        "status": rng.choice(("connected", "connected", "absent")),
        "capabilities": rng.choice((["generate"], ["generate", "tools"], ["tools"])),
        "quota": q, "reset_at": datetime.fromtimestamp(
            T0 + rng.choice((-3600, 36, 3600, 18000)), timezone.utc,
        ).isoformat(),
        "marginal_cost": cost, "effective_priority": rng.randint(1, 4),
        "competence": rng.choice((
            None, {"code": {"simple": {"eligible": False}}},
            {"code": {"simple": {"eligible": True}}},
            {"code": {"simple": {"eligible": False}, "complex": {}}},
        )),
    })


def test_seeded_fuzz(monkeypatch):
    rng = random.Random(2304)
    Poison.touches = 0
    pool = [MISSING, None, "unknown", "", float("nan"), float("inf"), -1,
            True, False, [], {}, object(), Poison()]
    monkeypatch.setattr(rm.time, "time", lambda: T0)
    all_unknown = ran = 0
    for case in range(500):
        rows = [generate(rng, pool, i) for i in range(rng.randint(1, 6))]
        decision = fields(rng, pool, {
            "task_type": rng.choice(("code", "query")),
            "needs": rng.choice(((), ["tools"], ["audio"], ["tools", 7])),
            "now": T0, "priority_ordered": True,
        })
        decision = draw(rng, pool, decision)
        if decision is MISSING:
            decision = None
        expected = [oracle(r, decision) for r in rows]
        for r, (_, _, terms, inputs, value) in zip(rows, expected):
            actual, explain = rm.score(r, decision)
            assert math.isfinite(actual) and type(actual) is float
            assert actual == value, case
            assert explain["terms"] == terms
            assert explain["inputs"] == inputs
            assert set(explain) == {"resource", "score", "terms", "inputs"}
            all_unknown += int(all(v == "unknown" for v in terms.values()))
        ordered, explains = rm.order_resources(rows, decision)
        wanted = sorted((i for i, e in enumerate(expected) if e[0]),
                        key=lambda i: (*expected[i][1], i))
        assert [id(r) for r in ordered] == [id(rows[i]) for i in wanted], case
        assert len(explains) == len(rows)
        ranks = {index: rank for rank, index in enumerate(wanted, 1)}
        for i, e in enumerate(explains):
            resource = rows[i].get("resource")
            assert e["resource"] == (resource if isinstance(resource, str) else "unknown")
            if expected[i][0]:
                assert set(e) == {"resource", "score", "rank", "terms", "inputs"}
                assert type(e["rank"]) is int and e["rank"] == ranks[i]
                assert type(e["score"]) is float and math.isfinite(e["score"])
                assert e["terms"] == expected[i][2]
                assert e["inputs"] == expected[i][3]
                assert all(type(v) is float and math.isfinite(v) or v == "unknown"
                           for v in e["terms"].values())
            else:
                assert set(e) == {"resource", "filtered"}
                assert isinstance(e["filtered"], str)
        chain = [r["chain_id"] for r in rows if isinstance(r.get("chain_id"), str)]
        chain += ["ollama/local", "openai/api"]
        result, _ = rm.order_chain(chain, {"policy": "balanced", "resources": rows}, decision)
        assert Counter(result) == Counter(chain)
        ran += 1
    assert ran == 500
    assert all_unknown >= 1
    # Prereg §6: a broad except must not hide a touch of an unknown value.
    assert Poison.touches == 0
    assert Poison.touches == 0
