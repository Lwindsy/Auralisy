"""Small generated catalog for the desktop smoke test, with no licensed data."""

import hashlib
import json
from pathlib import Path
from uuid import UUID

from bundle import write_json

GENRE = str(UUID(int=101))
ALBUMS = [str(UUID(int=number)) for number in range(1, 25)]
MANIFEST = "a" * 64
DATASET = "core:" + MANIFEST


def audience() -> dict:
    document = {
        "format_version": 2,
        "scope": "FULL_CATALOG",
        "source": "LISTENBRAINZ_SAMPLE",
        "catalog_dataset_id": 1,
        "source_manifest_sha256": MANIFEST,
        "dataset_id": DATASET,
        "window": "0",
        "sample_listeners": 100,
        "counts": [{"release_group_id": album, "listeners": 60} for album in ALBUMS],
    }
    # All keys and values here are ASCII strings, integers or containers of those types.
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    document["digest"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return document


def create_bundle(path: Path) -> None:
    pending = path.with_name("synthetic.incoming")
    taxonomy = pending / "taxonomy"
    taxonomy.mkdir(parents=True, exist_ok=True)
    node = {"id": GENRE, "name": "Jazz", "aliases": []}
    write_json(
        taxonomy / "taxonomy.json",
        {
            "format_version": 1,
            "source_sha256": "0" * 64,
            "core_manifest_sha256": MANIFEST,
            "genres": [node | {"parents": []}],
        },
    )
    revision = hashlib.sha256((taxonomy / "taxonomy.json").read_bytes()).hexdigest()
    write_json(
        taxonomy / "reference.json",
        {
            "taxonomy_sha256": revision,
            "genres": [node | {"description": "Jazz"}],
            "branches": [node | {"id": "branch-jazz", "description": "Jazz", "genre_ids": [GENRE]}],
        },
    )
    write_json(
        taxonomy / "snapshot.json",
        {
            "format_version": 1,
            "dataset_id": "synthetic",
            "window": "0",
            "album_ids": ALBUMS,
            "tag_ids": [GENRE],
            "tag_parents": [],
            "album_tags": [[1]] * len(ALBUMS),
            "interactions": [
                [1 + (index + user) % 4 for index in range(len(ALBUMS))] for user in range(6)
            ],
            "provenance": {"taxonomy_sha256": revision},
        },
    )
    counts = audience()
    write_json(pending / "audience.json", counts)
    write_json(pending / "bundle.json", {"audience_digest": counts["digest"]})
    pending.rename(path)


def seed_sql() -> str:
    digest = audience()["digest"]
    statements = [
        "BEGIN;",
        "INSERT INTO ingestion.dataset(id,manifest_sha256,layout_sha256,purpose,manifest,status)"
        f" OVERRIDING SYSTEM VALUE VALUES (1,'{MANIFEST}','{'b' * 64}',"
        "'private-development','{}','IMPORTING');",
        "INSERT INTO ingestion.active_dataset VALUES(true,1);",
        f"INSERT INTO catalog.genre VALUES(1,1,'{GENRE}','Jazz','');",
        "INSERT INTO discovery.audience_snapshot VALUES"
        f"('{digest}','{DATASET}','0',100,'{digest}','{MANIFEST}',"
        """'{"scope":"FULL_CATALOG","catalog_dataset_id":1}',true);""",
    ]
    for index, album in enumerate(ALBUMS, 1):
        artist = f"Synthetic Artist {index}"
        statements.extend(
            [
                f"INSERT INTO catalog.artist VALUES(1,{index},'{UUID(int=200 + index)}',"
                f"'{artist}','{artist}','',NULL,now());",
                "INSERT INTO catalog.album(dataset_id,id,mbid,title,comment,source_credit_id,"
                f"primary_type,source_updated) VALUES(1,{index},'{album}',"
                f"'Synthetic Album {index}','',1,'Album',now());",
                f"INSERT INTO catalog.album_credit VALUES(1,{index},0,{index},'{artist}','');",
                f"INSERT INTO catalog.album_genre VALUES(1,{index},1,1,1,now());",
                f"INSERT INTO discovery.audience_count VALUES('{digest}','0',100,'{album}',60);",
            ]
        )
    statements.extend(["UPDATE ingestion.dataset SET status='ACTIVE' WHERE id=1;", "COMMIT;"])
    return "\n".join(statements)
