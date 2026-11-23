"""Regression for queued requests retaining their own trace under contention."""

import asyncio
import os
import subprocess
import sys

import httpx
import pytest
from auralisy_intelligence import telemetry
from auralisy_intelligence.agents.resources import ServiceResources
from auralisy_intelligence.providers import ModelGateway, OpenAIResponsesAdapter
from auralisy_intelligence.providers.deepseek import DeepSeekResponsesAdapter
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_deepseek import BINDING as DEEPSEEK_BINDING
from test_providers import BINDING, REQUEST, response, web_execution


@pytest.mark.parametrize("value", [None, "false"])
def test_disabled_never_imports_exporters(value):
    env = dict(os.environ)
    if value is None:
        env.pop("AURALIS_TELEMETRY_ENABLED", None)
    else:
        env["AURALIS_TELEMETRY_ENABLED"] = value
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from auralisy_intelligence import telemetry
shutdown = telemetry.configure()
with telemetry.tracer.start_as_current_span('disabled') as span:
    assert not span.is_recording()
shutdown()
assert not any(name.startswith('opentelemetry.exporter.otlp') for name in sys.modules)
""",
        ],
        env=env,
        check=True,
        timeout=15,
    )


@pytest.mark.parametrize("provider_name", ["openai", "deepseek"])
def test_model_gateway_traces_real_adapters(monkeypatch, provider_name):
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "tracer", tracer_provider.get_tracer("test"))
    requests = []

    def handle(request):
        requests.append(request.url.host)
        return httpx.Response(200, json=response())

    async def run():
        transport = httpx.MockTransport(handle)
        if provider_name == "deepseek":
            adapter = DeepSeekResponsesAdapter("synthetic", transport=transport)
            binding = DEEPSEEK_BINDING
        else:
            adapter = OpenAIResponsesAdapter("synthetic", transport=transport, test_transport=True)
            binding = BINDING
        execution, _ = web_execution(binding)
        async with adapter:
            result = await ModelGateway(adapter, {binding.ref: binding}).generate(
                execution,
                role="screen",
                purpose="SCREEN",
                system=REQUEST.system,
                input=REQUEST.input,
                output_schema=REQUEST.output_schema,
                prompt_version="screen.v1",
            )
        await execution.cleanup()
        return result

    try:
        result = asyncio.run(run())
        assert result.finish == "COMPLETE"
        assert requests == [f"api.{provider_name}.com"]
        (span,) = exporter.get_finished_spans()
        assert span.name == "provider.model"
        assert span.attributes["auralisy.invocation.id"] == result.invocation_id
        assert span.attributes["gen_ai.usage.input_tokens"] == 4
        assert "synthetic" not in str(span.attributes)
    finally:
        tracer_provider.shutdown()


def test_queued_calls_keep_request_trace(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(telemetry, "tracer", tracer)

    async def scenario():
        service = ServiceResources(model=1, background=1)
        first, second = service.scope("first", "first"), service.scope("second", "second")
        gate = asyncio.Event()
        expected = {}
        observed = {}

        async def caller(scope, wait):
            with tracer.start_as_current_span(scope.session) as parent:
                expected[scope.session] = parent.get_span_context().trace_id

                async def work():
                    with tracer.start_as_current_span("provider.model"):
                        observed[scope.session] = (
                            trace.get_current_span().get_span_context().trace_id
                        )
                        if wait:
                            await gate.wait()

                await scope.run("model", work)

        a = asyncio.create_task(caller(first, True))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        b = asyncio.create_task(caller(second, False))
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(a, b)
        await service.close()
        assert expected == observed
        assert len(set(observed.values())) == 2

    asyncio.run(scenario())
    assert len(exporter.get_finished_spans()) == 4
    provider.shutdown()


def test_w3c_parent_is_preserved(monkeypatch):
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(telemetry, "tracer", tracer)
    with tracer.start_as_current_span("java.dispatch") as parent:
        headers = {}
        telemetry.propagator.inject(headers)
    with tracer.start_as_current_span(
        "python.accept", context=telemetry.propagator.extract(headers)
    ) as child:
        assert child.get_span_context().trace_id == parent.get_span_context().trace_id
        assert child.parent.span_id == parent.get_span_context().span_id
    provider.shutdown()
