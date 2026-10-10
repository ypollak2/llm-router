"""Which API keys exist, where they came from, and who can use them (P1.8 task 2).

``discover()`` returns one :class:`KeyInfo` per key variable that is set. It
records the variable NAME, the provider, the source (``env``, ``dotenv:<path>``
or ``keychain``), the tools that can use it and the billing kind. A key's value
is never returned, stored or logged: dotenv files are scanned for ``NAME=`` with
a non-empty value and the value is dropped on the same line; the keychain probes
look at the exit code only and discard their output.

Source rule: the process environment already contains the values that
``env_loader.load_dotenv_files`` merged in (the real environment wins), so a
variable whose dotenv value equals the process value is labelled with that
file, and one whose value differs is ``env``. A real-env export that happens to
equal a dotenv value is therefore labelled ``dotenv:`` (first file in priority
order); that ambiguity cannot be removed without a pre-merge snapshot.
Keychain is consulted for variables that are in neither.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

KEYCHAIN_SERVICE_PREFIX = "llm-router-"

# var -> (provider, usable_by, billing). billing is "paid" only where the API has
# no free tier; where a free tier exists it is "unknown": which one the key
# belongs to needs a billing call this module never makes.
KEY_TABLE: dict[str, tuple[str, tuple[str, ...], str]] = {
    "ANTHROPIC_API_KEY": ("anthropic", ("claude-code", "llm-router"), "paid"),
    "OPENAI_API_KEY": ("openai", ("codex", "llm-router"), "paid"),
    "GEMINI_API_KEY": ("gemini", ("gemini-cli", "llm-router"), "unknown"),
    "GOOGLE_API_KEY": ("gemini", ("gemini-cli", "llm-router"), "unknown"),
    "PERPLEXITY_API_KEY": ("perplexity", ("llm-router",), "paid"),
    "PERPLEXITYAI_API_KEY": ("perplexity", ("llm-router",), "paid"),
    "GROQ_API_KEY": ("groq", ("llm-router",), "unknown"),
    "DEEPSEEK_API_KEY": ("deepseek", ("llm-router",), "paid"),
    "MOONSHOT_API_KEY": ("moonshot", ("llm-router",), "paid"),
    "OPENROUTER_API_KEY": ("openrouter", ("llm-router",), "unknown"),
}

Probe = Callable[[str], bool]


@dataclass(frozen=True)
class KeyInfo:
    var: str
    provider: str
    source: str            # "env" | "dotenv:<path>" | "keychain"
    usable_by: tuple[str, ...]
    billing: str           # "paid" | "free_tier" | "unknown"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["usable_by"] = list(self.usable_by)
        return d


def _dotenv_values(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, val = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        val = val.strip().strip("'\"")
        if name in KEY_TABLE and val:
            out[name] = val
    return out


def keychain_has(var: str, *, platform: str | None = None, run=subprocess.run) -> bool:
    """True when the OS keychain holds ``llm-router-<var>``. Existence only."""
    from llm_router.claude_creds import keychain_query

    return keychain_query(KEYCHAIN_SERVICE_PREFIX + var, want_secret=False,
                          platform=platform, run=run)[0]


def discover(*, env: dict | None = None, dotenv_paths: list[Path] | None = None,
             keychain: Probe | None = None) -> list[KeyInfo]:
    env = dict(os.environ) if env is None else env
    if dotenv_paths is None:
        from llm_router import env_loader
        dotenv_paths = env_loader.candidate_env_paths()
    probe = keychain if keychain is not None else keychain_has
    files = [(p, _dotenv_values(p)) for p in dotenv_paths]
    found: list[KeyInfo] = []
    for var, (provider, usable_by, billing) in KEY_TABLE.items():
        source = None
        if (env.get(var) or "").strip():
            source = "env"
            for p, vals in files:
                if vals.get(var) == env[var].strip():
                    source = f"dotenv:{p}"
                    break
        else:
            for p, vals in files:
                if var in vals:
                    source = f"dotenv:{p}"
                    break
        if source is None and probe(var):
            source = "keychain"
        if source is not None:
            found.append(KeyInfo(var, provider, source, usable_by, billing))
    return found
