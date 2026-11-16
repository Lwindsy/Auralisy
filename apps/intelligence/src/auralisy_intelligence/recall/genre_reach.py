"""Deployment-time reach bands and shared, request-specific reference selection."""

import argparse
import json
import math
import tempfile
from pathlib import Path

import numpy as np
from auralisy_intelligence.contracts import content_digest, require_binding, validate_definition
from auralisy_intelligence.genre_taxonomy import load_taxonomy, taxonomy_hash

from .common import dataset_fingerprint, weighted_preferences
from .full_audience import FullAudience
from .io import load_dataset


def make_bands(values, low, head, minimum_candidates=10):
    """Quantize outward, then grow whole count groups by log distance from the interval."""
    available = np.sort(np.asarray(values, dtype=np.int64))
    available = available[available >= low]
    distinct, frequencies = np.unique(available, return_counts=True)
    target = min(minimum_candidates, len(available))
    bands = []
    for position in range(101):
        left = low * (head / low) ** (max(0, position - 10) / 100)
        right = low * (head / low) ** (min(100, position + 10) / 100)
        lower = max(low, math.floor(left + 1e-10))
        upper = math.ceil(right - 1e-10) if position < 90 else None
        included = (distinct >= lower) & ((distinct <= upper) if upper is not None else True)
        count = int(frequencies[included].sum())
        if count < target:
            distances = np.where(
                distinct < lower,
                np.log(left / distinct),
                np.log(distinct / right) if upper is not None else 0.0,
            )
            distances[included] = -1
            for distance in sorted(set(distances[~included])):
                group = (~included) & np.isclose(distances, distance, rtol=0, atol=1e-12)
                included |= group
                count += int(frequencies[group].sum())
                if count >= target:
                    break
            selected = distinct[included]
            lower = min(lower, int(selected.min()))
            if upper is not None:
                upper = max(upper, int(selected.max()))
        bands.append({"minimum": lower, "maximum": upper, "candidate_count": count})
    return bands


def build_index(snapshot: Path, taxonomy_path: Path, audience_path: Path) -> dict:
    data = load_dataset(snapshot)
    taxonomy = load_taxonomy(taxonomy_path)
    audience_document = json.loads(audience_path.read_text(encoding="utf-8"))
    binding = {key: audience_document[key] for key in ("dataset_id", "window", "sample_listeners")}
    binding["snapshot_digest"] = audience_document["digest"]
    audience = FullAudience(audience_path, binding)
    revision = taxonomy_hash(taxonomy_path)
    raw = json.loads(
        (snapshot / "dataset.json" if snapshot.is_dir() else snapshot).read_text(encoding="utf-8")
    )
    if (
        raw.get("provenance", {}).get("taxonomy_sha256") != revision
        or taxonomy["core_manifest_sha256"] != audience_document["source_manifest_sha256"]
        or set(data.tag_ids) != {g["id"] for g in taxonomy["genres"]}
        or data.window != audience_document["window"]
        or set(data.album_ids) != set(audience.counts)
    ):
        raise ValueError("REACH_SOURCE_MISMATCH")
    matrix = data.album_tags.tocsc()
    counts = np.array([audience.counts[i] for i in data.album_ids])
    positions = {genre: i for i, genre in enumerate(data.tag_ids)}
    children = {genre: [] for genre in data.tag_ids}
    for node in taxonomy["genres"]:
        for parent in node["parents"]:
            children[parent].append(node["id"])
    pools = {}

    def pool(genre):
        if genre not in pools:
            column = positions[genre]
            direct = matrix.indices[matrix.indptr[column] : matrix.indptr[column + 1]]
            pools[genre] = np.unique(
                np.concatenate([direct, *(pool(child) for child in children[genre])])
            )
        return pools[genre]

    profiles = []
    for index, genre in enumerate(data.tag_ids):
        direct_count = int(matrix.indptr[index + 1] - matrix.indptr[index])
        members = pool(genre)
        values = counts[members]
        positive = np.sort(values[values > 0])
        head = (
            float(np.median(positive[-max(3, math.ceil(len(positive) / 100)) :]))
            if len(positive)
            else None
        )
        low = max(1, min(50, math.ceil(head / 10))) if head is not None else None
        mode = (
            "HIDDEN"
            if not len(values)
            else "DIRECT"
            if len(positive) < 3 or head <= low
            else "NORMAL"
        )
        profiles.append(
            {
                "genre_id": genre,
                "mode": mode,
                "direct_album_count": direct_count,
                "pool_album_count": len(values),
                "positive_album_count": len(positive),
                "lower": low if mode == "NORMAL" else None,
                "head": head if mode == "NORMAL" else None,
                "bands": make_bands(values, low, head) if mode == "NORMAL" else [],
            }
        )
    document = {
        "format_version": 2,
        "algorithm": "genre-reach.v2",
        "catalog_dataset_id": audience_document["catalog_dataset_id"],
        "source_manifest_sha256": taxonomy["core_manifest_sha256"],
        "taxonomy_sha256": revision,
        "recall_snapshot_id": data.dataset_id,
        "recall_fingerprint": dataset_fingerprint(data),
        "audience_binding": binding,
        "minimum_candidates": 10,
        "genres": sorted(profiles, key=lambda g: g["genre_id"]),
    }
    document["digest"] = content_digest(document, "digest")
    validate_definition("GenreReachIndex", document)
    return document


