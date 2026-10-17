"""Routing authority: does the proxy ROUTE (rewrite the model, serve locally) or only ADVISE?

``route`` is what the proxy does today. ``advise`` makes it forward the model the
client asked for, byte for byte, and only log what it would have proposed
(``tier_proposed``). The mode is read on every request, never at process start:
the proxy is a launchd job, and a toggle that needed a restart would be an owner
gate (PLAN-v16 s.4.5, L21). ``llm-router mode advise|route`` therefore only writes
one small file.

Resolution, highest first:

1. env ``LLM_ROUTER_AUTHORITY`` (read at call time; empty counts as unset);
2. the state file ``<state>/authority`` holding one word;
3. ``route``.

An unrecognised value anywhere is treated as ``route`` (today's behaviour) and
recorded once per distinct value as ``CHZ-FO-AUTHORITY-MODE``. Stdlib only. This
module never writes ``settings.json`` or a service definition.
"""

from __future__ import annotations

import os

from llm_router import failopen, paths

ROUTE = "route"
ADVISE = "advise"
MODES = (ROUTE, ADVISE)
ENV_VAR = "LLM_ROUTER_AUTHORITY"
FILE_NAME = "authority"
DEFAULT = ROUTE

_FAILOPEN_CODE = "CHZ-FO-AUTHORITY-MODE"

# (path, mtime_ns, size) -> text. The proxy asks once per request; a stat is
# cheap, a read only happens after the file changed.
_file_cache: tuple[tuple[str, int, int], str | None] | None = None
_reported: set[str] = set()


def state_file():
    return paths.state_path(FILE_NAME)


def _report_invalid(where: str, value: str) -> None:
    key = f"{where}:{value[:40]}"
    if key in _reported:
        return
    _reported.add(key)
    failopen.record(_FAILOPEN_CODE, detail=f"{where} holds an unrecognised value; using route")


def _read_file() -> str | None:
    global _file_cache
    path = state_file()
    try:
        st = path.stat()
    except OSError:
        _file_cache = None
        return None
    sig = (str(path), st.st_mtime_ns, st.st_size)
    if _file_cache is not None and _file_cache[0] == sig:
        return _file_cache[1]
    try:
        text: str | None = path.read_text(encoding="utf-8").strip().lower()
    except (OSError, ValueError):
        text = None
    _file_cache = (sig, text)
    return text


def describe() -> tuple[str, str]:
    """``(mode, source)``; source is ``env``, ``file`` or ``default``."""
    raw = os.environ.get("LLM_ROUTER_AUTHORITY", "").strip().lower()
    if raw:
        if raw in MODES:
            return raw, "env"
        _report_invalid("env", raw)
        return DEFAULT, "default"
    text = _read_file()
    if text:
        if text in MODES:
            return text, "file"
        _report_invalid("file", text)
    return DEFAULT, "default"


def mode() -> str:
    """The current authority mode: ``route`` or ``advise``. Never raises."""
    try:
        return describe()[0]
    except Exception as exc:  # noqa: BLE001 - the proxy must never fail on this read
        failopen.record(_FAILOPEN_CODE, exc)
        return DEFAULT


def set_mode(value: str) -> str:
    """Write the state file atomically (0600) and return the mode written.
    Raises ``ValueError`` for anything but ``route`` / ``advise``."""
    global _file_cache
    value = (value or "").strip().lower()
    if value not in MODES:
        raise ValueError(f"authority mode must be one of {', '.join(MODES)}, not {value!r}")
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{FILE_NAME}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (value + "\n").encode("ascii"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _file_cache = None
    return value
