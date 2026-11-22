"""PR-3: the embedding classifier backend (capability, OFF by default, not wired into any decision).

``classify(text) -> Verdict`` embeds the assembled prompt on the SMALL Ollama server (default :11434, never the
server of the main local model, never :11435) and scores it against a ``local_ok`` head artifact (centroids).
It answers ``tier`` ``local`` / ``claude`` or ``None`` (abstain). Nothing in the proxy's decision calls it yet
(PR-4 shadow, PR-7 authority); the only live hook is :func:`warm_up` at proxy start, and only when enabled.

Safety, each one tested:

* flag ``LLM_ROUTER_CLS_EMBED`` (``off`` default | ``shadow``); anything else is off.
* kill file ``cls_embed.off`` in the state dir, checked on every call (a plist key needs a restart, a file does not).
* URL guard: loopback only, never ``main_url()``, never port 11435. A refused URL abstains (``guard``).
* hard budget (``LLM_ROUTER_CLS_EMBED_BUDGET_MS``, 250 default): over it the answer is abstain (``timeout``).
* breaker: 5 consecutive errors open it for 60 s (``breaker``); in-flight depth 4, overflow abstains (``queue_full``).
* no artifact, an unreadable or hash-mismatched artifact, a dim mismatch: abstain; a bad artifact is also a failopen row.
* the verdict carries numbers and codes only; the text, the vector and the artifact path never leave this module.
* deliberately does not import ``local_classifier`` (the chat classifier) and does not touch the main model's server.

Artifact format (same family as ``semantic_centroids.json``): ``{"embedding_model", "dim", "temperature",
"confidence_floor", "heads": {"local": [[..]], "claude": [[..]]}, "provenance", "sha256"}`` where ``sha256`` is the
hash of the canonical JSON of every other key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from llm_router import failopen
from llm_router.paths import state_path

FLAG_ENV = "LLM_ROUTER_CLS_EMBED"
URL_ENV = "LLM_ROUTER_CLS_EMBED_URL"
MODEL_ENV = "LLM_ROUTER_CLS_EMBED_MODEL"
BUDGET_ENV = "LLM_ROUTER_CLS_EMBED_BUDGET_MS"
ARTIFACT_ENV = "LLM_ROUTER_CLS_EMBED_ARTIFACT"
KEEP_ALIVE_ENV = "LLM_ROUTER_CLS_EMBED_KEEP_ALIVE"

KILL_FILE = "cls_embed.off"
DEFAULT_MODEL = "nomic-embed-text"
DEFAULT_BUDGET_MS = 250.0
DEFAULT_KEEP_ALIVE = "30m"
WARM_TIMEOUT_S = 10.0
FORBIDDEN_PORT = 11435
BREAKER_ERRORS = 5
BREAKER_OPEN_S = 60.0
MAX_IN_FLIGHT = 4
TIERS = ("local", "claude")

# Verdict sources. Only SRC_EMBED carries a tier.
SRC_EMBED = "embed"
SRC_DISABLED = "disabled"
SRC_KILLED = "killed"
SRC_NO_ARTIFACT = "no_artifact"
SRC_BAD_ARTIFACT = "bad_artifact"
SRC_GUARD = "guard"
SRC_BREAKER = "breaker"
SRC_QUEUE_FULL = "queue_full"
SRC_TIMEOUT = "timeout"
SRC_ERROR = "error"
SRC_DIM = "dim"
SRC_BELOW_FLOOR = "below_floor"
SRC_NO_TEXT = "no_text"

FO_ARTIFACT = "LR-FO-CLS-EMBED-ARTIFACT"
FO_EMBED = "LR-FO-CLS-EMBED-CALL"
FO_WARM = "LR-FO-CLS-EMBED-WARM"


@dataclass(frozen=True)
class Verdict:
    """Text-free. ``tier`` is set only when ``source == "embed"``; any other source is an abstain."""

    tier: str | None
    source: str
    ms: float
    score: float | None = None
    margin: float | None = None
    artifact_sha: str | None = None
    queue_depth: int = 0

    @property
    def decided(self) -> bool:
        return self.source == SRC_EMBED and self.tier is not None

    def as_record(self) -> dict:
        """The ledger fields (numbers and codes only)."""
        return {"cls_emb_tier": self.tier, "cls_emb_source": self.source, "cls_emb_ms": self.ms,
                "cls_emb_score": self.score, "cls_emb_margin": self.margin,
                "cls_emb_artifact_sha": self.artifact_sha, "cls_emb_queue_depth": self.queue_depth}


@dataclass(frozen=True)
class Head:
    model: str
    dim: int
    temperature: float
    floor: float
    protos: dict  # label -> list of unit vectors
    sha: str


# -- configuration ------------------------------------------------------------------------------


def enabled() -> bool:
    return os.environ.get("LLM_ROUTER_CLS_EMBED", "").strip().lower() == "shadow"


def killed() -> bool:
    try:
        return state_path(KILL_FILE).exists()
    except OSError:
        return True  # cannot tell: stay off


def budget_s() -> float:
    try:
        v = float(os.environ.get("LLM_ROUTER_CLS_EMBED_BUDGET_MS", ""))
        if 0 < v <= 2000:
            return v / 1000.0
    except ValueError:
        pass
    return DEFAULT_BUDGET_MS / 1000.0


def model_name() -> str:
    return os.environ.get("LLM_ROUTER_CLS_EMBED_MODEL", "").strip() or DEFAULT_MODEL


def keep_alive() -> str:
    return os.environ.get("LLM_ROUTER_CLS_EMBED_KEEP_ALIVE", "").strip() or DEFAULT_KEEP_ALIVE


def embed_url() -> str:
    raw = os.environ.get("LLM_ROUTER_CLS_EMBED_URL", "").strip()
    if raw:
        return raw.rstrip("/")
    from llm_router.toolkit.adapters.ollama import get_ollama_url
    return get_ollama_url().rstrip("/")


def _hostport(url: str) -> tuple[str, int] | None:
    try:
        p = urlparse(url if "//" in url else f"http://{url}")
        if not p.hostname:
            return None
        return p.hostname.lower(), p.port or 80
    except ValueError:
        return None


def url_ok(url: str) -> bool:
    """True when ``url`` is a loopback server that is neither the main local model's server nor port 11435."""
    hp = _hostport(url)
    if hp is None or hp[0] not in ("localhost", "127.0.0.1", "::1"):
        return False
    if hp[1] == FORBIDDEN_PORT:
        return False
    try:
        from llm_router.toolkit.adapters.ollama import main_url
        main = _hostport(main_url())
    except Exception:  # noqa: BLE001 - cannot name the main server: refuse rather than guess
        return False
    if main is not None and (main[1] == hp[1] and main[0] in ("localhost", "127.0.0.1", "::1")):
        return False
    return True


