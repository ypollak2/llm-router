"""Lightweight OpenTelemetry helpers with a zero-dependency no-op fallback."""

from __future__ import annotations

import os
from contextlib import contextmanager
from enum import Enum
from typing import Any, Iterator

_CONFIGURED = False
_TRACE_API = None
_STATUS = None
_STATUS_CODE = None


class _NoopSpan:
    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def record_exception(self, exc: BaseException) -> None:
        return None

    def set_status(self, status: Any) -> None:
        return None

    def is_recording(self) -> bool:
        return False


class _NoopTracer:
    @contextmanager
    def start_as_current_span(self, name: str, **kwargs: Any) -> Iterator[_NoopSpan]:
        yield _NOOP_SPAN


_NOOP_SPAN = _NoopSpan()
_NOOP_TRACER = _NoopTracer()


def configure_tracing() -> None:
    """Configure OTLP export once when an endpoint is present."""
    global _CONFIGURED, _TRACE_API, _STATUS, _STATUS_CODE

    if _CONFIGURED:
        return

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return

    _CONFIGURED = True
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.trace import Status, StatusCode
    except Exception:
        return

    service_name = os.getenv("OTEL_SERVICE_NAME", "llm-router")
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))

    exporter_kwargs: dict[str, Any] = {"endpoint": endpoint}
    insecure = os.getenv("OTEL_EXPORTER_OTLP_INSECURE", "").strip().lower()
    if insecure:
        exporter_kwargs["insecure"] = insecure in {"1", "true", "yes", "on"}
    elif endpoint.startswith("http://"):
        exporter_kwargs["insecure"] = True

    exporter = OTLPSpanExporter(**exporter_kwargs)
    provider.add_span_processor(BatchSpanProcessor(exporter))

    try:
        trace.set_tracer_provider(provider)
    except Exception:
        # The global provider may already be configured by the host process.
        pass

    _TRACE_API = trace
    _STATUS = Status
    _STATUS_CODE = StatusCode


def get_tracer(name: str = "llm_router") -> Any:
    """Return a configured tracer, or a no-op tracer when OTEL is disabled."""
    configure_tracing()
    if _TRACE_API is None:
        return _NOOP_TRACER
    return _TRACE_API.get_tracer(name)


def _normalize_attribute(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, (list, tuple, set, frozenset)):
        normalized = [_normalize_attribute(item) for item in value]
        return [item for item in normalized if item is not None]
    return str(value)


def set_span_attributes(span: Any, **attributes: Any) -> None:
    """Apply only OTEL-safe attributes to a span."""
    for key, value in attributes.items():
        normalized = _normalize_attribute(value)
        if normalized is None:
            continue
        try:
            span.set_attribute(key, normalized)
        except Exception:
            continue


def record_span_exception(span: Any, exc: BaseException) -> None:
    """Record an exception on a span when tracing is active."""
    try:
        span.record_exception(exc)
    except Exception:
        pass

    if _STATUS is not None and _STATUS_CODE is not None:
        try:
            span.set_status(_STATUS(_STATUS_CODE.ERROR, str(exc)))
        except Exception:
            pass


@contextmanager
def traced_span(
    name: str,
    *,
    tracer_name: str = "llm_router",
    **attributes: Any,
) -> Iterator[Any]:
    """Open a span and record any exception before re-raising it."""
    with get_tracer(tracer_name).start_as_current_span(name) as span:
        set_span_attributes(span, **attributes)
        try:
            yield span
        except Exception as exc:
            record_span_exception(span, exc)
            raise
