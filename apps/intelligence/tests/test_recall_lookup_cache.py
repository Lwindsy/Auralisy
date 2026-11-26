"""Recall lookups built once per dataset must keep per-request results unchanged."""

import json
from uuid import UUID

import pytest

from auralisy_intelligence.contracts import content_digest
from auralisy_intelligence.recall import Preference, RecallRequest, WeightConfig, load_a_v2
from auralisy_intelligence.recall.common import id_positions
from auralisy_intelligence.recall.full_audience import FullAudience

IDS = [str(UUID(int=i)) for i in range(1, 7)]
# The last album is absent from the full audience, which the old per-album rule read as -1.
LISTENERS = [0, 5, 10, 50, 500]


def engine_with_audience(tmp_path, listeners=LISTENERS, name="full.json"):
    train = tmp_path / "train.json"
    train.write_text(
        json.dumps(
            {
                "format_version": 1,
                "dataset_id": "train",
                "window": "0",
                "album_ids": IDS,
                "tag_ids": ["rock"],
                "tag_parents": [],
                "album_tags": [[1]] * len(IDS),
                "interactions": [[1] * len(IDS), [1, 0, 1, 0, 1, 0]],
            }
        )
    )
    engine = load_a_v2(train, weights=WeightConfig())
    return engine, bind_audience(engine, tmp_path, listeners, name)


def bind_audience(engine, tmp_path, listeners, name):
    artifact = {
        "format_version": 2,
        "scope": "FULL_CATALOG",
        "source": "LISTENBRAINZ_SAMPLE",
        "catalog_dataset_id": 1,
        "source_manifest_sha256": "a" * 64,
        "dataset_id": "core:" + "a" * 64,
        "window": "0",
        "sample_listeners": 1000,
        "counts": [{"release_group_id": i, "listeners": n} for i, n in zip(IDS, listeners)],
    }
    artifact["digest"] = content_digest(artifact, "digest")
    path = tmp_path / name
    path.write_text(json.dumps(artifact))
    binding = {k: artifact[k] for k in ("dataset_id", "window", "sample_listeners")} | {
        "snapshot_digest": artifact["digest"]
    }
    audience = FullAudience(path, binding)
    engine.bind_full_audience(audience)
    return audience


def per_album_rule(audience, minimum, maximum):
    """The previous implementation, applied album by album."""
    return {i for i in IDS if minimum <= audience.counts.get(i, -1) <= maximum}


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [
        (0, None),
        (5, 50),
        (6, 49),
        (10, 10),
        (51, None),
        (0, 10**30),
        (10**30, None),
    ],
)
def test_vectorized_audience_filter_matches_per_album_rule(tmp_path, minimum, maximum):
    engine, audience = engine_with_audience(tmp_path)
    request = RecallRequest(limit=len(IDS), tags=(Preference("rock"),))
    result = engine.recall(request, min_listeners=minimum, max_listeners=maximum)
    upper = float("inf") if maximum is None else maximum
    assert {row.album_id for row in result.candidates} == per_album_rule(audience, minimum, upper)


def test_audience_counts_are_built_once_and_follow_rebinding(tmp_path):
    engine, _ = engine_with_audience(tmp_path)
    first = engine._audience_counts()
    assert first.tolist() == LISTENERS + [-1]
    assert engine._audience_counts() is first
    bind_audience(engine, tmp_path, [7, 7, 7, 7, 7, 7], "other.json")
    assert engine._audience_counts().tolist() == [7] * len(IDS)


def test_id_positions_are_cached_per_id_tuple(tmp_path):
    engine, _ = engine_with_audience(tmp_path)
    data = engine._model.data
    albums, tags = id_positions(data)
    assert albums == {value: index for index, value in enumerate(IDS)}
    assert tags == {"rock": 0}
    assert id_positions(data)[0] is albums
    data.album_ids = tuple(reversed(data.album_ids))
    rebuilt, _ = id_positions(data)
    assert rebuilt is not albums and rebuilt[IDS[0]] == len(IDS) - 1
    data.album_ids = list(data.album_ids)
    assert id_positions(data)[0] is not id_positions(data)[0]