class ReachIndex:
    def __init__(self, document: dict, taxonomy: dict):
        validate_definition("GenreReachIndex", document)
        if document["digest"] != content_digest(document, "digest"):
            raise ValueError("REACH_DIGEST_MISMATCH")
        self.document = document
        self.digest = document["digest"]
        self.profiles = {p["genre_id"]: p for p in document["genres"]}
        nodes = {g["id"]: g for g in taxonomy["genres"]}
        if len(self.profiles) != len(document["genres"]) or self.profiles.keys() != nodes.keys():
            raise ValueError("REACH_GENRES_MISMATCH")
        self.ancestors = {}
        self.parents = {parent for node in taxonomy["genres"] for parent in node["parents"]}

        def visit(identifier):
            if identifier not in self.ancestors:
                parents = set(nodes[identifier]["parents"])
                self.ancestors[identifier] = parents | set().union(*(visit(p) for p in parents))
            return self.ancestors[identifier]

        for identifier in nodes:
            visit(identifier)
        for profile in self.profiles.values():
            if len(profile["bands"]) != (101 if profile["mode"] == "NORMAL" else 0):
                raise ValueError("REACH_BANDS_INVALID")
        self.selectable = {g for g, p in self.profiles.items() if p["mode"] != "HIDDEN"}

    @classmethod
    def load(cls, path: Path, taxonomy_path: Path):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document["taxonomy_sha256"] != taxonomy_hash(taxonomy_path):
            raise ValueError("REACH_TAXONOMY_MISMATCH")
        return cls(document, load_taxonomy(taxonomy_path))

    def specific(self, genres):
        found = set(genres) & self.selectable
        return found - set().union(*(self.ancestors[g] for g in found))

    def priority(self, genre):
        p = self.profiles[genre]
        return (p["mode"] != "NORMAL", -(p["lower"] or 0), -(p["head"] or 0), genre)

    def reference(self, album_genres, preferred):
        tags = set(album_genres)
        matched = {
            g
            for g in preferred
            if g in self.selectable and any(g == t or g in self.ancestors.get(t, ()) for t in tags)
        }
        choices = self.specific(matched) if matched else self.specific(tags)
        return (
            min(choices, key=lambda g: (-preferred.get(g, 0), *self.priority(g)))
            if choices
            else None
        )

    def proof(self, album_genres, preferred, position):
        if type(position) is not int or not 0 <= position <= 100:
            raise ValueError("REACH_POSITION_INVALID")
        genre = self.reference(album_genres, preferred)
        if genre is None:
            return None
        profile = self.profiles[genre]
        band = (
            profile["bands"][position]
            if profile["mode"] == "NORMAL"
            else {"minimum": 0, "maximum": None}
        )
        return {
            "index_digest": self.digest,
            "genre_id": genre,
            "mode": profile["mode"],
            "requested_percent": position,
            "minimum": band["minimum"],
            "maximum": band["maximum"],
        }


