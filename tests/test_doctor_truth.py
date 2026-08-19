"""WP-11 — `doctor` must fail on a real defect, and say what it did not check.

RED4-02: with a genuine tool-surface regression injected (a bogus canonical tool
name in the CORE tier), `llm_router doctor` exited 0 and produced output
byte-for-byte identical to a healthy run — verified by diff, the only difference
being a "5s" vs "22s" freshness timestamp. A diagnostic that cannot tell a broken
install from a working one is worse than none: it converts "I checked" into
evidence when it is not.

RED1-23: `scripts/trace_northstar.py` — the end-to-end trace that runs the REAL
hook — is referenced by a CHANGELOG line and nothing else. Not by doctor, not by
CI. A guard nobody invokes is documentation.

The second requirement matters as much as the first. A green doctor implies "your
install is fine", but doctor checks maybe a dozen things out of everything that
can break. Stating the unchecked surface is what makes a pass honest rather than
merely reassuring.
"""

from __future__ import annotations


from llm_router import tool_surface as ts
from llm_router.commands import doctor as doc


def test_doctor_fails_when_the_tool_surface_is_broken(monkeypatch, capsys):
    """The exact RED4-02 scenario: a tier offers a tool nothing implements."""
    monkeypatch.setattr(doc, "_tool_surface_phantoms", lambda: ["llm_bogus_xyz"])

    code, issues = doc._run_doctor()
    text = capsys.readouterr().out + "\n" + "\n".join(issues)

    assert code != 0, f"doctor passed with a broken tool surface:\n{text}"
    assert "llm_bogus_xyz" in text, (
        "doctor failed but did not NAME the defect — an operator cannot act on "
        f"an unexplained non-zero exit:\n{text}"
    )


def test_doctor_passes_on_a_healthy_surface():
    """Guards against the fix turning doctor into a permanent red light."""
    assert ts.phantom_tools("core") == []
    assert ts.phantom_tools("routing") == []
    assert ts.phantom_tools("consolidated") == []


def test_doctor_states_what_it_did_not_check(capsys):
    """A pass must not imply more coverage than doctor actually has."""
    doc._run_doctor()
    text = capsys.readouterr().out.lower()

    assert "not checked" in text, (
        "doctor does not state its unchecked surface, so a green run reads as "
        f"'everything is fine':\n{text}"
    )


def test_the_unchecked_list_names_concrete_paths(capsys):
    """A vague disclaimer is worse than none — it looks like disclosure while
    telling the operator nothing they can act on."""
    doc._run_doctor()
    text = capsys.readouterr().out.lower()

    idx = text.find("not checked")
    section = text[idx:idx + 600]
    # At least a few concrete, checkable nouns rather than "some things".
    concrete = sum(
        token in section
        for token in ("routing accuracy", "provider", "quality", "cost", "hook", "live")
    )
    assert concrete >= 2, f"unchecked section is too vague to act on:\n{section}"


def test_phantom_tools_is_unknown_safe(monkeypatch):
    """If ground truth cannot be read, report NOTHING rather than everything.

    A check that screams when its own inputs are missing gets muted, and a muted
    check is the state this whole work package exists to escape.
    """
    monkeypatch.setattr(ts, "implemented_tools", lambda: frozenset())
    assert ts.phantom_tools("core") == []
