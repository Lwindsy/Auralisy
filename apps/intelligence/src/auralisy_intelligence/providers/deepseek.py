"""Independent DeepSeek Responses and Search APIs, with one send per call."""

import json
from dataclasses import replace

import httpx
from auralisy_intelligence.agents.ports import CallDenied, CallResult, Usage

from .response_json import read_json_object, validate_output
from .types import ModelBinding, ModelRequest, SearchHit


class _DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 120,
        max_response_bytes: int = 2_000_000,
        test_transport: bool = False,
    ) -> None:
        if not api_key:
            raise ValueError("MISSING_API_KEY")
        if test_transport and transport is None:
            raise ValueError("TEST_TRANSPORT_REQUIRED")
        self.test_transport = test_transport
        self.max_response_bytes = max_response_bytes
        self.client = httpx.AsyncClient(
            base_url="https://api.deepseek.com/",
            headers={"Authorization": "Bearer " + api_key},
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def close(self) -> None:
        await self.client.aclose()

    def check_live_rates(self, binding: ModelBinding) -> None:
        rates = (
            binding.input_microusd_per_million,
            binding.cached_input_microusd_per_million,
            binding.output_microusd_per_million,
        )
        if not self.test_transport and (
            binding.live_enabled is not True
            or not isinstance(binding.price_source, str)
            or not binding.price_source.strip()
            or any(type(rate) is not int or rate < 0 for rate in rates)
            or rates[1] > rates[0]
        ):
            raise CallDenied("LIVE_CONFIGURATION_NOT_APPROVED")

    async def post(self, path, body, *, not_processed):
        try:
            async with self.client.stream("POST", path, json=body) as response:
                request_ref = response.headers.get("x-request-id")
                status = response.status_code
                if status == 200:
                    raw = await read_json_object(response, self.max_response_bytes)
                    if raw is not None:
                        return raw, request_ref
                return CallResult(
                    outcome="NOT_PROCESSED" if not_processed(status) else "UNCERTAIN",
                    finish="ERROR",
                    provider_request_ref=request_ref,
                    provider_http_status=status,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return CallResult(outcome="NOT_PROCESSED", finish="ERROR")
        except (httpx.HTTPError, OSError):
            return CallResult(outcome="UNCERTAIN", finish="ERROR")


class DeepSeekResponsesAdapter(_DeepSeekClient):
    def check_binding(self, binding: ModelBinding, *, search: bool = False) -> None:
        self.check_live_rates(binding)
        if binding.structured_outputs is not True:
            raise CallDenied("UNSUPPORTED_STRUCTURED_OUTPUT")

    async def complete(self, binding: ModelBinding, request: ModelRequest) -> CallResult:
        self.check_binding(binding)
        body = {
            "model": binding.model,
            "stream": False,
            "input": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": json.dumps(request.input, ensure_ascii=False)},
            ],
            "max_output_tokens": request.max_output_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": request.schema_name,
                    "schema": request.output_schema,
                }
            },
        }
        if binding.reasoning_effort is not None:
            body["reasoning"] = {"effort": binding.reasoning_effort}
        response = await self.post(
            "responses",
            body,
            not_processed=lambda status: status in {400, 401, 402, 403, 404, 413, 422, 429},
        )
        if isinstance(response, CallResult):
            return response
        raw, request_ref = response
        reply = self._normalize(raw, request_ref)
        if reply.finish != "COMPLETE":
            return replace(reply, diagnostic={"category": "MODEL_FAILED", "finish": reply.finish})
        items = raw.get("output")
        texts = [
            part["text"]
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, dict)
            and item.get("type") == "message"
            and isinstance(item.get("content"), list)
            for part in item["content"]
            if isinstance(part, dict)
            and part.get("type") == "output_text"
            and isinstance(part.get("text"), str)
        ]
        return validate_output(
            "".join(texts),
            request.output_schema,
            reply,
            {
                "output_text_parts": len(texts),
                "output_text_lengths": [len(value) for value in texts[:8]],
            },
            strict_json=True,
        )

    @staticmethod
    def _normalize(raw: dict, request_ref: str | None) -> CallResult:
        def token_count(value):
            return value if type(value) is int and value >= 0 else None

        usage = raw.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("input_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        status = raw.get("status")
        finish = "ERROR"
        if status == "completed":
            finish = "COMPLETE"
        elif status == "incomplete":
            details = raw.get("incomplete_details")
            filtered = isinstance(details, dict) and details.get("reason") == "content_filter"
            finish = "REFUSED" if filtered else "TRUNCATED"
        return CallResult(
            outcome="COMPLETED" if status in ("completed", "incomplete", "failed") else "UNCERTAIN",
            finish=finish,
            usage=Usage(
                token_count(usage.get("input_tokens")),
                token_count(usage.get("output_tokens")),
                token_count(cached),
            ),
            provider_request_ref=request_ref
            or (raw["id"] if isinstance(raw.get("id"), str) else None),
        )


class DeepSeekSearchAdapter(_DeepSeekClient):
    search_token_bounds = (1, 1)

    def check_binding(self, binding: ModelBinding, *, search: bool = False) -> None:
        self.check_live_rates(binding)
        price = binding.search_microusd_per_call
        if not self.test_transport and (type(price) is not int or price < 0):
            raise CallDenied("SEARCH_PRICE_NOT_CONFIGURED")
        if binding.web_search is not True:
            raise CallDenied("UNSUPPORTED_WEB_SEARCH")

    def check_query(
        self, query: str, allowed_domains: tuple[str, ...], blocked_domains: tuple[str, ...]
    ) -> None:
        if not 1 <= len(query) <= 128 or not query.strip():
            raise CallDenied("SEARCH_QUERY_LENGTH_INVALID")
        if allowed_domains or blocked_domains:
            raise CallDenied("SEARCH_DOMAIN_SCOPE_INVALID")

    async def search(
        self,
        binding: ModelBinding,
        query: str,
        allowed_domains: tuple[str, ...],
        blocked_domains: tuple[str, ...] = (),
    ) -> CallResult:
        self.check_binding(binding, search=True)
        self.check_query(query, allowed_domains, blocked_domains)
        response = await self.post(
            "v1/tools/search",
            {"query": query, "count": 10},
            not_processed=lambda status: 400 <= status <= 599,
        )
        if isinstance(response, CallResult):
            return response
        raw, request_ref = response
        results = raw.get("results")
        if not isinstance(results, list):
            return CallResult(
                outcome="UNCERTAIN",
                finish="ERROR",
                provider_request_ref=request_ref,
                provider_http_status=200,
            )
        hits = {}
        for result in results:
            if not isinstance(result, dict) or not isinstance(result.get("url"), str):
                continue
            url = result["url"]
            title, snippet = result.get("title"), result.get("snippet")
            hits.setdefault(
                url,
                SearchHit(
                    url,
                    title if isinstance(title, str) else "",
                    snippet if isinstance(snippet, str) else "",
                ),
            )
        return CallResult(
            tuple(hits.values()), usage=Usage(0, 0, 0), provider_request_ref=request_ref
        )
