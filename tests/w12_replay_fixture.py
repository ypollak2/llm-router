"""Deterministic fixture generator for the W1-2 ineligible-route replay (UN-09).

Two seeded generators, no I/O, no network:

* :func:`prompt_for` -- a developer-assistant prompt. The population follows the door corpus
  the acceptance spec names (``v16/door_corpus.jsonl``: 2,658 real prompts, length p10 / p50 / p90
  = 34 / 1,800 / 12,147 characters, 14 Hebrew): short questions, mid-size task requests and long
  pastes of code or logs, mostly English and a small Hebrew share. The sentences are written
  here from scratch (no corpus row is copied, none of the router's own keyword lists is read),
  and nothing in them is credential-shaped.
* :func:`map_state_for` -- one resource-map state: which providers the map knows, which of
  those are connected, and which are not (disconnected, unhealthy, absent). The mix is
  weighted towards hostile states (about half of the providers in a chain are not connected)
  so the eligibility filter has real work to do on most requests.

Everything derives from ``(seed, door, index)`` through :class:`random.Random`, so a failure
names the exact request that reproduces it.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

#: Providers the replay maps. ``ollama`` is local (model-level rows), the rest are cloud rows.
LOCAL_MODELS = ("qwen3:8b", "qwen3-coder:30b", "llama3.2:3b")
CLOUD = {
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-chat",
    "groq": "llama-3.3-70b-versatile",
}
PROVIDERS = ("ollama", *CLOUD)
#: What the map can say about a provider: ``None`` = the map has no row for it at all.
STATUSES = ("connected", "disconnected", "unhealthy", "absent", None)

_SHORT = [
    "why does my docker build keep re-running pip install even though requirements.txt did not change",
    "what is the difference between a mutex and a semaphore",
    "give me a regex that matches an ISO 8601 date with an optional time zone",
    "is it safe to run VACUUM on a sqlite file while another process has it open",
    "how do I make git forget a file that is already tracked without deleting it",
    "explain eventual consistency to a product manager in three sentences",
    "what does errno 98 mean on linux",
    "tl;dr of the CAP theorem please",
    "why is my react component rendering twice in development",
    "convert this to a list comprehension: for x in xs: if x > 0: out.append(x * 2)",
    "how long should a JWT access token live",
    "מה ההבדל בין תהליך לחוט במערכת הפעלה",
    "תסביר לי בקצרה מה זה אינדקס מורכב במסד נתונים",
    "can you rename the variable tmp to retry_budget everywhere in this function",
    "what is the idiomatic way to retry an http call with backoff in python",
]
_MEDIUM_OPENERS = [
    "I'm working on a service that ingests webhook events and writes them to postgres.",
    "We have a nightly job that rebuilds a search index and it has started taking twice as long.",
    "The mobile team reports that the login screen hangs on slow connections.",
    "Our CI went red after the dependency bump and I cannot tell which of the three changes did it.",
    "I need to migrate a table with about forty million rows without locking writes.",
    "Reviewing a pull request that touches the billing retry logic and I want a second opinion.",
    "The staging cluster keeps evicting pods whenever the batch worker scales up.",
    "Writing the design doc for a feature flag service that has to work offline.",
]
_MEDIUM_ASKS = [
    "Walk me through the likely causes, most probable first, and what to check for each.",
    "Propose a plan with the smallest safe first step and a way to roll it back.",
    "Write the function, with tests, and call out any edge case you are unsure about.",
    "Summarise the trade-offs between the two approaches and recommend one.",
    "Find the bug in the snippet below and explain why it only shows up under load.",
    "Draft a short message to the team explaining what happened and what we change next.",
    "List what could go wrong, ordered by blast radius.",
]
_LONG_BLOCKS = [
    "Traceback (most recent call last):\n  File \"worker.py\", line {n}, in run\n    result = handler(event)\n  File \"handlers.py\", line {m}, in handler\n    return store.write(event['id'], payload)\nKeyError: 'id'\n",
    "def merge_ranges(ranges):\n    out = []\n    for lo, hi in sorted(ranges):\n        if out and lo <= out[-1][1]:\n            out[-1][1] = max(out[-1][1], hi)\n        else:\n            out.append([lo, hi])\n    return out\n",
    "2026-10-{d}T09:{m}:11Z WARN  queue depth {n} exceeds soft limit 500 (consumer lag 41s)\n2026-10-{d}T09:{m}:19Z INFO  autoscaler: scaling consumers 4 -> 6\n2026-10-{d}T09:{m}:44Z ERROR upstream timeout after 30000ms (host=ledger-{n})\n",
    "SELECT o.id, o.total, c.email\nFROM orders o\nJOIN customers c ON c.id = o.customer_id\nWHERE o.created_at > now() - interval '{d} days'\n  AND o.status IN ('paid', 'refunded')\nORDER BY o.total DESC\nLIMIT {n};\n",
    "export async function fetchPage(cursor: string | null) {\n  const res = await fetch(`/api/items?cursor=${cursor ?? ''}`);\n  if (!res.ok) throw new Error(`status ${res.status}`);\n  return (await res.json()) as Page<Item>;\n}\n",
]
_HE_MEDIUM = [
    "יש לנו שירות שקורא הודעות מתור ושומר אותן בבסיס נתונים, והוא מאט בשעות העומס. איך מאבחנים את זה צעד אחר צעד?",
    "תכתוב לי פונקציה בפייתון שמאחדת טווחים חופפים ותוסיף בדיקות לשולי המקרים.",
]


def _fill(block: str, rng: random.Random) -> str:
    return (block.replace("{n}", str(rng.randint(3, 4800))).replace("{m}", f"{rng.randint(0, 59):02d}")
            .replace("{d}", f"{rng.randint(1, 28):02d}"))


def prompt_for(seed: int, door: str, index: int) -> str:
    """The prompt for request *index* of *door* (deterministic in the three arguments)."""
    rng = random.Random(f"{seed}/{door}/{index}/prompt")
    roll = rng.random()
    if roll < 0.30:                       # short question (door corpus p10 = 34 chars)
        return rng.choice(_SHORT)
    if roll < 0.33:                       # Hebrew share (14 of 2,658 in the corpus, boosted so it is exercised)
        return rng.choice(_HE_MEDIUM)
    text = f"{rng.choice(_MEDIUM_OPENERS)} {rng.choice(_MEDIUM_ASKS)}"
    if roll < 0.60:                       # mid-size request
        return text
    blocks = [_fill(b, rng) for b in rng.sample(_LONG_BLOCKS, k=rng.randint(1, 3))]
    return text + "\n\n" + "\n".join(blocks)   # long paste (door corpus p90 = 12k chars; kept shorter for speed)


@dataclass(frozen=True)
class MapState:
    """One resource-map state plus the candidate chain a door would start from."""
    statuses: dict[str, str | None]       # provider -> map status, None = the map has no row
    base_chain: tuple[str, ...]           # candidate ``provider/model`` ids, in the order the door starts from
    task: str                             # the label used for the router-style door (query / code / ...)
    complexity: str

    @property
    def local_models(self) -> list[str]:
        return [m for m in self.base_chain if m.startswith("ollama/")]


def map_state_for(seed: int, door: str, index: int) -> MapState:
    rng = random.Random(f"{seed}/{door}/{index}/map")
    statuses: dict[str, str | None] = {}
    for p in PROVIDERS:
        statuses[p] = rng.choices(STATUSES, weights=(52, 18, 12, 10, 8))[0]
    if not any(s == "connected" for s in statuses.values()) and rng.random() < 0.9:
        statuses[rng.choice(PROVIDERS)] = "connected"   # a few all-down maps stay: those chains must come back empty
    cands: list[str] = [f"ollama/{m}" for m in LOCAL_MODELS] + [f"{p}/{m}" for p, m in CLOUD.items()]
    rng.shuffle(cands)
    base = tuple(cands[: rng.randint(3, len(cands))])
    task = rng.choices(("code", "query", "analyze", "generate"), weights=(40, 25, 20, 15))[0]
    complexity = rng.choices(("simple", "moderate", "complex"), weights=(45, 40, 15))[0]
    return MapState(statuses=statuses, base_chain=base, task=task, complexity=complexity)


def map_document(state: MapState, generated_at: str) -> dict:
    """The resource-map JSON for *state*: one row per local model the chain carries and one per cloud provider.

    A local provider's rows all share the provider's status (the map is built from the Ollama
    inventory, so a model that is installed and listed is a row; the daemon being down takes
    every row down). A provider whose status is ``None`` has no row at all."""
    from llm_router import resource_map as rm

    rows = []
    prio = 0
    for p in PROVIDERS:
        st = state.statuses[p]
        if st is None:
            continue
        ids = [m for m in state.base_chain if m.startswith(f"{p}/")] or [f"{p}/{CLOUD.get(p, LOCAL_MODELS[0])}"]
        for cid in ids:
            prio += 1
            rows.append({"resource": cid, "kind": "local" if p == "ollama" else "api", "chain_id": cid,
                         "status": st, "effective_priority": prio, "capabilities": ["generate"], "quota": {}})
    return {"schema": rm.SCHEMA, "policy": "balanced", "resources": rows, "generated_at": generated_at}


def eligible_by_map_rows(doc: dict, chosen: str) -> bool | None:
    """Independent predicate, straight from the map rows (no ``decision_stamp`` / ``ineligible_routes`` code).

    ``True`` eligible, ``False`` ineligible, ``None`` the map has no row for the chosen provider
    (unknown to the map: the filter leaves such entries alone, and the counter does not judge them)."""
    prov = chosen.split("/", 1)[0]
    rows = [r for r in doc["resources"] if r["chain_id"].split("/", 1)[0] == prov]
    if not rows:
        return None
    if prov == "ollama":
        exact = [r for r in rows if r["chain_id"] == chosen]
        return bool(exact) and exact[0]["status"] == "connected"
    return any(r["status"] == "connected" for r in rows)
