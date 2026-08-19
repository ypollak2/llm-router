"""CHZ-SEC-06 must hold on EVERY reader of the Ollama URL, not just config.py.

`config.validate_ollama_url` exists because these env vars were an SSRF sink —
its own docstring records that `file://` was accepted (local file read) and
cloud-metadata addresses were attempted. That fix landed in config.py, and two
hook modules kept their own unvalidated copies of the same reader, so whichever
path ran first decided whether the protection applied:

    input                                validator   hook reader (before)
    file:///etc/passwd                   BLOCKED     allowed
    http://169.254.169.254/latest/...    BLOCKED     allowed

It needs no local access. `_load_dotenv` in auto-route.py reads
`Path.cwd()/".env"`, so a cloned repository can set the variable.

This is the same shape as the code_context/capabilities finding in doc 31: one
path hardened, its siblings left behind. The test is written against ALL readers
rather than the two that were wrong, so a third copy fails here on the day it
is added.
"""

from __future__ import annotations

import pytest

from llm_router.config import validate_ollama_url
from llm_router.hooks.agent_loop import _get_ollama_url as agent_loop_url
from llm_router.hooks.direct_executor import _get_ollama_url as direct_executor_url

_LOCALHOST = "http://localhost:11434"

#: Every function that turns the environment into a URL handed to urlopen.
#: Add new readers here; that is the point of the parametrisation.
_READERS = {
    "agent_loop": agent_loop_url,
    "direct_executor": direct_executor_url,
}

_HOSTILE = [
    ("file:///etc/passwd", "local file read"),
    ("file://localhost/etc/shadow", "local file read via host form"),
    ("http://169.254.169.254/latest/meta-data/", "cloud metadata SSRF"),
    ("http://0.0.0.0:11434", "unspecified address"),
    ("gopher://localhost:11434", "non-http scheme"),
]


@pytest.mark.parametrize("reader_name", sorted(_READERS))
@pytest.mark.parametrize("hostile,label", _HOSTILE)
def test_hostile_urls_fail_closed(monkeypatch, reader_name, hostile, label):
    """A rejected URL must fall back to localhost, never pass through."""
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", hostile)
    got = _READERS[reader_name]()
    assert got != hostile, f"{reader_name} passed through a {label} URL: {got}"
    assert got == _LOCALHOST


@pytest.mark.parametrize("reader_name", sorted(_READERS))
def test_a_legitimate_remote_ollama_still_works(monkeypatch, reader_name):
    """Failing closed must not mean refusing every non-localhost host.

    A remote Ollama is a supported configuration; the validator allows arbitrary
    http(s) hosts by design and only rejects schemes and metadata/link-local
    addresses. Without this, "fail closed" could be implemented as "always
    localhost" and every hostile-input test above would still pass.
    """
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", "http://ollama.internal:11434")
    assert _READERS[reader_name]() == "http://ollama.internal:11434"


@pytest.mark.parametrize("reader_name", sorted(_READERS))
def test_unset_environment_is_localhost(monkeypatch, reader_name):
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_URL", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    assert _READERS[reader_name]() == _LOCALHOST


@pytest.mark.parametrize("hostile,label", _HOSTILE)
def test_the_canonical_validator_agrees(hostile, label):
    """Guards the guard: if config's validator stops rejecting these, the tests
    above would pass by inheriting a permissive validator rather than by the
    readers being safe."""
    assert validate_ollama_url(hostile) == "", (
        f"config.validate_ollama_url no longer blocks a {label} URL — the "
        "parity tests above are now vacuous"
    )
