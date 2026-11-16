"""Consumer-shaped wire fixtures, binding and duplicate semantics (T-07/12/28)."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from auralisy_intelligence.contracts import (
    ContractError,
    IdempotencyLedger,
    content_digest,
    digest,
    require_binding,
    schema,
    validate_definition,
    validate_message,
)
from jsonschema import Draft202012Validator, FormatChecker

CASES = json.loads(
    (Path(__file__).parent / "fixtures/agent-contract-cases.json").read_text(encoding="utf-8")
)
INTEGRATION_CASES = json.loads(
    (Path(__file__).parent / "fixtures/java-integration-cases.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", INTEGRATION_CASES, ids=lambda case: case["name"])
def test_java_integration_definitions(case):
    document = {**schema(), "$ref": "#/$defs/" + case["definition"]}
    validator = Draft202012Validator(document, format_checker=FormatChecker())
    assert validator.is_valid(case["payload"]) == case["structural_valid"]
    if case["semantic_valid"]:
        validate_definition(case["definition"], case["payload"])
    else:
        with pytest.raises(ContractError):
            validate_definition(case["definition"], case["payload"])


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_wire_cases(case):
    Draft202012Validator.check_schema(schema())
    validator = Draft202012Validator(schema(), format_checker=FormatChecker())
    assert validator.is_valid(case["message"]) == case["structural_valid"]
    if case["semantic_valid"]:
        validate_message(case["message"])
    else:
        with pytest.raises(ContractError):
            validate_message(case["message"])


def test_wire_variants_have_unique_required_discriminators():
    variants = schema()["$defs"]["Envelope"]["oneOf"]
    names = [variant["properties"]["message_type"]["const"] for variant in variants]
    assert len(names) == len(set(names))
    assert all("message_type" in variant["required"] for variant in variants)


@pytest.mark.parametrize("name", ["UnknownMessage", "", None, [], {}])
def test_wire_rejects_unknown_or_non_string_message_type(name):
    message = deepcopy(CASES[0]["message"])
    message["message_type"] = name
    with pytest.raises(ContractError, match="INVALID_PAYLOAD"):
        validate_message(message)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "intel.old"),
        ("contract_revision", "0.31"),
        ("request_id", ""),
        ("trace_context", {}),
        ("payload", None),
        ("unexpected", True),
    ],
)
def test_wire_branch_still_validates_headers_and_payload(field, value):
    message = deepcopy(CASES[0]["message"])
    message[field] = value
    with pytest.raises(ContractError, match="INVALID_PAYLOAD"):
        validate_message(message)


def test_wire_branch_keeps_payload_semantic_validation():
    message = deepcopy(CASES[0]["message"])
    message["message_type"] = "RuntimeView"
    message["payload"] = deepcopy(
        next(
            case["payload"]
            for case in INTEGRATION_CASES
            if case["name"] == "duplicate-active-rejected"
        )
    )
    assert Draft202012Validator(schema(), format_checker=FormatChecker()).is_valid(message)
    with pytest.raises(ContractError, match="DUPLICATE_ID"):
        validate_message(message)


@pytest.mark.parametrize("message", [None, [], "not an envelope"])
def test_wire_rejects_non_object_envelopes(message):
    with pytest.raises(ContractError, match="INVALID_PAYLOAD"):
        validate_message(message)


@pytest.mark.parametrize("capacity", [0, 17, -1, True, "3", None])
def test_runtime_rejects_unsupported_capacity(capacity):
    with pytest.raises(ContractError):
        validate_definition(
            "RuntimeView",
            {
                "boot_id": "python-test",
                "status": "READY",
                "execution_capacity": capacity,
                "active": [],
            },
        )


def test_unknown_and_conflicting_bindings_do_not_leak_payload():
    with pytest.raises(ContractError, match="BINDING_MISMATCH") as result:
        require_binding({"secret-user-text": "wrong"}, {"expected": "binding"})
    assert "secret-user-text" not in str(result.value)


def test_duplicate_same_payload_returns_original_result_and_conflict_rejects():
    ledger = IdempotencyLedger(1)
    payload = {"value": 1}
    ledger.record("delivery-1", payload, {"accepted": True})
    found, result = ledger.lookup("delivery-1", payload)
    assert found and result == {"accepted": True}
    result["accepted"] = False
    assert ledger.lookup("delivery-1", payload)[1]["accepted"]
    with pytest.raises(ContractError, match="IDEMPOTENCY_CONFLICT"):
        ledger.lookup("delivery-1", {"value": 2})
    with pytest.raises(ContractError, match="LEDGER_CAPACITY"):
        ledger.record("delivery-2", payload, {})
    ledger.clear()
    assert ledger.lookup("delivery-1", payload) == (False, None)


def test_forged_recomputed_start_still_rejects_raw_text_in_later_round():
    message = deepcopy(CASES[0]["message"])
    message["payload"]["round_seq"] = 2
    message["payload"]["payload_digest"] = content_digest(message["payload"])
    with pytest.raises(ContractError, match="RAW_TEXT_LIFETIME"):
        validate_message(message)


def test_digest_uses_jcs_number_and_key_canonicalization():
    assert digest({"b": 1.0, "a": "中文"}) == digest({"a": "中文", "b": 1})
    assert digest([1, 2]) != digest([2, 1])


@pytest.mark.parametrize("retired", ["operation", "curated_candidates"])
def test_retired_online_curated_input_is_rejected(retired):
    message = deepcopy(CASES[0]["message"])
    message["payload"][retired] = "CURATED_REVIEWS" if retired == "operation" else None
    message["payload"]["payload_digest"] = content_digest(message["payload"])
    with pytest.raises(ContractError):
        validate_message(message)


@pytest.mark.parametrize("value", [0, 1, 10, 50, 500, 2147483647])
def test_audience_minimum_accepts_nonnegative_integer(value):
    validate_definition("AudienceMinimum", value)


@pytest.mark.parametrize("value", [-1, 1.5, "500", None, True, 2147483648])
def test_audience_minimum_rejects_invalid_values(value):
    with pytest.raises(ContractError):
        validate_definition("AudienceMinimum", value)
