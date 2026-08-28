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
    kind: Literal["raise_timeout", "too_slow_for_local"]
    message: str
    suggested_timeout_s: float = 0.0


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

    timeouts = [s for s in rows if s.timed_out]
    if not timeouts or len(timeouts) / len(rows) < _TIMEOUT_SHARE:
        # Mostly working. A slow outlier is not a broken setup.
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

    return (home or Path.home()) / ".llm-router" / "direct_samples.jsonl"


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
    except Exception:
        pass  # a diagnostic must never break the hook it runs in


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
