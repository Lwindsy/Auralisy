"""Export the Java recommendation fragment from the Python role bindings."""

import argparse
import json
from pathlib import Path

from .config import bindings_by_ref, load_role_bindings
from .types import ModelBinding


def java_config(roles: dict[str, ModelBinding]) -> dict:
    prices = {}
    for ref, binding in bindings_by_ref(roles).items():
        prices[ref] = {
            "revision": binding.price_revision,
            "input": binding.input_microusd_per_million,
            "cachedInput": binding.cached_input_microusd_per_million,
            "output": binding.output_microusd_per_million,
            "search": binding.search_microusd_per_call,
            "maxInput": binding.max_input_tokens,
            "maxOutput": binding.max_output_tokens,
        }
    return {
        "model_bindings": {role: binding.wire() for role, binding in roles.items()},
        "prices": prices,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bindings", type=Path)
    args = parser.parse_args()
    print(json.dumps(java_config(load_role_bindings(args.bindings)), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
