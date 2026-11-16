import json
from copy import deepcopy
from uuid import UUID

import pytest
from auralisy_intelligence.contracts import content_digest
from auralisy_intelligence.genre_taxonomy import taxonomy_hash
from auralisy_intelligence.recall import Preference, RecallRequest, WeightConfig, load_a_v2
from auralisy_intelligence.recall.full_audience import FullAudience
from auralisy_intelligence.recall.genre_reach import (
    ReachIndex,
    build_index,
    make_bands,
    within_policy,
)


def fixture(root):
    taxonomy = {
        "format_version": 1,
        "source_sha256": "a" * 64,
        "core_manifest_sha256": "b" * 64,
        "genres": [
            {"id": g, "name": g, "aliases": [], "parents": parents}
            for g, parents in [
                ("navigation", []),
                ("bridge", ["navigation"]),
                ("rock", ["navigation"]),
                ("post-rock", ["rock", "bridge"]),
                ("jazz", ["navigation"]),
                ("empty", []),
                ("zero", []),
                ("sparse", []),
                ("flat", []),
            ]
        ],
    }
    tax = root / "taxonomy.json"
    tax.write_text(json.dumps(taxonomy), encoding="utf-8")
    tags = [g["id"] for g in taxonomy["genres"]]
    records = [(["rock", "post-rock", "jazz"], n) for n in range(10, 211, 10)]
    records += [(["zero"], 0), (["zero"], 0), (["sparse"], 100), (["sparse"], 285)]
    records += [(["flat"], 1), (["flat"], 1), (["flat"], 11)]
    ids = [str(UUID(int=i + 1)) for i in range(len(records))]
    data = {
        "format_version": 1,
        "dataset_id": "reach-fixture",
        "window": "0",
        "album_ids": ids,
        "tag_ids": tags,
        "tag_parents": [
            [parent, node["id"]] for node in taxonomy["genres"] for parent in node["parents"]
        ],
        "album_tags": [[int(g in gs) for g in tags] for gs, _ in records],
        "interactions": [[int(i < 21) for i in range(len(records))]],
        "provenance": {"taxonomy_sha256": taxonomy_hash(tax)},
    }
    snapshot = root / "snapshot.json"
    snapshot.write_text(json.dumps(data), encoding="utf-8")
    audience = {
        "format_version": 2,
        "scope": "FULL_CATALOG",
        "source": "LISTENBRAINZ_SAMPLE",
        "catalog_dataset_id": 1,
        "source_manifest_sha256": "b" * 64,
        "dataset_id": "core:" + "b" * 64,
        "window": "0",
        "sample_listeners": 1000,
        "counts": [{"release_group_id": i, "listeners": row[1]} for i, row in zip(ids, records)],
    }
    audience["digest"] = content_digest(audience, "digest")
    audience_path = root / "audience.json"
    audience_path.write_text(json.dumps(audience), encoding="utf-8")
    document = build_index(snapshot, tax, audience_path)
    return snapshot, tax, audience_path, document


def test_profiles_direct_zero_sparse_flat_and_hidden(tmp_path):
    _, taxonomy, _, document = fixture(tmp_path)
    index = ReachIndex.load(_write(tmp_path / "index.json", document), taxonomy)
    assert index.profiles["empty"]["mode"] == "HIDDEN"
    for g in ("zero", "sparse", "flat"):
        assert index.profiles[g]["mode"] == "DIRECT"
        for n in (0, 50, 100):
            proof = index.proof([g], {}, n)
            policy = {"audience_reach_percent": n, "audience_reach_index_digest": index.digest}
            assert within_policy({"listeners": 0, "reach": proof}, policy)
    assert index.profiles["rock"]["mode"] == "NORMAL"
    assert all(b["candidate_count"] >= 10 for b in index.profiles["rock"]["bands"])


