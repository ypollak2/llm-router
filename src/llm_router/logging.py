"""Structured logging helpers for llm_router."""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING

from llm_router.secret_scrubber import structlog_scrubber_processor

if TYPE_CHECKING:  # structlog is imported on first use, see _LazyLogger
    import structlog

_CONFIGURED = False
#: True once ``configure_logging_lazily`` has deferred configuration to the first
#: structlog import. While it is set and structlog is not imported yet, a ``debug`` /
#: ``info`` call that the stdlib logger would drop anyway returns without importing it.
_DEFERRED = False
_STDLIB_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO}


def _discard(*_args, **_kwargs) -> None:
    return None


def _resolve_level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level
    level_name = (level or os.getenv("LLM_ROUTER_LOG_LEVEL", "INFO")).upper()
    return getattr(logging, level_name, logging.INFO)


def configure_logging(*, json_output: bool | None = None, level: str | int | None = None) -> None:
    """Configure stdlib logging + structlog once for the current process."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    import structlog  # deferred: structlog + rich is ~25-45 ms of import

    if json_output is None:
        json_output = os.getenv("LLM_ROUTER_LOG_JSON", "").lower() in {"1", "true", "yes"}

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog_scrubber_processor,  # Scrub secrets before any other processing
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer = (
        structlog.processors.JSONRenderer(sort_keys=True)
        if json_output
        else structlog.dev.ConsoleRenderer()
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    root = logging.getLogger()
    root.setLevel(_resolve_level(level))
    if root.handlers:
        for handler in root.handlers:
            handler.setFormatter(formatter)
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root.addHandler(handler)

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


class _LazyLogger:
    """``structlog.get_logger(name)``, created on first use.

    ``get_logger`` is called at import time by dozens of modules, so importing
    structlog there made every process that touched any of them pay for structlog and
    its rich dependency, even when nothing was ever logged. Every attribute that is not
    set on this object is forwarded to the real logger, which resolves its configuration
    when it first logs, exactly as before. (No ``__slots__``: tests patch ``log.warning``
    on a module's logger.)
    """

    def __init__(self, name: str | None) -> None:
        self._name = name
        self._real = None

    def __getattr__(self, attr: str):
        real = self._real
        if real is None:
            if _DEFERRED and attr in _STDLIB_LEVELS and "structlog" not in sys.modules:
                # Configured (lazily) to route through stdlib logging, whose level
                # decides: below it, structlog would process the event and drop it.
                if not logging.getLogger(self._name).isEnabledFor(_STDLIB_LEVELS[attr]):
                    return _discard
            import structlog

            real = self._real = structlog.get_logger(self._name)
        return getattr(real, attr)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger for *name* (created when first used)."""
    return _LazyLogger(name)  # type: ignore[return-value]


class _ConfigureOnStructlogImport:
    """One-shot ``sys.meta_path`` finder: run ``configure_logging`` right after
    ``structlog`` is imported, by anyone, instead of before.

    A per-prompt hook must keep structlog's default PrintLogger off stdout (audit
    section 2.1) but should not pay for structlog when the prompt never logs. Whoever
    imports structlog first -- ``get_logger`` here, or a module that does
    ``import structlog`` itself -- gets it already configured.
    """

    def __init__(self, kwargs: dict) -> None:
        self._kwargs = kwargs

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "structlog":
            return None
        if self in sys.meta_path:
            sys.meta_path.remove(self)
        import importlib.util

        spec = importlib.util.find_spec("structlog")
        if spec is None or spec.loader is None:
            return None
        real, kwargs = spec.loader, self._kwargs

        class _Loader:
            def create_module(self, s):
                return real.create_module(s)

            def exec_module(self, module):
                real.exec_module(module)
                try:
                    configure_logging(**kwargs)
                except Exception:  # noqa: BLE001 -- logging setup must not abort the hook
                    root = logging.getLogger()
                    if not root.handlers:
                        root.addHandler(logging.StreamHandler(sys.stderr))

        spec.loader = _Loader()
        return spec


def configure_logging_lazily(*, json_output: bool | None = None, level: str | int | None = None) -> None:
    """``configure_logging``, run when structlog is first imported (now, if it already is)."""
    if _CONFIGURED:
        return
    if "structlog" in sys.modules:
        configure_logging(json_output=json_output, level=level)
        return
    # The stdlib half is cheap and takes effect now: a stderr handler at the
    # configured level. configure_logging later only attaches its formatter to it.
    root = logging.getLogger()
    root.setLevel(_resolve_level(level))
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    global _DEFERRED
    if not any(isinstance(f, _ConfigureOnStructlogImport) for f in sys.meta_path):
        sys.meta_path.insert(0, _ConfigureOnStructlogImport({"json_output": json_output, "level": level}))
    _DEFERRED = True