class ReachFilter:
    def __init__(self, index, data, audience):
        self.index, self.data = index, data
        require_binding(index.document["audience_binding"], audience.binding_copy())
        if index.document["recall_snapshot_id"] != data.dataset_id or index.document[
            "recall_fingerprint"
        ] != dataset_fingerprint(data):
            raise ValueError("REACH_RECALL_MISMATCH")
        self.counts = np.array([audience.counts.get(a, -1) for a in data.album_ids])
        self.matrix = data.album_tags.tocsc()
        self.genre_positions = {g: i for i, g in enumerate(data.tag_ids)}
        self.album_positions = {a: i for i, a in enumerate(data.album_ids)}
        self.own = np.full(len(data.album_ids), -1, dtype=np.int32)
        direct = data.album_tags.tocsr()
        for row in range(len(self.own)):
            tags = [
                data.tag_ids[i] for i in direct.indices[direct.indptr[row] : direct.indptr[row + 1]]
            ]
            genre = index.reference(tags, {})
            if genre is not None:
                self.own[row] = self.genre_positions[genre]

    def members(self, genre):
        columns = [
            i
            for i, g in enumerate(self.data.tag_ids)
            if g == genre or genre in self.index.ancestors[g]
        ]
        return (
            np.unique(
                np.concatenate(
                    [
                        self.matrix.indices[self.matrix.indptr[i] : self.matrix.indptr[i + 1]]
                        for i in columns
                    ]
                )
            )
            if columns
            else np.array([], dtype=int)
        )

    def evaluate(self, request, position, weights):
        preferred = weighted_preferences(request.tags, weights)
        matched = {g: self.members(g) for g in preferred if g in self.index.selectable}
        choices = self.own.copy()
        assigned = np.zeros(len(choices), dtype=bool)
        for genre in sorted(matched, key=lambda g: (-preferred[g], *self.index.priority(g))):
            members = matched[genre]
            more_specific = [
                v for other, v in matched.items() if genre in self.index.ancestors[other]
            ]
            if more_specific:
                members = np.setdiff1d(members, np.concatenate(more_specific), assume_unique=False)
            members = members[~assigned[members]]
            choices[members] = self.genre_positions[genre]
            assigned[members] = True
        lower = np.zeros(len(self.data.tag_ids), dtype=np.int64)
        upper = np.full(len(lower), np.iinfo(np.int64).max, dtype=np.int64)
        direct = np.zeros(len(lower), dtype=bool)
        for i, genre in enumerate(self.data.tag_ids):
            profile = self.index.profiles[genre]
            direct[i] = profile["mode"] == "DIRECT"
            if profile["mode"] == "NORMAL":
                band = profile["bands"][position]
                lower[i] = band["minimum"]
                if band["maximum"] is not None:
                    upper[i] = band["maximum"]
        mask = (
            (choices >= 0)
            & (self.counts >= 0)
            & (self.counts >= lower[choices])
            & (self.counts <= upper[choices])
        )
        # Zero-audience direct pools need a genre route even when co-listening has no score.
        origins = set(preferred)
        for seed in request.seeds:
            row = self.data.album_tags.getrow(self.album_positions[seed.id])
            origins.update(self.data.tag_ids[i] for i in row.indices)
        fallback = np.zeros(len(choices))
        fallback_allowed = direct[choices].copy()
        for genre in origins & self.index.selectable:
            members = self.members(genre)
            fallback[members] += preferred.get(genre, 1.0)
            if genre in preferred and genre in self.index.parents:
                # A selected parent covers descendants even in familiar mode, where the
                # original scorer may have no direct parent-label signal.
                fallback_allowed[members] = True
        fallback[~mask | ~fallback_allowed] = 0
        return mask, fallback


def within_policy(audience: dict | None, policy: dict) -> bool:
    if not audience or audience.get("listeners") is None:
        return False
    if "audience_reach_percent" in policy:
        proof = audience.get("reach")
        return bool(
            proof
            and proof["index_digest"] == policy.get("audience_reach_index_digest")
            and proof["requested_percent"] == policy["audience_reach_percent"]
            and audience["listeners"] >= proof["minimum"]
            and (proof["maximum"] is None or audience["listeners"] <= proof["maximum"])
        )
    return (
        "audience_min_listeners" in policy
        and audience["listeners"] >= policy["audience_min_listeners"]
        and audience["listeners"] <= policy.get("audience_max_listeners", float("inf"))
    )


def prepare_from_config(config_path: Path, output: Path, *, replace=False):
    config = json.loads(config_path.read_text(encoding="utf-8"))

    def relative(key):
        return (config_path.parent / config[key]).resolve()

    document = build_index(
        relative("recall_snapshot"), relative("genre_taxonomy"), relative("audience_snapshot")
    )
    write_index(document, output, replace=replace)
    return document


def write_index(document, output: Path, *, replace=False):
    text = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    if replace:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=output.name + ".",
            suffix=".preparing",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(text)
            except BaseException:
                stream.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        with output.open("x", encoding="utf-8") as stream:
            stream.write(text)


def main():
    parser = argparse.ArgumentParser(
        description="Prepare an immutable genre reach index before deployment"
    )
    parser.add_argument("--config", type=Path)
    for key in ("snapshot", "taxonomy", "audience"):
        parser.add_argument("--" + key, type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    if args.config:
        document = prepare_from_config(args.config.resolve(), args.output, replace=args.replace)
    else:
        if not all((args.snapshot, args.taxonomy, args.audience)):
            parser.error("supply --config or --snapshot, --taxonomy and --audience")
        document = build_index(args.snapshot, args.taxonomy, args.audience)
        write_index(document, args.output, replace=args.replace)
    print(json.dumps({"digest": document["digest"], "genres": len(document["genres"])}))


if __name__ == "__main__":
    main()
