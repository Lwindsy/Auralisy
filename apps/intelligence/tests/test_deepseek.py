"""DeepSeek protocol, paid admission and configuration using synthetic transports."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from auralisy_intelligence.agents import CallDenied, Usage
from auralisy_intelligence.album_reviews.cold import ColdEvidence
from auralisy_intelligence.providers import ModelGateway, ToolGateway
from auralisy_intelligence.providers.config import (
    load_role_bindings,
    model_provider,
    validate_provider_bindings,
)
from auralisy_intelligence.providers.deepseek import DeepSeekResponsesAdapter, DeepSeekSearchAdapter
from auralisy_intelligence.providers.java_config import java_config
from auralisy_intelligence.providers.types import SearchHit
from test_providers import BINDING as SYNTHETIC_BINDING
from test_providers import REQUEST, response

EXAMPLES = Path(__file__).resolve().parents[1]
BINDING = replace(
    SYNTHETIC_BINDING,
    model="deepseek-flash",
    live_enabled=True,
    price_source="fixture",
    input_microusd_per_million=300_000,
    cached_input_microusd_per_million=6_000,
    output_microusd_per_million=1_200_000,
    search_microusd_per_call=750,
    reasoning_effort="none",
)


def adapter_for(value, adapter_type=DeepSeekResponsesAdapter, **kwargs):
    requests = []

    async def handler(request):
        requests.append(request)
        if isinstance(value, type) and issubclass(value, httpx.HTTPError):
            raise value("fixture", request=request)
        return value

    adapter = adapter_type("synthetic", transport=httpx.MockTransport(handler), **kwargs)
    return adapter, requests


@pytest.mark.parametrize("header", [None, "header-id"])
def test_responses_request_and_usage(header):
    async def run():
        raw = response()
        raw["output"].insert(
            0, {"type": "reasoning", "content": [{"type": "output_text", "text": "ignored"}]}
        )
        adapter, requests = adapter_for(
            httpx.Response(200, json=raw, headers={"x-request-id": header} if header else {})
        )
        async with adapter:
            result = await adapter.complete(BINDING, REQUEST)
        assert len(requests) == 1
        (sent,) = requests
        assert sent.method == "POST" and str(sent.url) == "https://api.deepseek.com/responses"
        assert json.loads(sent.content) == {
            "model": "deepseek-flash",
            "stream": False,
            "input": [
                {"role": "system", "content": REQUEST.system},
                {"role": "user", "content": json.dumps(REQUEST.input, ensure_ascii=False)},
            ],
            "max_output_tokens": 100,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": REQUEST.schema_name,
                    "schema": REQUEST.output_schema,
                }
            },
            "reasoning": {"effort": "none"},
        }
        assert result.output == {"score": 4} and result.finish == "COMPLETE"
        assert result.usage == Usage(4, 6, 2)
        assert result.provider_request_ref == (header or "response-1")

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,reason,value,outcome,finish,category",
    [
        ("incomplete", "content_filter", "", "COMPLETED", "REFUSED", "MODEL_FAILED"),
        ("incomplete", "max_output_tokens", "", "COMPLETED", "TRUNCATED", "MODEL_FAILED"),
        ("failed", None, "", "COMPLETED", "ERROR", "MODEL_FAILED"),
        ("in_progress", None, "", "UNCERTAIN", "ERROR", "MODEL_FAILED"),
        (None, None, "", "UNCERTAIN", "ERROR", "MODEL_FAILED"),
        ("completed", None, "not-json", "COMPLETED", "ERROR", "JSON_PARSE_FAILED"),
        ("completed", None, "  ", "COMPLETED", "ERROR", "JSON_PARSE_FAILED"),
        ("completed", None, '{"score": NaN}', "COMPLETED", "ERROR", "JSON_PARSE_FAILED"),
        ("completed", None, "{}", "COMPLETED", "ERROR", "SCHEMA_VALIDATION_FAILED"),
    ],
)
def test_response_status_and_structured_failures(status, reason, value, outcome, finish, category):
    async def run():
        raw = response(text=value)
        if status is None:
            del raw["status"]
        else:
            raw["status"] = status
        raw["incomplete_details"] = {"reason": reason}
        adapter, requests = adapter_for(httpx.Response(200, json=raw))
        async with adapter:
            result = await adapter.complete(BINDING, REQUEST)
        assert len(requests) == 1
        assert (result.outcome, result.finish, result.diagnostic["category"]) == (
            outcome,
            finish,
            category,
        )
        assert result.usage == Usage(4, 6, 2)

    asyncio.run(run())


@pytest.mark.parametrize(
    "value,outcome",
    [
        (httpx.Response(402), "NOT_PROCESSED"),
        (httpx.Response(429), "NOT_PROCESSED"),
        (httpx.Response(503), "UNCERTAIN"),
        (httpx.Response(200, content=b"x" * 513), "UNCERTAIN"),
        (httpx.Response(200, content=b"not json"), "UNCERTAIN"),
        (httpx.ConnectError, "NOT_PROCESSED"),
        (httpx.ReadTimeout, "UNCERTAIN"),
    ],
)
def test_model_transport_outcomes(value, outcome):
    async def run():
        adapter, requests = adapter_for(value, max_response_bytes=512)
        async with adapter:
            result = await adapter.complete(BINDING, REQUEST)
        assert len(requests) == 1 and result.outcome == outcome and result.finish == "ERROR"

    asyncio.run(run())


@pytest.mark.parametrize(
    "results",
    [
        [],
        [
            {"url": "https://one.example", "title": "One", "snippet": "Lead"},
            {"url": "https://one.example", "title": "Duplicate"},
            {"url": "https://two.example", "title": "Two"},
        ],
    ],
)
def test_search_body_hits_and_zero_token_usage(results):
    async def run():
        adapter, requests = adapter_for(
            httpx.Response(200, json={"results": results}), DeepSeekSearchAdapter
        )
        async with adapter:
            result = await adapter.search(BINDING, "歌" * 128, ())
        (sent,) = requests
        assert sent.method == "POST" and str(sent.url) == "https://api.deepseek.com/v1/tools/search"
        assert json.loads(sent.content) == {"query": "歌" * 128, "count": 10}
        assert (result.outcome, result.finish, result.usage) == (
            "COMPLETED",
            "COMPLETE",
            Usage(0, 0, 0),
        )
        expected = (
            SearchHit("https://one.example", "One", "Lead"),
            SearchHit("https://two.example", "Two"),
        )
        assert result.output == (expected if results else ())

    asyncio.run(run())


@pytest.mark.parametrize(
    "value,outcome",
    [(httpx.Response(code), "NOT_PROCESSED") for code in (400, 401, 402, 408, 413, 429, 500, 504)]
    + [
        (httpx.ConnectError, "NOT_PROCESSED"),
        (httpx.ConnectTimeout, "NOT_PROCESSED"),
        (httpx.PoolTimeout, "NOT_PROCESSED"),
        (httpx.ReadTimeout, "UNCERTAIN"),
        (httpx.Response(200, content=b"invalid"), "UNCERTAIN"),
        (httpx.Response(200, json=[]), "UNCERTAIN"),
        (httpx.Response(200, json={}), "UNCERTAIN"),
    ],
)
def test_search_transport_outcomes(value, outcome):
    async def run():
        adapter, requests = adapter_for(value, DeepSeekSearchAdapter)
        async with adapter:
            result = await adapter.search(BINDING, "Artist Album", ())
        assert len(requests) == 1 and result.outcome == outcome and result.finish == "ERROR"

    asyncio.run(run())


def execution_for(binding):
    execution = Mock()
    execution.start = {
        "capabilities": ["web_search.v1"],
        "model_bindings": {"screen": binding.wire()},
        "agent_policy": {"timing": {"research_timeout_fg_ms": 1000}},
    }
    return execution


@pytest.mark.parametrize(
    "change",
    [
        {"live_enabled": False},
        {"price_source": None},
        {"output_microusd_per_million": None},
        {"cached_input_microusd_per_million": 300_001},
        {"input_microusd_per_million": -1},
        {"search_microusd_per_call": None},
        {"web_search": False},
        {"structured_outputs": False},
    ],
)
def test_live_admission_direct_and_gateway(change):
    async def run():
        binding = replace(BINDING, **change)
        execution = execution_for(binding)
        model, model_requests = adapter_for(httpx.Response(200, json=response()))
        search, search_requests = adapter_for(
            httpx.Response(200, json={"results": []}), DeepSeekSearchAdapter
        )
        gateway = ModelGateway(model, {binding.ref: binding})
        async with model, search:
            if not (change.keys() & {"search_microusd_per_call", "web_search"}):
                with pytest.raises(CallDenied):
                    await model.complete(binding, REQUEST)
                with pytest.raises(CallDenied):
                    await gateway.generate(
                        execution,
                        role="screen",
                        purpose="SCREEN",
                        system=REQUEST.system,
                        input=REQUEST.input,
                        output_schema=REQUEST.output_schema,
                        prompt_version="screen.v1",
                    )
            if "structured_outputs" not in change:
                with pytest.raises(CallDenied):
                    await search.search(binding, "Artist Album", ())
                with pytest.raises(CallDenied):
                    await ToolGateway(search_adapter=search, model_gateway=gateway).search(
                        execution,
                        candidate_id="album",
                        query="Artist Album",
                    )
        assert not model_requests and not search_requests
        execution.invocation_plan.assert_not_called()
        execution.invoke.assert_not_called()

    asyncio.run(run())


def test_query_validation_precedes_plan_and_cold_search():
    async def run():
        execution = execution_for(BINDING)
        search, requests = adapter_for(
            httpx.Response(200, json={"results": []}), DeepSeekSearchAdapter
        )
        tools = ToolGateway(
            search_adapter=search, model_gateway=ModelGateway(Mock(), {BINDING.ref: BINDING})
        )
        async with search:
            for query in ("", "　 ", "🎵" * 129):
                with pytest.raises(CallDenied, match="SEARCH_QUERY_LENGTH_INVALID"):
                    await tools.search(execution, candidate_id="album", query=query)
            for allowed, blocked in [(("one.example",), ()), ((), ("one.example",))]:
                with pytest.raises(CallDenied, match="SEARCH_DOMAIN_SCOPE_INVALID"):
                    await search.search(BINDING, "Artist Album", allowed, blocked)
            cold = ColdEvidence()
            await cold.prepare(
                execution,
                [
                    {
                        "release_group_id": "album",
                        "identity": {"title": "唱" * 128, "artist_credit": "Artist"},
                    }
                ],
                tools,
            )
        assert cold.events[0]["error"] == "SEARCH_QUERY_LENGTH_INVALID"
        assert not requests
        execution.invocation_plan.assert_not_called()
        execution.invoke.assert_not_called()

    asyncio.run(run())


def test_gateway_search_uses_minimum_bounds_and_filters_invalid_urls():
    async def run():
        execution = execution_for(BINDING)
        settled = []

        async def invoke(plan, sender, **kwargs):
            result = await sender()
            settled.append(result)
            return result

        execution.invoke.side_effect = invoke
        search, requests = adapter_for(
            httpx.Response(200, json={"results": [{"url": "http://127.0.0.1/private"}]}),
            DeepSeekSearchAdapter,
        )
        tools = ToolGateway(
            search_adapter=search, model_gateway=ModelGateway(Mock(), {BINDING.ref: BINDING})
        )
        async with search:
            assert await tools.search(execution, candidate_id="album", query="Artist Album") == ()
        assert len(requests) == 1 and len(settled[0].output) == 1
        assert settled[0].usage == Usage(0, 0, 0)
        plan = execution.invocation_plan.call_args.kwargs
        assert (plan["max_input_tokens"], plan["max_output_tokens"]) == (1, 1)

    asyncio.run(run())


def test_configurations_and_java_export():
    assert model_provider({}) == model_provider({"model_provider": "openai"}) == "openai"
    with pytest.raises(ValueError, match="UNKNOWN_MODEL_PROVIDER"):
        model_provider({"model_provider": "invalid"})
    for file in ("agent-service.example.json", "agent-service.deepseek.example.json"):
        service = json.loads((EXAMPLES / file).read_text())
        roles = load_role_bindings(EXAMPLES / service["model_bindings"])
        provider = model_provider(service)
        validate_provider_bindings(provider, roles)
        assert not any(binding.live_enabled for binding in roles.values())
        if provider == "deepseek":
            resources = service["service_resources"]
            for binding in roles.values():
                quota = resources["quota_groups"][resources["model_groups"][binding.model]]
                assert quota["tpm"] >= binding.max_input_tokens + binding.max_output_tokens
            roles["select"] = roles["write"]
            fragment = java_config(roles)
            assert fragment["model_bindings"] == {
                role: binding.wire() for role, binding in roles.items()
            }
            assert len(fragment["prices"]) == 4
            for binding in roles.values():
                assert fragment["prices"][binding.ref] == {
                    "revision": binding.price_revision,
                    "input": binding.input_microusd_per_million,
                    "cachedInput": binding.cached_input_microusd_per_million,
                    "output": binding.output_microusd_per_million,
                    "search": binding.search_microusd_per_call,
                    "maxInput": binding.max_input_tokens,
                    "maxOutput": binding.max_output_tokens,
                }


@pytest.mark.parametrize(
    "role,change,message",
    [
        ("long_write", None, "FIVE_MODEL_ROLES_REQUIRED"),
        ("screen", {"web_search": False}, "SCREEN_SEARCH_NOT_CONFIGURED"),
        (
            "screen",
            {"live_enabled": True, "search_microusd_per_call": None},
            "SCREEN_SEARCH_NOT_CONFIGURED",
        ),
        ("write", {"structured_outputs": False}, "STRUCTURED_OUTPUTS_REQUIRED"),
        ("interpret", {"model": "other"}, "INVALID_DEEPSEEK_BINDING"),
        ("select", {"service_tier": "fast"}, "INVALID_DEEPSEEK_BINDING"),
        ("long_write", {"reasoning_effort": "other"}, "INVALID_DEEPSEEK_BINDING"),
    ],
)
def test_invalid_provider_combinations(role, change, message):
    roles = load_role_bindings(EXAMPLES / "deepseek-bindings.example.json")
    if change is None:
        del roles[role]
    else:
        roles[role] = replace(roles[role], **change)
    with pytest.raises(ValueError, match=message):
        validate_provider_bindings("deepseek", roles)
