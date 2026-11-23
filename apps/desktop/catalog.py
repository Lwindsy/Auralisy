"""Export and restore only the active V19 catalog fields used by the desktop."""

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

HEX = re.compile(r"[a-f0-9]{64}")
SERVICE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")

# Columns needed only by the existing V19 constraints use local constants. Only
# the active dataset is copied, and repeated source tag rows collapse to one
# album/genre edge. The order also satisfies the target foreign keys.
COLUMNS = {
    "genre": "dataset_id, id, mbid, name",
    "artist": "dataset_id, id, mbid, name",
    "album": "dataset_id, id, mbid, title, comment, primary_type, first_year",
    "album_secondary_type": "dataset_id, album_id, name",
    "album_credit": "dataset_id, album_id, position, artist_id, credited_name, join_phrase",
    "album_genre": "dataset_id, album_id, genre_id",
    "album_redirect": "dataset_id, old_mbid, album_id",
}
FILL = {
    "genre": ("comment", "''"),
    "artist": ("sort_name, comment", "name, ''"),
    "album": ("source_credit_id", "0"),
    "album_genre": ("source_tag_id, support_count", "0, 1"),
}
CSV_FILES = {f"catalog/{table}.csv" for table in COLUMNS}
LAYOUT_HASH = hashlib.sha256(json.dumps(COLUMNS, sort_keys=True).encode("ascii")).hexdigest()
AUDIENCE_COLUMNS = "release_group_id, listeners"