def _write(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_outward_rounding_gaps_whole_ties_and_small_population():
    # 2.05..2.93 has no integer under inward rounding; retain both complete count groups.
    bands = make_bands([1, 2, 2, 3, 3, 6], 1, 6)
    assert bands[50]["minimum"] <= 2 <= 3 <= bands[50]["maximum"]
    values = [1] * 20 + [83] * 4 + [241] * 7 + [481] * 3
    band = make_bands(values, 49, 481)[50]
    assert band["candidate_count"] >= 10
    assert band["minimum"] >= 49
    assert sum(band["minimum"] <= v <= band["maximum"] for v in values) == band["candidate_count"]
    # Both equidistant groups enter together, even if the first alone would meet the target.
    tied = make_bands([10] * 10 + [40] * 10, 10, 40)[50]
    assert tied["candidate_count"] == 20
    assert make_bands([5, 20], 5, 20)[0]["candidate_count"] == 2
    assert make_bands([10, 100, 1000], 10, 100)[100]["maximum"] is None


def test_reference_selection_and_proof_binding(tmp_path):
    _, tax, _, document = fixture(tmp_path)
    index = ReachIndex(document, json.loads(tax.read_text()))
    tags = ["rock", "post-rock", "jazz"]
    assert index.reference(tags, {"rock": 4}) == "rock"
    assert index.reference(tags, {"rock": 4, "post-rock": 1}) == "post-rock"
    assert index.reference(tags, {"jazz": 4, "post-rock": 1}) == "jazz"
    assert index.reference(["rock", "post-rock"], {}) == "post-rock"
    assert index.reference(["empty"], {}) is None
    proof = index.proof(tags, {"rock": 1}, 50)
    policy = {"audience_reach_percent": 50, "audience_reach_index_digest": index.digest}
    assert within_policy({"listeners": proof["minimum"], "reach": proof}, policy)
    assert not within_policy({"listeners": proof["minimum"] - 1, "reach": proof}, policy)
    assert not within_policy(
        {"listeners": 100, "reach": proof}, policy | {"audience_reach_percent": 51}
    )
    changed = deepcopy(document)
    changed["minimum_candidates"] = 11
    with pytest.raises(ValueError):
        ReachIndex(changed, json.loads(tax.read_text()))


def test_zero_audience_recalled_without_common_listeners_and_constraints_preserved(tmp_path):
    snapshot, tax, audience, document = fixture(tmp_path)
    engine = load_a_v2(snapshot, weights=WeightConfig())
    engine.bind_full_audience(FullAudience(audience, document["audience_binding"]))
    engine.bind_reach_index(ReachIndex(document, json.loads(tax.read_text())))
    request = RecallRequest(limit=10, tags=(Preference("zero"),))
    result = engine.recall(request, min_listeners=50, reach_percent=100)
    assert result.returned_count == 2
    assert set(engine.audience([c.album_id for c in result.candidates])["counts"].values()) == {0}
    # Seed-only co-listening has no score: the direct-pool route still supplies a related album.
    seed = result.candidates[0].album_id
    result = engine.recall(RecallRequest(limit=10, seeds=(Preference(seed),)), reach_percent=0)
    assert result.returned_count == 1 and result.candidates[0].album_id != seed
    result = engine.recall(
        RecallRequest(limit=10, tags=(Preference("zero"),), forbidden_tags=("zero",)),
        reach_percent=50,
    )
    assert result.returned_count == 0


def test_all_generated_bands_obey_floor_population_and_no_splitting():
    values = [1] * 40 + [5, 8, 11, 11, 15, 15, 15, 25, 100, 900]
    bands = make_bands(values, 5, 100)
    for band in bands:
        assert band["minimum"] >= 5
        actual = [
            v
            for v in values
            if v >= band["minimum"] and (band["maximum"] is None or v <= band["maximum"])
        ]
        assert len(actual) == band["candidate_count"] >= 10


def test_new_direct_link_reactivates_genre_on_rebuild(tmp_path):
    snapshot, taxonomy, audience, document = fixture(tmp_path)
    assert next(g for g in document["genres"] if g["genre_id"] == "empty")["mode"] == "HIDDEN"
    data = json.loads(snapshot.read_text())
    data["album_tags"][0][data["tag_ids"].index("empty")] = 1
    snapshot.write_text(json.dumps(data), encoding="utf-8")
    rebuilt = build_index(snapshot, taxonomy, audience)
    index = ReachIndex(rebuilt, json.loads(taxonomy.read_text()))
    assert "empty" in index.selectable
    assert index.profiles["empty"]["mode"] == "DIRECT"
    assert rebuilt["digest"] != document["digest"]


def test_per_direction_recall_keeps_full_request_reference(tmp_path):
    snapshot, taxonomy, audience, document = fixture(tmp_path)
    # Give Jazz a different supplied band to distinguish reference selection from scoring.
    jazz = next(g for g in document["genres"] if g["genre_id"] == "jazz")
    jazz["bands"][50] = {"minimum": 200, "maximum": 210, "candidate_count": 2}
    document["digest"] = content_digest(document, "digest")
    engine = load_a_v2(snapshot, weights=WeightConfig())
    engine.bind_full_audience(FullAudience(audience, document["audience_binding"]))
    engine.bind_reach_index(ReachIndex(document, json.loads(taxonomy.read_text())))
    full = RecallRequest(
        limit=100, tags=(Preference("rock", focused=True, strength="special"), Preference("jazz"))
    )
    sub = RecallRequest(limit=100, tags=(Preference("jazz"),), required_tags=("jazz",))
    expected = engine.recall(full, reach_percent=50)
    actual = engine.recall(sub, reach_percent=50, reference_request=full)
    assert {c.album_id for c in actual.candidates} == {c.album_id for c in expected.candidates}
    assert len(actual.candidates) >= 10
    assert engine.recall(sub, reach_percent=50).returned_count == 2


def test_preference_material_keeps_available_parents_selectable():
    from auralisy_intelligence.preferences import ReferenceCatalog

    genres = [
        {"id": g, "name": g, "aliases": [], "description": g} for g in ["root", "bridge", "leaf"]
    ]
    taxonomy = {
        "genres": [
            {**g, "parents": parents} for g, parents in zip(genres, [[], ["root"], ["bridge"]])
        ]
    }
    reference = ReferenceCatalog(
        {
            "genres": genres,
            "branches": [
                {
                    "id": "branch",
                    "name": "root",
                    "aliases": [],
                    "description": "root",
                    "genre_ids": [g["id"] for g in genres],
                }
            ],
        },
        taxonomy=taxonomy,
    )
    reference.available_ids = {"root", "bridge", "leaf"}
    assert [g["id"] for g in reference.expand(["branch"])] == ["bridge"]
    assert reference.named_genre_id("bridge") == "bridge"
    assert reference.named_genre_id("leaf") == "leaf"


def test_shared_java_python_reference_cases():
    from pathlib import Path

    from auralisy_intelligence.recall.common import weighted_preferences

    root = Path(__file__).parent / "fixtures/genre-reach"
    index = ReachIndex.load(root / "index.json", root / "taxonomy.json")
    for case in json.loads((root / "cases.json").read_text(encoding="utf-8")):
        preferred = weighted_preferences(
            tuple(Preference(**tag) for tag in case["preferences"]["tags"]),
            WeightConfig(**case["policy"]["recall"]["weights"]),
        )
        assert (
            index.proof(case["genres"], preferred, case["policy"]["audience_reach_percent"])
            == case["expected"]
        )


def test_parent_pool_deduplicates_and_supports_familiar_mode(tmp_path):
    snapshot, taxonomy, audience, document = fixture(tmp_path)
    index = ReachIndex(document, json.loads(taxonomy.read_text()))
    profile = index.profiles["navigation"]
    assert profile["direct_album_count"] == 0
    assert profile["pool_album_count"] == profile["positive_album_count"] == 21
    assert profile["mode"] == "NORMAL"
    assert "navigation" in index.selectable
    assert index.reference(["post-rock"], {"navigation": 1}) == "navigation"
    engine = load_a_v2(snapshot, weights=WeightConfig())
    engine.bind_full_audience(FullAudience(audience, document["audience_binding"]))
    engine.bind_reach_index(index)
    request = RecallRequest(limit=100, tags=(Preference("navigation"),), mode="familiar")
    result = engine.recall(request, reach_percent=50)
    assert result.returned_count == profile["bands"][50]["candidate_count"]
    assert result.returned_count >= 10
    counts = engine.audience([c.album_id for c in result.candidates])["counts"]
    band = profile["bands"][50]
    assert all(band["minimum"] <= count <= band["maximum"] for count in counts.values())
