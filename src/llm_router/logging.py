"""Structured logging helpers for llm-router."""

from __future__ import annotations

import logging
import os

import structlog

_CONFIGURED = False


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

    if json_output is None:
        json_output = os.getenv("LLM_ROUTER_LOG_JSON", "").lower() in {"1", "true", "yes"}

    shared_processors = [
        structlog.contextvars.merge_contextvars,
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


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger for *name*."""
    return structlog.get_logger(name)
