"""Pack and validate the desktop distribution without private recall inputs."""

import hashlib
import json
import re
import secrets
import shutil
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from zipfile import ZIP_STORED, ZipFile

import catalog

FORMAT = {"format": 2, "database": 19, "taxonomy": "app016-v2", "reach": 2}
ARCHIVE_DIGEST = ".archive-sha256"
CURATED_KEYS = (
    "01-no-bad-memories",
    "02-endtroducing",
    "03-u",
    "04-laughing-stock",
    "05-imaginal-disk",
    "06-sometimes-i-might-be-introvert",
    "07-moon-safari",
    "08-cosmogramma",
    "09-voodoo",
    "10-sign-o-the-times",
)
ARTIFACTS = {
    "index/manifest.sha256",
    "curated/format-version.txt",
    "taxonomy/taxonomy.json",
    "taxonomy/reference.json",
    *(
        f"curated/{key}/{name}.json"
        for key in CURATED_KEYS
        for name in ("album", "short-review", "long-review", "sources")
    ),
}
REQUIRED = ARTIFACTS | catalog.CSV_FILES | {"bundle.json", "audience.json"}
HEX = catalog.HEX
SEGMENTS = re.compile(r"segments_[a-z0-9]+")
LUCENE = re.compile(
    r"_[a-z0-9]+(?:_[A-Za-z0-9]+)*\."
    r"(?:cfe|cfs|si|fdm|fdt|fdx|fnm|nvd|nvm|dvd|dvm|doc|pos|psm|pay|tim|tip|tmd|liv|kdd|kdi|kdm)"
)
RECALL_FILES = ("dataset.json", "album_tags.npz", "interactions.npz")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def check_members(members: list[str]) -> None:
    names = set(members)
    if len({name.casefold() for name in members}) != len(members):
        raise ValueError("Bundle contains duplicate file names.")
    if not REQUIRED <= names:
        raise ValueError("Bundle is missing required v2 catalog or artifact files.")
    additional = names - REQUIRED
    segments = 0
    for name in additional:
        if not name.startswith("index/"):
            raise ValueError("Bundle entry outside the desktop file allowlist.")
        filename = name.removeprefix("index/")
        if SEGMENTS.fullmatch(filename):
            segments += 1
        elif not LUCENE.fullmatch(filename):
            raise ValueError("Bundle entry outside the Lucene file allowlist.")
    if segments != 1 or not any(name.endswith(".si") for name in additional):
        raise ValueError("Expected one Lucene commit and its segment files.")


def required_metadata(metadata: dict) -> None:
    if (
        not isinstance(metadata, dict)
        or set(metadata) != FORMAT.keys() | {"audience_digest"}
        or any(metadata.get(key) != value for key, value in FORMAT.items())
        or not isinstance(metadata["audience_digest"], str)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", metadata["audience_digest"])
    ):
        raise ValueError("Expected desktop bundle format 2 (V19). Export a new allowed bundle.")


def export_bundle(args) -> None:
    inputs = {name: getattr(args, name) for name in ("index", "curated", "taxonomy")}
    files = {name: inputs[name.split("/", 1)[0]] / name.split("/", 1)[1] for name in ARTIFACTS}
    files.update(
        {
            f"index/{path.name}": path
            for path in args.index.iterdir()
            if path.is_file() and (SEGMENTS.fullmatch(path.name) or LUCENE.fullmatch(path.name))
        }
    )
    check_members([*files, *catalog.CSV_FILES, "bundle.json", "audience.json"])
    for name, path in files.items():
        root = inputs[name.split("/", 1)[0]].resolve()
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Missing or linked desktop artifact: {name}")
    audience = catalog.read_audience(args.audience)
    metadata = FORMAT | {"audience_digest": audience["digest"]}
    with TemporaryDirectory() as scratch:
        target = Path(scratch) / "catalog"
        catalog.export_csv(args.source, target, audience)
        catalog.check_csv_headers(target)
        files.update({name: target / Path(name).name for name in catalog.CSV_FILES})
        with (
            args.output.open("xb") as output,
            ZipFile(output, "w", compression=ZIP_STORED) as archive,
        ):
            archive.writestr("bundle.json", json.dumps(metadata))
            archive.writestr("audience.json", json.dumps(audience, separators=(",", ":")))
            for name, path in sorted(files.items()):
                archive.write(path, name)
    print(f"SHA-256 {sha256(args.output)}")


def source_hash(source: str, expected: str | None) -> str:
    scheme = urlsplit(source).scheme.lower()
    if scheme in {"http", "https"}:
        if scheme != "https" or not expected or not HEX.fullmatch(expected):
            raise ValueError("Remote bundles require HTTPS and a lowercase --sha256.")
        return expected
    actual = sha256(Path(source))
    if expected is not None and expected != actual:
        raise ValueError("Bundle ZIP SHA-256 mismatch.")
    return actual


def import_bundle(source: str, expected: str | None, state: Path) -> None:
    remote = urlsplit(source).scheme.lower() in {"http", "https"}
    archive_hash = source_hash(source, expected) if remote else expected
    incoming = state / ("bundle.incoming-" + secrets.token_hex(8))
    destination = state / "bundle"
    incoming.mkdir()
    downloaded = incoming.with_suffix(".download")
    try:
        if remote:
            with (
                urllib.request.urlopen(source, timeout=60) as response,
                downloaded.open("xb") as stream,
            ):
                if urlsplit(response.url).scheme != "https":
                    raise ValueError("Bundle download redirected away from HTTPS.")
                shutil.copyfileobj(response, stream)
            archive_path = downloaded
        else:
            archive_path = Path(source)
        archive_hash = unpack(archive_path, archive_hash, incoming)
        (incoming / ARCHIVE_DIGEST).write_text(archive_hash, encoding="ascii")
        incoming.rename(destination)
    finally:
        downloaded.unlink(missing_ok=True)
        if incoming.exists():
            shutil.rmtree(incoming)


def unpack(archive_path: Path, expected: str | None, destination: Path) -> str:
    with archive_path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if expected is not None and actual != expected:
            raise ValueError("Bundle ZIP SHA-256 mismatch.")
        with ZipFile(stream) as archive:
            if "bundle.json" not in archive.namelist():
                raise ValueError("Missing v2 desktop bundle metadata.")
            required_metadata(json.loads(archive.read("bundle.json")))
            members = archive.namelist()
            check_members(members)
            for name in members:
                path = destination / name
                path.parent.mkdir(exist_ok=True, parents=True)
                with archive.open(name) as source, path.open("wb") as output:
                    shutil.copyfileobj(source, output)
    catalog.check_csv_headers(destination / "catalog")
    audience = catalog.read_audience(destination / "audience.json")
    if audience["digest"] != read(destination / "bundle.json")["audience_digest"]:
        raise ValueError("Bundle and aggregate audience digests differ.")
    return actual
