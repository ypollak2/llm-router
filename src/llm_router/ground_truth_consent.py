"""Consent for prompt capture — recorded, revocable, and required. R8.

The audit's finding was not that capture is dangerous. It was that the product
CLAIMED to preserve task success while the only mechanism that could measure it
was off by default, so the claim rested on nothing.

Two ways out were defensible: turn capture on and measure, or drop the claim.
The decision taken was to turn it on — **with explicit consent at install**,
because the thing being switched on reads the operator's prompts.

What that means concretely, and what this module enforces:

* **Consent is a recorded event, not a flag.** `~/.llm-router/gt_consent.json`
  carries when it was given, what version of the terms was shown, and what the
  answer was. A flag alone cannot answer "did anyone actually agree to this?",
  and that is the only question that matters if it turns out they did not.

* **Silence is refusal.** A non-interactive install — CI, a Dockerfile, `yes |`
  piped into onboarding — records NO consent and capture stays off. Defaulting
  to on when nobody could answer is the shape of every dark pattern in this
  space, and it is also just wrong: an unattended machine cannot agree to
  anything.

* **The terms carry a version.** Changing what is captured changes what was
  agreed to. `TERMS_VERSION` moves, prior consent stops matching, and the
  operator is asked again rather than being held to an agreement about
  something else.

* **Revocation is one command and takes effect immediately**, because consent
  that is hard to withdraw is not meaningfully given.

Capture itself still reads `LLM_ROUTER_GROUND_TRUTH`. An operator who sets that
variable by hand has consented by doing so — this module is about the INSTALL
path, where the software would otherwise be deciding on their behalf.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "TERMS_VERSION",
    "TERMS",
    "Consent",
    "consent_path",
    "read_consent",
    "record_consent",
    "revoke",
    "has_current_consent",
]

#: Bump whenever WHAT IS CAPTURED changes. Prior consent then stops matching
#: and the operator is asked again.
TERMS_VERSION = 1

TERMS = """\
  Ground Truth capture records your PROMPTS (not your files, not your repo
  contents) so llm-router can measure whether routing to a cheaper model
  actually preserved the answer quality.

  What is written:   the prompt text, the task type, which model was chosen
  Where it goes:     ~/.llm-router/prompt_capture.jsonl — LOCAL ONLY.
                     Nothing is uploaded. There is no server.
  What is removed:   API keys, tokens, and any term you list in a denylist
                     file are scrubbed before the line is written.
  Turn it off:       llm-router gt-consent --revoke
                     (or unset LLM_ROUTER_GROUND_TRUTH)

  Without this, llm-router cannot tell you whether it preserved task success.
  It will still route, and still report savings — but the quality half of the
  claim would rest on nothing measured."""


@dataclass(frozen=True)
class Consent:
    granted: bool
    terms_version: int
    recorded_at: float
    #: How the answer was obtained: "interactive" or "non-interactive". A
    #: non-interactive record is always a refusal, and saying so makes the
    #: difference auditable rather than inferred from the timestamp.
    source: str = "interactive"

    @property
    def is_current(self) -> bool:
        return self.granted and self.terms_version == TERMS_VERSION


def consent_path() -> Path:
    from llm_router.paths import state_path

    return state_path("gt_consent.json")


def read_consent() -> Consent | None:
    """The recorded decision, or None if never asked. Never raises."""
    try:
        p = consent_path()
        if not p.exists():
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        return Consent(
            granted=bool(d.get("granted")),
            terms_version=int(d.get("terms_version", 0)),
            recorded_at=float(d.get("recorded_at", 0.0)),
            source=str(d.get("source", "interactive")),
        )
    except Exception:  # noqa: BLE001
        # An unreadable record is NOT consent. Fail closed: the cost of asking
        # again is a prompt; the cost of assuming yes is capturing someone's
        # prompts without agreement.
        return None


def record_consent(granted: bool, *, source: str = "interactive") -> Consent:
    """Write the decision. Returns what was recorded."""
    c = Consent(
        granted=bool(granted),
        terms_version=TERMS_VERSION,
        recorded_at=time.time(),
        source=source,
    )
    p = consent_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    from llm_router.paths import private_opener

    with open(p, "w", opener=private_opener) as fh:
        fh.write(json.dumps({
            "granted": c.granted,
            "terms_version": c.terms_version,
            "recorded_at": c.recorded_at,
            "source": c.source,
        }, indent=2))
    return c


def revoke() -> None:
    """Withdraw consent. Immediate, and recorded as a refusal rather than
    deleted — "they said no" and "they were never asked" are different facts."""
    record_consent(False, source="revoked")


def has_current_consent() -> bool:
    c = read_consent()
    return bool(c and c.is_current)


def ask(*, interactive: bool, reader=input, printer=print) -> Consent:
    """Show the terms and record the answer.

    `interactive=False` records a refusal WITHOUT prompting. That is the whole
    point: a CI run or a Dockerfile cannot agree to anything, and a default-yes
    there would be consent manufactured by the absence of a human.
    """
    if not interactive:
        return record_consent(False, source="non-interactive")
    printer("")
    printer("  Ground Truth capture — measure whether routing kept the answer good")
    printer("")
    printer(TERMS)
    printer("")
    try:
        answer = reader("  Enable Ground Truth capture? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        # Could not be asked -> was not asked -> no.
        return record_consent(False, source="non-interactive")
    granted = answer in ("", "y", "yes")
    return record_consent(granted, source="interactive")
