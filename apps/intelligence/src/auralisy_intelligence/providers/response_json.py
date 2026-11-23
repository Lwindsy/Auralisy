"""Bounded JSON decoding and local structured-output validation."""

import json
from dataclasses import replace
from typing import Any

import httpx
from auralisy_intelligence.agents.ports import CallResult
from jsonschema import Draft202012Validator


async def read_json_object(response: httpx.Response, max_bytes: int) -> dict[str, Any] | None:
    chunks = bytearray()
    async for chunk in response.aiter_bytes():
        if len(chunks) + len(chunk) > max_bytes:
            return None
        chunks.extend(chunk)
    try:
        raw = json.loads(chunks)
    except (ValueError, RecursionError):
        return None
    return raw if isinstance(raw, dict) else None


def _reject_nonfinite(value: str) -> None:
    raise ValueError("NONFINITE_JSON_NUMBER")


def validate_output(
    text: str,
    schema: dict[str, Any],
    reply: CallResult,
    shape: dict[str, Any],
    *,
    strict_json: bool = False,
) -> CallResult:
    try:
        output = json.loads(text, parse_constant=_reject_nonfinite if strict_json else None)
    except (ValueError, TypeError, RecursionError) as error:
        return CallResult(
            outcome=reply.outcome,
            finish="ERROR",
            usage=reply.usage,
            provider_request_ref=reply.provider_request_ref,
            service_tier=reply.service_tier,
            diagnostic={
                "category": "JSON_PARSE_FAILED",
                "line": getattr(error, "lineno", None),
                "column": getattr(error, "colno", None),
                "output_text_chars": len(text),
                "parse_error": getattr(error, "msg", "INVALID_JSON")[:80],
                **shape,
            },
        )
    error = next(Draft202012Validator(schema).iter_errors(output), None)
    if error is not None:
        return CallResult(
            output=output,
            outcome=reply.outcome,
            finish="ERROR",
            usage=reply.usage,
            provider_request_ref=reply.provider_request_ref,
            diagnostic={
                "category": "SCHEMA_VALIDATION_FAILED",
                "validator": error.validator,
                "schema_path": list(error.absolute_schema_path),
                "schema_errors": [
                    {
                        "validator": item.validator,
                        "schema_path": list(item.absolute_schema_path),
                        "missing_required": [
                            name for name in item.validator_value if name not in item.instance
                        ]
                        if item.validator == "required" and isinstance(item.instance, dict)
                        else [],
                    }
                    for item in [error, *error.context[:4]]
                ],
            },
        )
    return replace(reply, output=output)
