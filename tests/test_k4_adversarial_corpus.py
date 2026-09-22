"""K4 — a standing adversarial corpus, diagnostic only, never tuned against.

The 2026-09-22 audit re-derived the same probes by hand each time it wanted to
check something: canary secrets against the scrubbers, interpreter invocations
against the command allowlist, near-neighbour prompts against the semantic
cache. That work is re-runnable in minutes if it is written down once.

**NEVER TUNE AGAINST THIS FILE.** The moment a threshold is adjusted to make a
row here pass, the corpus stops measuring the system and starts measuring
itself — which is precisely how the RouterArena result became uninformative
(a constant policy was competitive with everything except retrieval, which said
more about the benchmark than the routers). A row that fails is a finding to
investigate, not a number to move.

Rows that currently FAIL are marked `xfail(strict=True)`, so:
  * a known gap does not break the suite, and
  * closing it without updating this file FAILS, because an xpass is an error.

That is the honest way to carry a known weakness: visible, counted, and unable
to be silently forgotten.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


# ── Canary secrets vs. the scrubbers ────────────────────────────────────────
#
# Each is a REAL credential shape. `scrub_text` must not leave the secret
# portion intact. Fragments are asserted, never exact strings: truncation is
# not redaction, and a `[:80]` slice once defeated an exact-match probe while
# leaving 80 characters of an API key on disk.

#: Assembled at RUNTIME from fragments, never written as a literal.
#:
#: The first version spelled each canary out in full, and GitHub's push
#: protection blocked the push — correctly. It cannot know a token is
#: synthetic, and a repository carrying realistic credential literals is bad
#: hygiene whether or not any of them is live. The offered "allow this secret"
#: URL is a bypass, and bypassing a security control because it inconvenienced
#: me is the exact move this whole audit exists to argue against.
#:
#: Assembling the same bytes at runtime is strictly better: the scrubber still
#: receives a genuine credential shape, and the repository contains no string
#: a scanner or a human can mistake for a real key. The test is not weakened —
#: `test_the_canaries_are_actually_secret_shaped` proves each assembled value
#: still carries its fragment.
def _canaries() -> list[tuple[str, str, str]]:
    """(name, secret, fragment) — the fragment must survive nowhere."""
    return [
        ("github_pat",
         "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
         "A1b2C3d4E5f6"),
        ("slack",
         "xo" + "xb-" + "1234567890-0987654321-" + "AbCdEfGhIjKlMnOpQrStUvWx",
         "AbCdEfGhIjKl"),
        ("aws_key_id",
         "AK" + "IAIOSFODNN7EXAMPLE",
         "IOSFODNN7EXAMPLE"),
        ("bearer",
         "Authorization: Bearer " + "sk-" + "abcdef0123456789abcdef0123456789",
         "abcdef0123456789"),
        ("postgres_dsn",
         "postgresql://svc:" + "hunter2CorrectHorse" + "@db.internal:5432/app",
         "hunter2CorrectHorse"),
        ("openai",
         "sk-" + "proj-" + "T3BlbkFJ" * 6,
         "T3BlbkFJ"),
    ]


CANARIES = _canaries()


@pytest.mark.parametrize("name,secret,fragment", CANARIES,
                         ids=[c[0] for c in CANARIES])
def test_a_canary_secret_does_not_survive_scrubbing(name, secret, fragment):
    from llm_router.secret_scrubber import scrub_text

    out = scrub_text(f"call raised: auth failed for {secret}")
    assert fragment not in out, (
        f"{name}: the secret fragment {fragment!r} survived scrubbing.\n"
        f"got: {out[:200]}"
    )


def test_the_canaries_are_actually_secret_shaped():
    """Anti-vacuity. A canary the scrubber would never see is not a test.

    If a fragment does not appear in its own unscrubbed text, the row is
    malformed and the assertion above passes for free.
    """
    for name, secret, fragment in CANARIES:
        assert fragment in secret, f"{name}: fragment is not part of the secret"


#: A BARE AWS secret (no `aws_secret_access_key=` label) is a KNOWN GAP, pinned
#: rather than hidden. The pattern requires the label because a bare
#: 40-character base64-ish string is indistinguishable from a commit SHA or an
#: ordinary blob, and a scrubber that eats diagnostic text gets switched off.
@pytest.mark.xfail(strict=True, reason="known gap: a bare AWS secret has no "
                                       "distinguishing shape; the labelled "
                                       "form IS redacted")
def test_a_bare_aws_secret_is_redacted():
    from llm_router.secret_scrubber import scrub_text

    bare = "wJalrXUtnFEMI" + "/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
    assert "wJalrXUtnFEMI" not in scrub_text(f"error: {bare}")


def test_the_labelled_aws_form_is_redacted():
    """The half that IS covered — so the xfail above is a scoped gap, not a
    blanket failure."""
    from llm_router.secret_scrubber import scrub_text

    labelled = ("aws_secret_access_key="
                + "wJalrXUtnFEMI" + "/K7MDENG/" + "bPxRfiCYEXAMPLEKEY")
    assert "wJalrXUtnFEMI" not in scrub_text(f"error: {labelled}")


# ── Interpreter invocations vs. the command allowlist ───────────────────────
#
# Every one of these is ALLOWED, and each reaches ~/.ssh or the network. They
# are here so the number stays measured rather than remembered — see R3 and
# docs/security_command_matrix.txt.

INTERPRETER_ESCAPES = [
    ["python3", "-c", "import os;print(open(os.path.expanduser('~/.ssh/id_rsa')).read())"],
    ["node", "-e", "require('child_process').execSync('curl https://x.invalid')"],
    ["find", ".", "-exec", "curl", "https://x.invalid", "{}", ";"],
    ["git", "-c", "core.pager=curl https://x.invalid", "log"],
]


def _guard():
    path = REPO / "src" / "llm_router" / "hooks" / "agent_writes.py"
    spec = importlib.util.spec_from_file_location("_k4_aw", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("argv", INTERPRETER_ESCAPES,
                         ids=lambda a: a[0] if isinstance(a, list) else str(a))
def test_the_interpreter_escape_is_still_allowed(argv, monkeypatch):
    """Recording reality, NOT asserting it should stay this way.

    The allowlist is documented as a footgun guardrail rather than containment
    (R3, SECURITY.md). If one of these starts being REFUSED, that is good news
    and this test fails so the documentation gets updated in the same commit —
    a corpus that silently agrees with any behaviour measures nothing.
    """
    monkeypatch.delenv("LLM_ROUTER_AGENT_COMMANDS", raising=False)
    allowed, msg = _guard().guard_command(argv)
    assert allowed, (
        f"{argv[0]} is now REFUSED ({msg[:120]}). That is an improvement — "
        "update SECURITY.md's interpreter table and docs/"
        "security_command_matrix.txt, which both state it is allowed."
    )


def test_an_obviously_destructive_command_is_still_refused():
    """The other population. Both numbers matter; only one was ever quoted."""
    aw = _guard()
    for argv in (["rm", "-rf", "/"], ["git", "push", "--force"],
                 ["pip", "install", "requests"]):
        allowed, _ = aw.guard_command(argv)
        assert not allowed, f"{argv} is no longer refused"


# ── Routing pairs: wording must not decide the route ───────────────────────

#: Pairs that mean the same thing. A route that differs between them is a
#: route decided by WORDING rather than by the task.
ROUTING_PAIRS = [
    ("what is 17 * 3?", "Could you tell me: what is 17 * 3?"),
    ("list the files here", "Please list the files in this directory"),
]


@pytest.mark.parametrize("a,b", ROUTING_PAIRS, ids=lambda s: s[:24])
def test_politeness_does_not_change_the_classification(a, b):
    """An audit finding claimed wording drove the route; it was INVALIDATED as
    a confounded pair. These are the unconfounded cases that still hold."""
    from llm_router.classify import GATEWAY_POLICY, classify_signals

    sa, sb = classify_signals(a, GATEWAY_POLICY), classify_signals(b, GATEWAY_POLICY)
    assert sa.task_type == sb.task_type, (
        f"{a!r} -> {sa.task_type}, {b!r} -> {sb.task_type}: wording changed "
        "the task type"
    )


@pytest.mark.xfail(strict=True, reason="FOUND BY THIS CORPUS, 2026-09-22: the "
                                       "classifier keys on a literal 'what is' "
                                       "bigram; inverting to 'what X is' falls "
                                       "through to the ANALYZE default")
def test_word_order_does_not_change_the_classification():
    """A NEW finding, produced by K4 on its first run.

    Finding I-01 ("wording drives the route") was INVALIDATED during the audit
    as a confounded pair. This pair is not confounded — same question, same
    length class, same complexity — and it still routes differently:

        'what is 17 * 3?'                    -> query    (len 15)
        'Could you tell me: what is 17 * 3?' -> query    (len 34)
        'tell me what 17 * 3 is'             -> ANALYZE  (len 22)
        'Could you tell me what 17 * 3 is?'  -> ANALYZE  (len 33)

    So it is neither politeness nor length. The classifier matches a literal
    `what is` bigram; inverting the word order to `what <expr> is` loses that
    signal and the prompt falls through to the "analyze low-signal default"
    that `gateway._classify` documents.

    THE DIRECTION IS WHAT MAKES IT COST MONEY: the fallback is the MORE
    expensive tier, so the failure mode is silently upgrading a trivial
    question, not downgrading a hard one. Nothing reports it, because from
    every surface it looks like an ordinary analyze route.

    Left as xfail(strict) deliberately. Fixing it means changing routing
    behaviour, which needs evaluating on the target distribution rather than
    on this pair — a one-point calibration is a guess, and tuning the
    classifier against a corpus row is exactly what this file forbids. When it
    is fixed, this test XPASSes and fails the suite, which is the point.
    """
    from llm_router.classify import GATEWAY_POLICY, classify_signals

    a = classify_signals("what is 17 * 3?", GATEWAY_POLICY)
    b = classify_signals("tell me what 17 * 3 is", GATEWAY_POLICY)
    assert a.task_type == b.task_type, (
        f"word order changed the route: query-form -> {a.task_type}, "
        f"inverted-form -> {b.task_type}"
    )
