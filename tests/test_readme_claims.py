"""The README's claims must be checkable, and checked.

The first screen is where a stranger decides whether to install. It previously
led with "save 35-80%", which is the same sentence every competitor in this
category prints — and it is the claim llm-router is worst placed to win, being
one of the smallest projects in a field including a 37k-star proxy.

The wedge is the thing a proxy structurally cannot do: intercept a session
authenticated by a subscription, where there is no API key to forward. These
tests hold the README to claims that are true and that stay true.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _readme() -> str:
    return (REPO / "README.md").read_text()


def test_the_first_screen_leads_with_the_wedge():
    """Above the fold, before any badge or install line."""
    first_screen = _readme()[:1200].lower()
    assert "subscription" in first_screen or "pro or max" in first_screen, (
        "the opening does not mention the subscription seat, which is the only "
        "buyer a proxy cannot serve"
    )


def test_the_zero_key_claim_is_present_and_true():
    """`No API keys` is the differentiator; it must also be accurate.

    Accuracy is asserted elsewhere by the config tests — here we only require
    that the README says it, since it was previously buried below the fold.
    """
    readme = _readme()
    assert re.search(r"no api keys?", readme, re.I), "the zero-key claim is absent"


def test_routerarena_write_up_is_linked_not_just_badged():
    """A badge asserts a rank. The write-up states what was measured."""
    readme = _readme()
    assert "docs/ROUTERARENA.md" in readme, (
        "the benchmark is claimed by badge only; link the write-up that says "
        "what was measured and what did not work"
    )
    assert (REPO / "docs" / "ROUTERARENA.md").is_file()


def test_no_unearned_hook_claims():
    """The host table must not claim auto-routing on a host we have not shipped.

    llm_router.hosts.events.routing_ready() is the source of truth, and it
    currently returns claude-code only.
    """
    import sys

    sys.path.insert(0, str(REPO / "src"))
    from llm_router.hosts.events import HOSTS, routing_ready

    ready = {name for name in HOSTS if routing_ready(name)}

    readme = _readme()
    table = readme.split("## Works With")[1].split("```")[0]

    for line in table.splitlines():
        if not line.strip().startswith("| **"):
            continue
        if "Full auto-routing" not in line:
            continue
        host_label = line.split("**")[1].strip().lower()
        # Gemini CLI ships its own hook set outside the events map's ready check;
        # everything else claiming full auto-routing must be routing_ready.
        if "gemini" in host_label:
            continue
        matched = any(
            h.replace("-", " ") in host_label or host_label in h.replace("-", " ")
            for h in ready
        )
        assert matched, (
            f"README claims full auto-routing for {host_label!r}, but "
            f"routing_ready() reports only {sorted(ready)}"
        )


def test_the_naming_note_appears_once():
    """`pip install llm-routing` giving you `llm-router` needs saying once.

    It was said twice, both times apologetically. ADR 0001 keeps the CLI
    hyphenated deliberately — a distribution name differing from its command is
    ordinary (ripgrep/rg), not a defect to apologise for.
    """
    readme = _readme()
    assert "> Package name:" not in readme, "the apology block is back"
    mentions = len(re.findall(r"installs the `llm-router` command", readme))
    assert mentions == 1, f"expected exactly one naming note, found {mentions}"


def test_adr_records_the_name_decision():
    adr = REPO / "docs" / "decisions" / "0001-package-name.md"
    assert adr.is_file(), "the name decision is not recorded"
    text = adr.read_text()
    assert "llm-routing" in text
    assert "npm" in text, "the ADR does not explain the npm constraint that forced it"


def test_pypi_name_matches_the_adr():
    """One name, and the packaging metadata agrees with the decision."""
    import tomllib

    data = tomllib.load((REPO / "pyproject.toml").open("rb"))
    assert data["project"]["name"] == "llm-routing"


def test_plugin_manifests_describe_the_same_product_as_the_readme():
    """A marketplace description that predates the repositioning is a third
    story a reader can find."""
    for meta in (".claude-plugin", ".codex-plugin"):
        manifest = json.loads((REPO / meta / "plugin.json").read_text())
        blob = json.dumps(manifest).lower()
        assert "rout" in blob, f"{meta} does not describe routing at all"
