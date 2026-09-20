"""Deterministic scrubbing for ground-truth prompt capture.

Scrubbing runs at WRITE time: nothing sensitive is supposed to reach disk, so
this module is imported by the live capture hook as well as by the offline
corpus extractor.

Relationship to `llm_router.secret_scrubber`
--------------------------------------------
`secret_scrubber.scrub_text()` is the canonical credential scrubber. Its
docstring is explicit — "the single source of truth every content store should
call, replacing the three drifted per-module scrubbers" (CHZ-SEC-01) — so this
module **calls it first** and never re-implements credential patterns. An
earlier version of this file carried its own copy of the key/token regexes,
which would have made it the fourth drift and re-opened the bug CHZ-SEC-01
closed.

What is added on top, and only because the canonical scrubber does not do it:

* **Home-directory paths.** `/Users/<name>/…` identifies a person, is not a
  credential, and the path *shape* is worth keeping so a task still reads as a
  path reference.
* **A literal denylist.** Regexes cannot detect a customer or person name.
  Pretending otherwise is worse than admitting it, so those go in a file the
  operator maintains.
* **Stable placeholders** for the terms this module handles: the same value
  maps to the same token within a dataset, via an HMAC over a per-dataset salt,
  so "the path in prompt A is the path in prompt B" survives scrubbing while the
  path itself does not. `secret_scrubber` deliberately collapses each class to a
  single `[REDACTED-*]` marker instead; that is the safer default for a log and
  is left exactly as it is.
* **`residual_risk()`** — flags shapes that survived, so a record goes to human
  review rather than silently into the dataset.

`scrub()` is idempotent: scrubbing already-scrubbed text is a no-op. The capture
hook and the extractor can both run over the same string, and
`test_groundtruth_scrub.py` pins it.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The canonical credential scrubber. Imported defensively: if the package
# layout ever hides it, capture must fail closed (no scrubbing => no writing)
# rather than quietly fall back to a weaker local copy.
_CANONICAL_ERR: str | None = None
try:
    _src = Path(__file__).resolve().parents[2] / "src"
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))
    from llm_router.secret_scrubber import scrub_text as _canonical_scrub_text
except Exception as exc:  # noqa: BLE001
    _canonical_scrub_text = None  # type: ignore[assignment]
    _CANONICAL_ERR = f"{type(exc).__name__}: {exc}"


def canonical_available() -> tuple[bool, str | None]:
    """Whether `secret_scrubber.scrub_text` could be imported, and why not."""
    return _canonical_scrub_text is not None, _CANONICAL_ERR


# A placeholder this module already emitted. Used for the idempotence guard.
PLACEHOLDER = re.compile(r"<(?:[A-Z][A-Z0-9_]*):[0-9a-f]{4,12}>")

# What the canonical scrubber emits. Protected from this module's rules so the
# two layers compose instead of fighting, and counted so the report shows how
# much each layer caught.
CANONICAL_MARKER = re.compile(r"\[REDACTED-[A-Z0-9_]+\]")

# Default salt. A dataset that needs cross-run stability passes its own; the
# manifest records the salt's fingerprint (never the salt) so two datasets can
# be told apart without either being de-anonymised.
_DEFAULT_SALT = b"llm-router-groundtruth-v1"


def _token(kind: str, value: str, salt: bytes, width: int = 8) -> str:
    digest = hmac.new(salt, value.encode("utf-8", "replace"), hashlib.sha256)
    return f"<{kind}:{digest.hexdigest()[:width]}>"


@dataclass
class Rule:
    kind: str
    pattern: re.Pattern[str]
    group: int = 0
    # When set, only the named group is replaced and the rest of the match is
    # kept. Lets us scrub `Bearer <token>` without eating the word "Bearer".
    keep_prefix: bool = False


# ── Supplementary rules ──────────────────────────────────────────────────────
# ONLY patterns `secret_scrubber.SECRET_PATTERNS` does not already cover. Every
# credential family it handles — OpenAI/Anthropic/Google/AWS keys, GitHub
# tokens, bearer/authorization headers, `KEY=value` assignments, passwords,
# generic secrets and PEM private-key blocks — is deliberately absent here.
# Duplicating one would recreate the drift CHZ-SEC-01 closed.
#
# Before adding a rule, check `SECRET_PATTERNS` first. If it belongs there,
# it belongs there — this file is for what a credential scrubber should not be
# doing, namely identity and location.
RULES: list[Rule] = [
    # JWTs: three base64url segments. Not in SECRET_PATTERNS; the generic
    # `token` rule there only fires on an assignment, not a bare JWT.
    Rule("JWT", re.compile(r"\bey[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b")),
    # Vendor prefixes SECRET_PATTERNS does not list.
    Rule("SLACK_TOKEN", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b")),
    Rule("STRIPE_KEY", re.compile(r"\b[rs]k_(?:live|test)_[0-9A-Za-z]{16,}\b")),
    # Connection strings with inline credentials — a URL, not a key assignment.
    Rule("DB_URL", re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s/@]+:[^\s/@]+@[^\s]+")),
    # ── Identity and location. Not credentials; out of scope for a credential
    # scrubber, and the reason this module exists at all. ────────────────────
    Rule("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # Home directories: keep the shape, drop the user. /Users/foo/x -> <HOME:..>/x
    Rule("HOME", re.compile(r"(?:/Users/|/home/|C:\\\\Users\\\\)[^/\\\s\"':,)]+")),
    # Private/loopback addresses are not interesting; public ones might be.
    Rule("IP", re.compile(
        r"\b(?!(?:10|127|169\.254|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.)"
        r"(?:\d{1,3}\.){3}\d{1,3}\b")),
    # E.164-ish phone numbers. Deliberately conservative: requires a + and a
    # separator pattern, so it does not eat version numbers or byte counts.
    Rule("PHONE", re.compile(r"\+\d{1,3}[\s.-]?\(?\d{2,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}\b")),
]


@dataclass
class ScrubReport:
    """What a scrub pass did. Carried into the manifest for auditability."""

    counts: dict[str, int] = field(default_factory=dict)

    def bump(self, kind: str, n: int = 1) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + n

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def merge(self, other: "ScrubReport") -> None:
        for k, v in other.counts.items():
            self.bump(k, v)


def scrub(
    text: str,
    *,
    salt: bytes = _DEFAULT_SALT,
    denylist: list[str] | None = None,
    report: ScrubReport | None = None,
) -> tuple[str, ScrubReport]:
    """Return (scrubbed_text, report).

    `denylist` holds literal strings — customer names, project codenames,
    anything a regex cannot know about. Matching is case-insensitive and
    whole-word where the term is alphanumeric.
    """
    rep = report if report is not None else ScrubReport()
    if not text:
        return text, rep

    if _canonical_scrub_text is None:
        # Fail closed. A weaker local fallback is exactly how the drift this
        # module was rewritten to avoid gets reintroduced.
        raise RuntimeError(
            "llm_router.secret_scrubber.scrub_text is unavailable "
            f"({_CANONICAL_ERR}); refusing to scrub with a partial ruleset.")

    # Layer 1: the canonical credential scrubber, first and unconditionally.
    before_markers = len(CANONICAL_MARKER.findall(text))
    out = _canonical_scrub_text(text)
    rep.bump("CANONICAL", len(CANONICAL_MARKER.findall(out)) - before_markers)

    # Protect placeholders already present — this module's own, and the
    # canonical scrubber's markers — so a second pass is a no-op and layer 2
    # never chews on layer 1's output.
    vault: dict[str, str] = {}

    def _stash(m: re.Match[str]) -> str:
        key = f"\x00PH{len(vault)}\x00"
        vault[key] = m.group(0)
        return key

    out = PLACEHOLDER.sub(_stash, out)
    out = CANONICAL_MARKER.sub(_stash, out)

    # Layer 2: identity, location, and the vendor prefixes layer 1 omits.
    for rule in RULES:
        def _sub(m: re.Match[str], _rule: Rule = rule) -> str:
            rep.bump(_rule.kind)
            secret = m.group(_rule.group)
            token = _token(_rule.kind, secret, salt)
            if _rule.keep_prefix:
                return m.group(1) + token
            return token

        out = rule.pattern.sub(_sub, out)

    for term in denylist or []:
        term = term.strip()
        if not term:
            continue
        flags = re.IGNORECASE
        pat = (re.compile(rf"\b{re.escape(term)}\b", flags)
               if term.isalnum() else re.compile(re.escape(term), flags))

        def _deny(m: re.Match[str]) -> str:
            rep.bump("DENYLIST")
            return _token("REDACTED", m.group(0).lower(), salt)

        out = pat.sub(_deny, out)

    for key, original in vault.items():
        out = out.replace(key, original)
    return out, rep


def salt_fingerprint(salt: bytes = _DEFAULT_SALT) -> str:
    """A safe identifier for the salt, for the manifest. Not reversible."""
    return hashlib.sha256(b"fingerprint:" + salt).hexdigest()[:16]


def residual_risk(text: str) -> list[str]:
    """Flag shapes that survived scrubbing but *look* sensitive.

    This is the honest counterpart to `scrub()`: it does not claim the text is
    clean, it lists what a reviewer should look at. Used to route records into
    the manual-review queue rather than silently into the dataset.
    """
    flags: list[str] = []
    if re.search(r"\b[A-Za-z0-9+/]{40,}={0,2}\b", text):
        flags.append("long-base64-like-blob")
    if re.search(r"\b[0-9a-f]{32,}\b", text, re.I):
        flags.append("long-hex-blob")
    if re.search(r"(?i)\b(confidential|internal only|do not share|nda)\b", text):
        flags.append("confidentiality-marker")
    if re.search(r"\b\d{3}-\d{2}-\d{4}\b", text):
        flags.append("ssn-shaped")
    if re.search(r"\b(?:\d[ -]?){13,19}\b", text):
        flags.append("card-shaped")
    return flags