def read_audience(path: Path) -> dict:
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate key in audience artifact.")
        return result

    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    if not isinstance(value, dict) or set(value) != {
        "format_version",
        "scope",
        "source",
        "catalog_dataset_id",
        "source_manifest_sha256",
        "dataset_id",
        "window",
        "sample_listeners",
        "counts",
        "digest",
    }:
        raise ValueError("Expected an aggregate audience artifact with the standard fields.")
    manifest = value["source_manifest_sha256"]
    sample = value["sample_listeners"]
    counts = value["counts"]
    if (
        type(value["format_version"]) is not int
        or value["format_version"] != 2
        or value["scope"] != "FULL_CATALOG"
        or value["source"] != "LISTENBRAINZ_SAMPLE"
        or type(value["catalog_dataset_id"]) is not int
        or not 1 <= value["catalog_dataset_id"] <= 9_007_199_254_740_991
        or not isinstance(manifest, str)
        or not HEX.fullmatch(manifest)
        or value["dataset_id"] != f"core:{manifest}"
        or not isinstance(value["window"], str)
        or not re.fullmatch(r"0|[0-9]{4}", value["window"])
        or type(sample) is not int
        or not 1 <= sample <= 9_007_199_254_740_991
        or not isinstance(counts, list)
        or not counts
    ):
        raise ValueError("Invalid aggregate audience binding.")

    previous = ""
    for row in counts:
        if not isinstance(row, dict) or set(row) != {"release_group_id", "listeners"}:
            raise ValueError("Audience rows may contain only an album ID and an aggregate count.")
        identifier, listeners = row["release_group_id"], row["listeners"]
        if (
            not isinstance(identifier, str)
            or str(UUID(identifier)) != identifier
            or identifier <= previous
            or type(listeners) is not int
            or not 0 <= listeners <= sample
        ):
            raise ValueError("Invalid or unordered aggregate audience row.")
        previous = identifier

    # The validated artifact uses ASCII keys/strings and safe-range integers, so
    # sorted compact JSON has the same representation as the RFC 8785 contract.
    body = {key: field for key, field in value.items() if key != "digest"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("ascii")
    if value["digest"] != "sha256:" + hashlib.sha256(encoded).hexdigest():
        raise ValueError("Audience digest mismatch.")
    return value


def source_sql(audience: dict) -> str:
    dataset = audience["catalog_dataset_id"]
    manifest = audience["source_manifest_sha256"]
    digest = audience["digest"]
    lines = [
        f"""
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
DO $desktop$
BEGIN
 IF current_setting('server_version_num')::int / 10000 <> 18 OR
    (SELECT max(version::int) FROM public.flyway_schema_history WHERE success) IS DISTINCT FROM 19
    OR NOT EXISTS (
        SELECT FROM ingestion.active_dataset a JOIN ingestion.dataset d ON d.id = a.dataset_id
        JOIN discovery.audience_snapshot s ON s.snapshot_fingerprint = d.manifest_sha256
        WHERE a.dataset_id = {dataset} AND d.status = 'ACTIVE' AND d.manifest_sha256 = '{manifest}'
          AND s.id = '{digest}' AND s.active AND s.manifest_digest = '{digest}'
          AND s.recall_binding->>'scope' = 'FULL_CATALOG'
          AND s.recall_binding->>'catalog_dataset_id' = '{dataset}'
    ) THEN RAISE EXCEPTION 'Desktop export requires the matching active V19 catalog and audience';
 END IF;
END
$desktop$;
"""
    ]
    for table, columns in COLUMNS.items():
        distinct = "DISTINCT " if table == "album_genre" else ""
        selection = f"SELECT {distinct}{columns} FROM catalog.{table} WHERE dataset_id = {dataset}"
        if table == "artist":
            selection += (
                " AND EXISTS (SELECT FROM catalog.album_credit c"
                " WHERE c.dataset_id = artist.dataset_id AND c.artist_id = artist.id)"
            )
        lines.append(rf"\copy ({selection}) TO '{table}.csv' WITH (FORMAT csv, HEADER true)")
    return "\n".join([*lines, "COMMIT;", ""])


def require_service_name(source: str) -> str:
    if not SERVICE_NAME.fullmatch(source):
        raise ValueError("Expected a libpq service name such as auralis_desktop_export.")
    return source


def export_csv(source: str, target: Path, audience: dict) -> None:
    service = require_service_name(source)
    psql = shutil.which("psql")
    if not psql:
        raise ValueError("Export requires PostgreSQL 18 psql on PATH.")
    target.mkdir()
    env = {**os.environ, "PGCLIENTENCODING": "UTF8", "PGSERVICE": service}
    env.pop("PGPASSWORD", None)
    subprocess.run(
        [psql, "-Xq", "-w", "-v", "ON_ERROR_STOP=1"],
        input=source_sql(audience),
        env=env,
        encoding="utf-8",
        cwd=target,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def check_csv_headers(root: Path) -> None:
    for table, columns in COLUMNS.items():
        with (root / f"{table}.csv").open(encoding="utf-8", newline="") as stream:
            if next(csv.reader(stream), None) != columns.split(", "):
                raise ValueError(f"Unexpected CSV header for catalog.{table}.")


def restore_sql(data: Path, audience: dict) -> str:
    dataset = audience["catalog_dataset_id"]
    manifest = audience["source_manifest_sha256"]
    digest = audience["digest"]
    window = audience["window"]
    sample = audience["sample_listeners"]
    # The file is generated from the verified aggregate JSON for this install.
    # Only the typed CSV files enter PostgreSQL, never SQL from the ZIP.
    with (data / "audience-counts.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(AUDIENCE_COLUMNS.split(", "))
        writer.writerows((row["release_group_id"], row["listeners"]) for row in audience["counts"])

    lines = [
        f"""
BEGIN;
INSERT INTO ingestion.dataset(id, manifest_sha256, layout_sha256, purpose, manifest, status)
OVERRIDING SYSTEM VALUE
VALUES ({dataset}, '{manifest}', '{LAYOUT_HASH}', 'private-development',
        '{{"projection":"desktop-v2"}}', 'IMPORTING');
"""
    ]
    for table, columns in COLUMNS.items():
        target = f"catalog.{table}"
        if table in FILL:
            target = f"desktop_{table}"
            lines.append(
                f"CREATE TEMP TABLE {target} ON COMMIT DROP AS "
                f"SELECT {columns} FROM catalog.{table} WITH NO DATA;"
            )
        lines.append(
            rf"\copy {target} ({columns}) FROM '/bundle/catalog/{table}.csv'"
            " WITH (FORMAT csv, HEADER MATCH)"
        )
        if table in FILL:
            missing, values = FILL[table]
            lines.append(
                f"INSERT INTO catalog.{table} ({columns}, {missing}) "
                f"SELECT {columns}, {values} FROM {target};"
            )
    lines.append(rf"""
CREATE TEMP TABLE desktop_audience (release_group_id uuid PRIMARY KEY, listeners bigint NOT NULL);
\copy desktop_audience ({AUDIENCE_COLUMNS}) FROM '/bundle/audience-counts.csv' WITH (FORMAT csv, HEADER MATCH)
DO $desktop$
BEGIN
 IF EXISTS (SELECT mbid FROM catalog.album WHERE dataset_id = {dataset}
            EXCEPT SELECT release_group_id FROM desktop_audience)
 OR EXISTS (SELECT release_group_id FROM desktop_audience
            EXCEPT SELECT mbid FROM catalog.album WHERE dataset_id = {dataset})
 THEN RAISE EXCEPTION 'Desktop catalog and audience album IDs differ'; END IF;
END
$desktop$;
INSERT INTO discovery.audience_snapshot
(id, dataset_id, listening_window, sample_listeners, manifest_digest, snapshot_fingerprint,
 recall_binding, active)
VALUES ('{digest}', 'core:{manifest}', '{window}', {sample}, '{digest}', '{manifest}',
        jsonb_build_object('scope', 'FULL_CATALOG', 'catalog_dataset_id', {dataset},
                           'source_manifest_sha256', '{manifest}'), true);
INSERT INTO discovery.audience_count
(snapshot_id, listening_window, sample_listeners, release_group_id, listeners)
SELECT '{digest}', '{window}', {sample}, release_group_id, listeners FROM desktop_audience;
UPDATE ingestion.dataset SET status = 'ACTIVE' WHERE id = {dataset};
INSERT INTO ingestion.active_dataset(singleton, dataset_id) VALUES (true, {dataset});
COMMIT;
""")
    return "\n".join(lines)