# -- artifact -------------------------------------------------------------------------------------


def artifact_path() -> Path:
    raw = os.environ.get("LLM_ROUTER_CLS_EMBED_ARTIFACT", "").strip()
    return Path(raw) if raw else state_path("cls_embed_head.json")


def _canonical_sha(raw: dict) -> str:
    body = {k: v for k, v in raw.items() if k != "sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _unit(v: list[float]) -> list[float] | None:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n > 0 else None


_head_cache: dict = {}  # path str -> (mtime_ns, size, Head | str)   str = failure source


def load_head(path: Path | None = None) -> tuple[Head | None, str | None]:
    """``(head, None)`` or ``(None, source)``. No file is ``no_artifact`` (silent); a bad one is ``bad_artifact``
    and a failopen row, once per file version."""
    p = path or artifact_path()
    try:
        st = p.stat()
    except OSError:
        return None, SRC_NO_ARTIFACT
    key = (st.st_mtime_ns, st.st_size)
    hit = _head_cache.get(str(p))
    if hit is not None and hit[0] == key:
        return (hit[1], None) if isinstance(hit[1], Head) else (None, hit[1])
    head: Head | None = None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        sha = str(raw.get("sha256") or "")
        if not sha or sha != _canonical_sha(raw):
            raise ValueError("artifact hash mismatch")
        dim = int(raw["dim"])
        protos: dict = {}
        for label in TIERS:
            vecs = []
            for v in (raw.get("heads") or {}).get(label) or []:
                if len(v) != dim:
                    raise ValueError("artifact prototype dim mismatch")
                u = _unit([float(x) for x in v])
                if u is not None:
                    vecs.append(u)
            if vecs:
                protos[label] = vecs
        if set(protos) != set(TIERS):
            raise ValueError("artifact needs both heads")
        head = Head(str(raw["embedding_model"]), dim, float(raw.get("temperature", 0.05)),
                    float(raw.get("confidence_floor", 0.5)), protos, sha)
    except Exception as exc:  # noqa: BLE001 - a bad artifact abstains and is counted
        failopen.record(FO_ARTIFACT, exc)
    _head_cache[str(p)] = (key, head if head is not None else SRC_BAD_ARTIFACT)
    return (head, None) if head is not None else (None, SRC_BAD_ARTIFACT)


def score_vector(vec: list[float], head: Head) -> tuple[str | None, float, float]:
    """``(tier or None, p_max, margin)``: softmax over each label's best cosine; below the floor is None."""
    u = _unit(vec)
    if u is None:
        return None, 0.0, 0.0
    sims = {lab: max(sum(a * b for a, b in zip(u, p)) for p in ps) for lab, ps in head.protos.items()}
    mx = max(sims.values())
    exps = {lab: math.exp((s - mx) / max(head.temperature, 1e-6)) for lab, s in sims.items()}
    z = sum(exps.values())
    probs = {lab: e / z for lab, e in exps.items()}
    top = max(probs, key=probs.get)
    pmax = probs[top]
    other = max(v for k, v in probs.items() if k != top)
    return (top if pmax >= head.floor else None), round(pmax, 4), round(pmax - other, 4)


# -- embed call -----------------------------------------------------------------------------------


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.in_flight = 0
        self.errors = 0
        self.open_until = 0.0


_state = _State()
_clock = time.monotonic  # tests replace it


def reset_state() -> None:
    global _state
    _state = _State()
    _head_cache.clear()


def _post_embed(base: str, text: str, timeout: float) -> list[float] | None:
    from llm_router import local_privacy
    if local_privacy.private_dispatch_active():
        return None  # a secret-door dispatch: this text goes nowhere
    import urllib.request
    payload = json.dumps({"model": model_name(), "input": text, "keep_alive": keep_alive()}).encode()
    req = urllib.request.Request(f"{base}/api/embed", data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    with local_privacy.open_url(req, base_url=base, timeout=timeout) as resp:
        data = json.loads(resp.read())
    embs = data.get("embeddings")
    if isinstance(embs, list) and embs and isinstance(embs[0], list) and embs[0]:
        return [float(x) for x in embs[0]]
    return None


def _abstain(source: str, t0: float, head: Head | None = None, depth: int = 0) -> Verdict:
    return Verdict(None, source, round((_clock() - t0) * 1000.0, 1), artifact_sha=head.sha if head else None,
                   queue_depth=depth)


def classify(text: str) -> Verdict:
    """Never raises; every non-decision is an abstain with a ``source`` saying why."""
    t0 = _clock()
    try:
        if not enabled():
            return _abstain(SRC_DISABLED, t0)
        if killed():
            return _abstain(SRC_KILLED, t0)
        if not (text or "").strip():
            return _abstain(SRC_NO_TEXT, t0)
        head, why = load_head()
        if head is None:
            return _abstain(why or SRC_NO_ARTIFACT, t0)
        base = embed_url()
        if not url_ok(base):
            return _abstain(SRC_GUARD, t0, head)
        st = _state
        with st.lock:
            if st.open_until > t0:
                return _abstain(SRC_BREAKER, t0, head, st.in_flight)
            if st.in_flight >= MAX_IN_FLIGHT:
                return _abstain(SRC_QUEUE_FULL, t0, head, st.in_flight)
            st.in_flight += 1
            depth = st.in_flight
        budget = budget_s()
        try:
            vec = _post_embed(base, text, max(budget - (_clock() - t0), 0.01))
            err = None
        except TimeoutError:
            vec, err = None, SRC_TIMEOUT
        except Exception as exc:  # noqa: BLE001 - a socket timeout also arrives wrapped in URLError
            vec, err = None, (SRC_TIMEOUT if "timed out" in str(exc) else SRC_ERROR)
            failopen.record(FO_EMBED, exc)
        finally:
            with st.lock:
                st.in_flight -= 1
        if err is None and (_clock() - t0) > budget:
            err = SRC_TIMEOUT
        with st.lock:
            if err is None and vec is not None:
                st.errors = 0
            else:
                st.errors += 1
                if st.errors >= BREAKER_ERRORS:
                    st.open_until = _clock() + BREAKER_OPEN_S
                    st.errors = 0
        if err is not None:
            return _abstain(err, t0, head, depth)
        if vec is None:
            return _abstain(SRC_ERROR, t0, head, depth)
        if len(vec) != head.dim:
            return _abstain(SRC_DIM, t0, head, depth)
        tier, pmax, margin = score_vector(vec, head)
        ms = round((_clock() - t0) * 1000.0, 1)
        if tier is None:
            return Verdict(None, SRC_BELOW_FLOOR, ms, pmax, margin, head.sha, depth)
        return Verdict(tier, SRC_EMBED, ms, pmax, margin, head.sha, depth)
    except Exception as exc:  # noqa: BLE001 - a recording must never cost a call
        failopen.record(FO_EMBED, exc)
        return _abstain(SRC_ERROR, t0)


async def classify_async(text: str) -> Verdict:
    return await asyncio.to_thread(classify, text)


# -- warm-up --------------------------------------------------------------------------------------


def warm_up() -> str:
    """Load the embed model once at proxy start (the cold first call is far over the budget) and hold it with
    ``keep_alive``. Returns a short status code; never raises. No-op unless enabled, not killed, URL allowed."""
    try:
        if not enabled():
            return SRC_DISABLED
        if killed():
            return SRC_KILLED
        base = embed_url()
        if not url_ok(base):
            return SRC_GUARD
        try:  # the one choke point before any model load (memory budget, one-model rule)
            from llm_router.toolkit.adapters.ollama import ensure_fits
            ensure_fits(model_name(), base, "cls_embed.warm_up")
        except Exception as exc:  # noqa: BLE001 - refused or unreadable: do not load
            failopen.record(FO_WARM, exc, detail="ensure_fits")
            return "refused"
        _post_embed(base, "warm-up", WARM_TIMEOUT_S)
        return "ok"
    except Exception as exc:  # noqa: BLE001
        failopen.record(FO_WARM, exc)
        return SRC_ERROR
