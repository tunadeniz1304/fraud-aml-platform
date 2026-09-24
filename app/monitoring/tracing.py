"""OpenTelemetry tracing shim (P0.9) — optional exporter, otherwise a no-op.

Tracing turns on only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set **and** the
``otel`` extras are installed (``pip install -e ".[otel]"``). Without them
:func:`span` returns a null context manager, so instrumented code costs
nothing and the platform runs without any tracing backend.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger("fraud.tracing")

_tracer: Any = None


def setup_tracing(service_name: str = "anil3-api") -> bool:
    """Configure an OTLP exporter if requested; return whether tracing is on."""
    global _tracer
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        _tracer = None
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("[Tracing] OTEL uç noktası tanımlı ama 'otel' ekleri kurulu değil — no-op")
        _tracer = None
        return False
    name = os.environ.get("OTEL_SERVICE_NAME", service_name)
    provider = TracerProvider(resource=Resource.create({"service.name": name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("anil3")
    logger.info("[Tracing] OpenTelemetry aktif → %s (%s)", endpoint, name)
    return True


def enabled() -> bool:
    return _tracer is not None


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Start a span when tracing is on; otherwise do nothing."""
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(
                    key, value if isinstance(value, int | float | bool) else str(value)
                )
        yield current


def reset() -> None:
    """Disable tracing (tests)."""
    global _tracer
    _tracer = None
