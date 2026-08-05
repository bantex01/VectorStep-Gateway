"""OpenTelemetry tracing for the VectorStep Gateway.

Disabled by default — the OTel SDK's no-op TracerProvider means every
tracer.start_as_current_span() call is free until setup_tracing() installs
a real exporter via the observability.otel block in config.yaml.

The key entry point for unified VectorStep → Gateway traces is extract_remote_context():
VectorStep injects a W3C traceparent into the agent WebSocket request params, and
the Gateway uses it to make agent.run a child of VectorStep's gen_ai.gateway span.
"""
from __future__ import annotations

import logging

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

logger = logging.getLogger(__name__)

tracer = trace.get_tracer("vectorstep-gateway")

_DEFAULT_SERVICE_NAME = "vectorstep-gateway"
_DEFAULT_OTLP_ENDPOINT = "http://localhost:4318/v1/traces"


def setup_tracing(config) -> None:
    """Install a real TracerProvider if observability.otel.enabled is true."""
    otel_cfg = getattr(getattr(config, "observability", None), "otel", None)
    if otel_cfg is None or not otel_cfg.enabled:
        return

    service_name = otel_cfg.service_name or _DEFAULT_SERVICE_NAME
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    if otel_cfg.exporter == "console":
        exporter = ConsoleSpanExporter()
    else:
        endpoint = otel_cfg.endpoint or _DEFAULT_OTLP_ENDPOINT
        exporter = OTLPSpanExporter(endpoint=endpoint)

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    logger.info(
        "OpenTelemetry tracing enabled: service=%s exporter=%s",
        service_name, otel_cfg.exporter,
    )


def shutdown_tracing() -> None:
    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if callable(shutdown):
        shutdown()


def extract_remote_context(params: dict) -> otel_context.Context:
    """Extract W3C trace context injected by VectorStep into the WebSocket request params.

    VectorStep calls propagate.inject(params) before sending the agent request, which
    sets params["traceparent"] (and optionally params["tracestate"]). This function
    reverses that, returning an OTel Context that makes the gateway's agent.run span
    a child of VectorStep's gen_ai.gateway span.

    Returns an empty Context (new trace root) if no traceparent is present.
    """
    return propagate.extract(params)
