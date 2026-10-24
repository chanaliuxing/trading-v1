"""OpenTelemetry tracing setup helpers."""
from __future__ import annotations

from typing import Optional

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
except Exception:  # pragma: no cover - optional dependency
    trace = None  # type: ignore


def setup_tracing(service_name: str = "trader", endpoint: Optional[str] = None):
    """Initialise OTLP/HTTP exporter if OpenTelemetry is available."""
    if trace is None:
        return None
    import os

    endpoint = endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4318")
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": os.getenv("OTEL_SERVICE_NAMESPACE", "quant"),
            "service.version": os.getenv("BUILD_VERSION", "dev"),
            "deployment.environment": os.getenv("ENV", "local"),
        }
    )
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces", timeout=5)
    processor = BatchSpanProcessor(exporter)
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    try:  # Optional instrumentation
        from opentelemetry.instrumentation.asyncio import AsyncioInstrumentor

        AsyncioInstrumentor().instrument()
    except Exception:  # pragma: no cover
        pass

    return trace.get_tracer(service_name)
