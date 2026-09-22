"""Turn silent DIRECT-execution timeouts into one actionable sentence.

GH#57. When the local-draft path timed out, auto-route.py logged "DIRECT
FAILED: falling through to Claude" to the debug log and continued. Routing
still worked — it fell through to Claude — so nothing looked broken, and the
only way to learn that the local path never once succeeded was to read a debug
log by hand.

On the reporter's machine the smallest of three pulled models answered trivial
one-line questions in 9.9-33.6s against a 4s default, and a larger one took
153s. Every call timed out, on every prompt, silently.

The request was specifically for TWO messages, because two different truths
need different advice:

  * The model answers, just slower than the timeout -> name a concrete number.
  * The model cannot answer at interactive speed at all -> say that, and stop
    offering a knob that will not help.

Telling someone with a 153s model to raise a timeout sends them in circles;
telling someone with a 12s model that local routing is hopeless is wrong. The
distinction is the whole feature, so it is the thing under test.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, NamedTuple, Optional

# Below this, a bigger timeout buys a working local path.
_VIABLE_CEILING_S = 60.0
# One slow call is noise. Advice on n=1 trains people to ignore advice.
_MIN_SAMPLES = 2
# If most calls succeed, a slow outlier is not a diagnosis.
_TIMEOUT_SHARE = 0.5
# Headroom over the observed p90 so the suggestion does not immediately re-fail.
_HEADROOM = 1.25


class Sample(NamedTuple):
    """One observed DIRECT attempt."""

    elapsed_s: float
    timed_out: bool


@dataclass(frozen=True)
class Advice:
    kind: Literal["raise_timeout", "too_slow_for_local", "failing_fast"]
    message: str
    #: None, not 0.0 (T-23). A default of 0.0 on advice that is NOT about the
    #: timeout reads as "set it to zero" — which makes every call fail
    #: instantly. Only `raise_timeout` carries a number, and only a usable one.
    suggested_timeout_s: float | None = None


#: Below this, a "timeout" is not a timeout (T-23).
#:
#: A real timeout takes approximately as long as the timeout. A DIRECT attempt
#: that returns in under a second failed for some other reason — no free model,
#: a grounding rejection, an unreachable Ollama — and calling it a timeout sends
#: the operator to the one setting that cannot help.
_MIN_TIMEOUT_ELAPSED_S: float = 1.0


def looks_like_timeout(elapsed_s: float | None) -> bool:
    """Could a failure that took `elapsed_s` plausibly have been a timeout?"""
    try:
        return float(elapsed_s or 0.0) >= _MIN_TIMEOUT_ELAPSED_S
    except (TypeError, ValueError):
        return False


def _p90(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))
    return ordered[idx]


def diagnose(
    samples: Optional[Iterable[Sample]], timeout_s: float = 4.0
) -> Optional[Advice]:
    """Return one piece of advice, or None when there is nothing worth saying.

    Never raises: this runs inside a PreToolUse/UserPromptSubmit hook, where an
    exception costs the user their prompt. Junk input yields None.
    """
    try:
        rows = [s for s in (samples or []) if s.elapsed_s is not None and s.elapsed_s >= 0]
    except TypeError:
        return None
    if len(rows) < _MIN_SAMPLES:
        return None

    # T-23. A sample flagged `timed_out` that finished in ~0s did not time out:
    # it is a "no free model" or a grounding rejection that the hook filed under
    # the wrong flag. Excluded here as well as fixed at the source, because the
    # samples already on disk carry the old labelling — 17 of 20 on the audited
    # machine — and this function has to be right about THAT data too.
    timeouts = [s for s in rows if s.timed_out and looks_like_timeout(s.elapsed_s)]
    mislabelled = [s for s in rows if s.timed_out and not looks_like_timeout(s.elapsed_s)]
    if not timeouts or len(timeouts) / len(rows) < _TIMEOUT_SHARE:
        # Mostly working, or the "timeouts" were instant failures of some other
        # kind. Either way a longer timeout is not the fix.
        if mislabelled and len(mislabelled) / len(rows) >= _TIMEOUT_SHARE:
            return Advice(
                kind="failing_fast",
                message=(
                    f"{len(mislabelled)} of {len(rows)} local attempts failed in "
                    f"under {_MIN_TIMEOUT_ELAPSED_S:g}s. That is not a timeout — "
                    f"raising LLM_ROUTER_OLLAMA_TIMEOUT will not help. Check that a "
                    f"local model is installed and that Ollama is running "
                    f"(`ollama list`), and see `llm-router doctor` for the "
                    f"provider section."
                ),
            )
        return None

    observed = _p90([s.elapsed_s for s in timeouts])

    if observed >= _VIABLE_CEILING_S:
        return Advice(
            kind="too_slow_for_local",
            message=(
                f"local model calls are taking ~{observed:.0f}s, which is too slow "
                f"for an interactive round trip however the timeout is set. "
                f"Consider a smaller model, or leave LLM_ROUTER_DIRECT_EXECUTION=false "
                f"and let routing fall through."
            ),
        )

    suggested = round(observed * _HEADROOM)
    # T-23, belt and braces: never advise a timeout at or below the current one.
    # `LLM_ROUTER_OLLAMA_TIMEOUT=0` guarantees every call fails instantly, which
    # is how a diagnostic came to recommend the failure it was diagnosing.
    if suggested <= timeout_s:
        return None
    return Advice(
        kind="raise_timeout",
        message=(
            f"local calls are averaging ~{observed:.0f}s against a {timeout_s:.0f}s "
            f"timeout, so DIRECT execution never completes. "
            f"Set LLM_ROUTER_OLLAMA_TIMEOUT={int(suggested)} to match this machine."
        ),
        suggested_timeout_s=float(suggested),
    )


# ── Persistence ────────────────────────────────────────────────────────────
# A single hook invocation sees one attempt; the advice needs a handful. The
# samples therefore outlive the process, in a small ring buffer beside the
# other router state. Every operation is best-effort: recording a diagnostic
# must never be the reason a prompt fails.

_MAX_SAMPLES = 20


def _samples_path(home=None):
    from pathlib import Path

    # R11: go through the canonical resolver. `(home or Path.home())` is a
    # hand-copied resolver that never consults LLM_ROUTER_HOME, so an
    # "isolated" probe wrote direct_samples.jsonl into the operator's REAL
    # home. Reproduced 2026-09-22. `home` stays an explicit override for the
    # callers that pass one.
    if home is not None:
        return Path(home) / ".llm-router" / "direct_samples.jsonl"
    from llm_router import paths as _paths
    return _paths.state_path("direct_samples.jsonl")


def record_sample(elapsed_s: float, timed_out: bool, home=None) -> None:
    """Append one DIRECT attempt, keeping only the most recent _MAX_SAMPLES."""
    import json

    try:
        path = _samples_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        if path.exists():
            lines = path.read_text().splitlines()[-(_MAX_SAMPLES - 1):]
        lines.append(json.dumps({"elapsed_s": round(float(elapsed_s), 3),
                                 "timed_out": bool(timed_out)}))
        path.write_text("\n".join(lines) + "\n")
    except Exception as _exc:  # noqa: BLE001 — a diagnostic must never break the hook it runs in
        # T-14: still fail-open, but no longer SILENT. A failed write here loses
        # a DIRECT-execution sample, the input to `doctor`'s advice and reported nothing: no error, no log, no counter. 92
        # persistence sites had this shape; this is one of the ones that loses
        # data a user would notice missing.
        try:
            from llm_router import failopen as _fo
            _fo.record("CHZ-FO-DIRECT-SAMPLE-WRITE", _exc)
        except Exception:  # noqa: BLE001
            pass


def load_samples(home=None) -> list:
    import json

    out = []
    try:
        path = _samples_path(home)
        if not path.exists():
            return out
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                out.append(Sample(float(d["elapsed_s"]), bool(d["timed_out"])))
            except Exception:
                continue
    except Exception:
        return []
    return out


def current_advice(timeout_s: float = 4.0, home=None) -> Optional[Advice]:
    """Advice from the persisted samples, or None."""
    return diagnose(load_samples(home), timeout_s=timeout_s)
