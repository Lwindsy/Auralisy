"""Content-free OTLP instrumentation for executions and paid provider calls."""

import os
from contextlib import contextmanager
from time import perf_counter

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

propagator = TraceContextTextMapPropagator()
tracer = trace.get_tracer("auralisy.intelligence")
meter = metrics.get_meter("auralisy.intelligence")
call_duration = meter.create_histogram("auralisy.provider.duration", unit="s")
tokens = meter.create_counter("auralisy.provider.tokens", unit="{token}")
executions = meter.create_counter("auralisy.executions", unit="{execution}")
execution_duration = meter.create_histogram("auralisy.execution.duration", unit="s")
round_cost = meter.create_histogram("auralisy.round.settled.snapshot", unit="USD")


def configure():
    if os.environ.get("AURALIS_TELEMETRY_ENABLED", "false").lower() != "true":
        return lambda: None
    resource = Resource.create({"service.name": "auralisy-python"})
    sample_rate = float(os.environ.get("AURALIS_TRACE_SAMPLE_RATE", "0.0"))
    traces = TracerProvider(resource=resource, sampler=ParentBased(TraceIdRatioBased(sample_rate)))
    traces.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(traces)
    meters = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=10000)
        ],
    )
    metrics.set_meter_provider(meters)

    def shutdown():
        meters.shutdown()
        traces.shutdown()

    return shutdown


@contextmanager
def execution_span(execution):
    started = perf_counter()
    with tracer.start_as_current_span(
        "recommendation.execution",
        record_exception=False,
        set_status_on_exception=False,
        attributes={
            "auralisy.round.id": execution.start["round_id"],
            "auralisy.execution.id": execution.id,
        },
    ) as span:
        try:
            yield span
        finally:
            outcome = (
                "FAILED" if execution.error_code else execution.first_outcome or execution.status
            )
            attributes = {"outcome": outcome}
            elapsed = perf_counter() - started
            executions.add(1, attributes)
            execution_duration.record(elapsed, attributes)
            span.set_attribute("auralisy.outcome", outcome)
            span.set_attribute("auralisy.duration.seconds", elapsed)
            span.set_attribute("auralisy.tokens.input", execution.telemetry_input_tokens)
            span.set_attribute("auralisy.tokens.output", execution.telemetry_output_tokens)
            span.set_attribute("auralisy.usage.unknown_calls", len(execution.uncertain))
            if execution.error_code:
                span.set_attribute("error.type", execution.error_code)
                span.set_status(trace.StatusCode.ERROR)
            if execution.lanes is not None:
                settled = sum(v["settled_microusd"] for v in execution.lanes.values()) / 1e6
                reserved = sum(v["reserved_microusd"] for v in execution.lanes.values()) / 1e6
                span.set_attribute("auralisy.round.settled.usd", settled)
                span.set_attribute("auralisy.round.reserved.usd", reserved)
                round_cost.record(settled)


@contextmanager
def provider_span(execution, plan, resource):
    started = perf_counter()
    attributes = {"kind": resource, "purpose": plan["purpose"], "lane": plan["lane"]}
    with tracer.start_as_current_span(
        "provider." + resource,
        kind=trace.SpanKind.CLIENT,
        attributes={**attributes, "auralisy.invocation.id": plan["invocation_id"]},
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as error:
            span.set_status(trace.StatusCode.ERROR)
            span.set_attribute("error.type", type(error).__name__)
            raise
        finally:
            call_duration.record(perf_counter() - started, attributes)


def record_usage(execution, span, reply):
    span.set_attribute("auralisy.outcome", reply.outcome)
    span.set_attribute("auralisy.finish", reply.finish)
    if reply.finish != "COMPLETE":
        span.set_status(trace.StatusCode.ERROR)
    for direction in ("input", "output"):
        value = getattr(reply.usage, direction + "_tokens")
        if value is not None:
            tokens.add(value, {"direction": direction})
            span.set_attribute("gen_ai.usage." + direction + "_tokens", value)
            field = "telemetry_" + direction + "_tokens"
            setattr(execution, field, getattr(execution, field) + value)
    if reply.usage.cached_input_tokens is not None:
        span.set_attribute("gen_ai.usage.cache_read.input_tokens", reply.usage.cached_input_tokens)
