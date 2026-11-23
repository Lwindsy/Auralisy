"""Explicit local binding configuration; credentials are supplied separately."""

import json
from pathlib import Path

from auralisy_intelligence.contracts import validate_definition

from .types import ModelBinding

MODEL_ROLES = ("interpret", "screen", "select", "write", "long_write")
DEEPSEEK_MODELS = ("deepseek-flash", "deepseek-v4-pro")


def model_provider(config: dict) -> str:
    provider = config.get("model_provider", "openai")
    if provider not in ("openai", "deepseek"):
        raise ValueError("UNKNOWN_MODEL_PROVIDER")
    return provider


def load_role_bindings(path: str | Path) -> dict[str, ModelBinding]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        not isinstance(raw, dict)
        or not set(MODEL_ROLES[:4]).issubset(raw)
        or set(raw) - set(MODEL_ROLES)
    ):
        raise ValueError("FOUR_MODEL_ROLES_REQUIRED")
    roles = {role: ModelBinding(**value) for role, value in raw.items()}
    for binding in roles.values():
        validate_definition("ModelBinding", binding.wire())
    return roles


def bindings_by_ref(roles: dict[str, ModelBinding]) -> dict[str, ModelBinding]:
    bindings = {}
    for binding in roles.values():
        if binding.ref in bindings and bindings[binding.ref] != binding:
            raise ValueError("CONFLICTING_MODEL_BINDING")
        bindings[binding.ref] = binding
    return bindings


def load_bindings(path: str | Path) -> dict[str, ModelBinding]:
    return bindings_by_ref(load_role_bindings(path))


def validate_provider_bindings(provider: str, roles: dict[str, ModelBinding]) -> None:
    if set(roles) != set(MODEL_ROLES):
        raise ValueError("FIVE_MODEL_ROLES_REQUIRED")
    screen = roles["screen"]
    price = screen.search_microusd_per_call
    if screen.web_search is not True or (
        screen.live_enabled and (type(price) is not int or price < 0)
    ):
        raise ValueError("SCREEN_SEARCH_NOT_CONFIGURED")
    if any(binding.structured_outputs is not True for binding in roles.values()):
        raise ValueError("STRUCTURED_OUTPUTS_REQUIRED")
    if provider == "deepseek" and any(
        binding.model not in DEEPSEEK_MODELS
        or binding.service_tier is not None
        or binding.reasoning_effort not in (None, "none", "low", "high", "max")
        for binding in roles.values()
    ):
        raise ValueError("INVALID_DEEPSEEK_BINDING")
