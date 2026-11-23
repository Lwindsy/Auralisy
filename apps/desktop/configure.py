"""Assemble one matching policy for the Java coordinator and Python agent."""

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from auralisy_intelligence.contracts import content_digest, digest, validate_definition
from auralisy_intelligence.genre_taxonomy import check_artifacts
from auralisy_intelligence.preferences import ReferenceCatalog
from auralisy_intelligence.preferences.stage import PARSER_REVISION
from auralisy_intelligence.providers.types import ModelBinding
from auralisy_intelligence.recall import WeightConfig

HERE = Path(__file__).resolve().parent
DATA = Path("/bundle")
CONFIG = Path("/config")
MODEL = "gpt-5.4-mini-2026-03-17"
PRICE_SOURCE = "https://developers.openai.com/api/docs/models/gpt-5.4-mini"


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(name: str, document: dict) -> None:
    path = CONFIG / name
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def main() -> None:
    synthetic = os.environ["DESKTOP_SYNTHETIC"] == "1"
    metadata = read(DATA / "bundle.json")
    snapshot_path = DATA / "taxonomy/snapshot.json" if synthetic else Path("/recall")
    taxonomy = DATA / "taxonomy/taxonomy.json"
    reference_path = DATA / "taxonomy/reference.json"
    check_artifacts(taxonomy, reference_path, snapshot_path)
    reference = ReferenceCatalog.load(reference_path, taxonomy_path=taxonomy)
    snapshot = read(snapshot_path if synthetic else snapshot_path / "dataset.json")
    audience = read(DATA / "audience.json")
    if (
        content_digest(audience, "digest") != audience["digest"]
        or audience["digest"] != metadata["audience_digest"]
    ):
        raise ValueError("AUDIENCE_DIGEST_MISMATCH")
    audience_binding = {
        key: audience[key] for key in ("dataset_id", "window", "sample_listeners")
    } | {"snapshot_digest": audience["digest"]}

    session = int(os.environ["DESKTOP_SESSION_MICROUSD"])
    round_cap = int(os.environ["DESKTOP_ROUND_MICROUSD"])
    if not 0 < round_cap <= session <= 2_147_483_647:
        raise ValueError("INVALID_SESSION_OR_ROUND_BUDGET")

    models = read(HERE / "autonomous-bindings.example.json")
    prices = {}
    for role, config in models.items():
        config.update(
            ref=f"desktop-{role}",
            model=MODEL,
            price_revision="desktop-openai-5.4-mini-2026-03-17",
            price_source=PRICE_SOURCE,
            input_microusd_per_million=750_000,
            cached_input_microusd_per_million=75_000,
            output_microusd_per_million=4_500_000,
            search_microusd_per_call=10_000,
            reasoning_effort="none",
            live_enabled=not synthetic,
        )
        config.pop("service_tier", None)
        if role == "screen":
            # Hosted search has a 128k context window. Include room for request framing.
            config["max_input_tokens"] = 132_000
        if synthetic:
            config.update(
                price_revision="desktop-synthetic-v1",
                price_source=None,
                input_microusd_per_million=1,
                cached_input_microusd_per_million=1,
                output_microusd_per_million=1,
                search_microusd_per_call=1,
            )
        prices[config["ref"]] = {
            "revision": config["price_revision"],
            "input": config["input_microusd_per_million"],
            "cachedInput": config["cached_input_microusd_per_million"],
            "output": config["output_microusd_per_million"],
            "search": config["search_microusd_per_call"],
            "maxInput": config["max_input_tokens"],
            "maxOutput": config["max_output_tokens"],
        }

    service = read(HERE / "agent-service.example.json")
    service.update(
        reference=str(reference_path),
        genre_taxonomy=str(taxonomy),
        recall_snapshot=str(snapshot_path),
        audience_snapshot=str(DATA / "audience.json"),
        model_bindings="models.json",
        control_token_env="AURALIS_SERVICE_TOKEN",
        control_url="http://127.0.0.1:8080",
        audience_binding=audience_binding,
        execution_capacity=1,
    )
    service["service_resources"].update(
        model=2,
        search=2,
        read=4,
        cpu=1,
        per_host=2,
        background=1,
        quota_groups={"desktop": {"rpm": 60, "tpm": 400_000}},
        model_groups={MODEL: "desktop"},
    )

    settings = read(HERE / "policy.json")
    policy = settings["agent_policy"]
    policy["audience_binding"] = audience_binding
    policy["recall"]["weights"] = asdict(WeightConfig())
    policy["digest"] = content_digest(policy, "digest")
    foreground = round_cap * 3 // 5
    settings.update(
        stage_capacity=settings["selection_config"],
        model_bindings={role: ModelBinding(**config).wire() for role, config in models.items()},
        prices=prices,
        budget_limits={
            "session_cap_microusd": session,
            "round_cap_microusd": round_cap,
            "model_calls": 40,
            "search_calls": 12,
            "read_calls": 60,
            "lane_caps": {"FOREGROUND": foreground, "BACKGROUND": round_cap - foreground},
        },
        reference_revision=reference.revision,
        parser_revisions=[PARSER_REVISION, "generic-discovery.v2"],
        reference_genres={key: item["name"] for key, item in reference.genres.items()},
        recall_binding={
            "strategy_revision": "A-V2",
            "data_snapshot_id": snapshot["dataset_id"],
            "window": snapshot["window"],
            "weights_digest": digest(asdict(WeightConfig())),
        },
        audience_binding={
            "dataset": audience_binding["dataset_id"],
            "window": audience_binding["window"],
            "sampleListeners": audience_binding["sample_listeners"],
            "snapshotDigest": audience_binding["snapshot_digest"],
        },
    )
    validate_definition("RecallBinding", settings["recall_binding"])
    for name, document in (
        ("models.json", models),
        ("agent-service.json", service),
        ("recommendation.json", settings),
    ):
        write(name, document)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "auralisy_intelligence.recall.prepare_reach",
            "--config",
            str(CONFIG / "agent-service.json"),
            "--output",
            str(CONFIG / "genre-reach.json"),
            "--replace",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
