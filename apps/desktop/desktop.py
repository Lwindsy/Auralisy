"""Start, stop, and inspect an isolated Auralisy desktop Compose project."""

import argparse
import contextlib
import getpass
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from zipfile import BadZipFile

import bundle
import catalog
import synthetic

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
KEY = "AURALIS_PRODUCT_TEST_API_KEY"
CREDENTIALS = (
    "POSTGRES_PASSWORD",
    "MIGRATOR_PASSWORD",
    "ONLINE_PASSWORD",
    "SERVICE_TOKEN",
    "SUBJECT_SECRET",
)
DB = "auralis_core"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def state_path(path: Path) -> Path:
    lexical = Path(os.path.abspath(path.expanduser()))
    state = path.expanduser().resolve()
    for location in (lexical, state):
        safe_local = (
            location.is_relative_to(HERE / ".local")
            or location != ROOT / "local-data"
            and location.is_relative_to(ROOT / "local-data")
        )
        in_checkout = location.is_relative_to(ROOT)
        foreign_checkout = any(
            (parent / ".git").exists() and not (in_checkout and ROOT.is_relative_to(parent))
            for parent in (location, *location.parents)
        )
        if (in_checkout and not safe_local) or foreign_checkout:
            raise ValueError(
                "Choose --state outside Git checkouts or under this checkout's "
                "apps/desktop/.local or local-data."
            )
    if state.is_relative_to(ROOT) and (ROOT / ".git").exists():
        relative = state.relative_to(ROOT).as_posix()
        git = ["git", "-C", str(ROOT)]
        env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
        paths = [f"{relative}/"] + [
            f"{relative}/{name}"
            for name in ("settings.json", ".settings-check.pending", "launcher.lock")
        ]
        ignored = subprocess.run(
            [*git, "check-ignore", "--stdin", "-z"],
            input="\0".join(paths) + "\0",
            capture_output=True,
            encoding="utf-8",
            env=env,
        )
        tracked = subprocess.check_output([*git, "ls-files", "-z", "--", relative], env=env)
        if ignored.returncode or set(ignored.stdout.split("\0")) - {""} != set(paths) or tracked:
            raise ValueError("The selected desktop state directory must be ignored and untracked.")
    return state


def port_problem(port: int) -> str | None:
    if not 1 <= port <= 65535:
        raise ValueError("Port must be from 1 to 65535.")
    with socket.socket() as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return (
                f"Cannot bind 127.0.0.1:{port}. Rerun the same start command with "
                "--port <free-port> and keep the same --state and bundle."
            )
    return None


@contextlib.contextmanager
def lock_state(state: Path):
    with (state / "launcher.lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError(
                "Another desktop launcher command is active for this state."
            ) from error
        yield


class Desktop:
    def __init__(self, state: Path, settings: dict):
        self.state = state
        self.settings = settings
        project_hash = hashlib.sha256(os.path.normcase(str(state)).encode()).hexdigest()[:16]
        self.project = f"auralis-wallpaper-{project_hash}"
        port = settings["port"]
        self.origin = "http://127.0.0.1" if port == 80 else f"http://127.0.0.1:{port}"
        self.data = state / "bundle"
        self.config = state / "config"
        self.synthetic = settings["synthetic"]
        self.env = {key: value for key, value in os.environ.items() if key != KEY}
        self.env.update({f"DESKTOP_{key}": value for key, value in settings["credentials"].items()})
        self.env.update(
            DESKTOP_PORT=str(settings["port"]),
            DESKTOP_ORIGIN=self.origin,
            DESKTOP_SESSION_MICROUSD=str(settings["session_microusd"]),
            DESKTOP_ROUND_MICROUSD=str(settings["round_microusd"]),
            DESKTOP_BUNDLE=self.data.as_posix(),
            DESKTOP_CONFIG=self.config.as_posix(),
            DESKTOP_RECALL=Path(settings.get("recall_snapshot") or self.data).as_posix(),
            DESKTOP_SYNTHETIC="1" if self.synthetic else "0",
            DESKTOP_SEARCH_INDEX="",
            DESKTOP_SEARCH_MANIFEST="",
            DESKTOP_CURATED="",
        )
        self.env["DESKTOP_PROVIDER_KEY"] = ""

    def compose(
        self, *args: str, capture: bool = False, data: str | None = None, key: str = ""
    ) -> str:
        command = ["docker", "compose", "-p", self.project, "-f", str(HERE / "compose.yml"), *args]
        env = {**self.env, "DESKTOP_PROVIDER_KEY": key} if key else self.env
        return (
            subprocess.run(
                command,
                env=env,
                input=data,
                encoding="utf-8",
                stdout=subprocess.PIPE if capture else None,
                check=True,
            ).stdout
            or ""
        )

    def sql(self, query: str, database: str = DB) -> str:
        return self.compose(
            "exec",
            "-T",
            "database",
            "psql",
            "-XqAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            database,
            capture=True,
            data=query + "\n",
        ).strip()

    def prepare_database(self) -> None:
        passwords = self.settings["credentials"]
        self.sql(
            rf"""
SELECT 'CREATE ROLE auralis_migrator LOGIN PASSWORD ''{passwords["MIGRATOR_PASSWORD"]}'''
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='auralis_migrator') \gexec
SELECT 'CREATE ROLE auralis_online LOGIN PASSWORD ''{passwords["ONLINE_PASSWORD"]}'''
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='auralis_online') \gexec
SELECT 'CREATE DATABASE {DB} OWNER auralis_migrator'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='{DB}') \gexec
REVOKE ALL ON DATABASE {DB} FROM PUBLIC;
GRANT CONNECT ON DATABASE {DB} TO auralis_online;
""",
            "postgres",
        )
        self.sql("REVOKE CREATE ON SCHEMA public FROM PUBLIC;")
        self.compose("run", "--rm", "--no-deps", "migrate")
        if self.sql("SELECT count(*) FROM ingestion.dataset;") == "0":
            if self.synthetic:
                self.sql(synthetic.seed_sql())
            else:
                audience = catalog.read_audience(self.data / "audience.json")
                try:
                    self.sql(catalog.restore_sql(self.data, audience))
                finally:
                    (self.data / "audience-counts.csv").unlink(missing_ok=True)
            self.sql("ANALYZE;")
        audience = bundle.read(self.data / "bundle.json")["audience_digest"]
        if not re.fullmatch("sha256:[a-f0-9]{64}", audience):
            raise ValueError("Invalid audience binding.")
        actual = self.sql("""
SELECT s.manifest_digest FROM discovery.audience_snapshot s
JOIN ingestion.active_dataset a ON (s.recall_binding->>'catalog_dataset_id')::bigint=a.dataset_id
JOIN ingestion.dataset d ON d.id=a.dataset_id AND d.manifest_sha256=s.snapshot_fingerprint
WHERE s.active AND s.recall_binding->>'scope'='FULL_CATALOG';
""")
        if actual != audience:
            raise ValueError("The database, catalog and audience artifact are not the same bundle.")
        if (
            self.sql(
                "SELECT max(version::integer) FROM public.flyway_schema_history WHERE success;"
            )
            != "19"
        ):
            raise ValueError("Expected database migration V19.")
        self.sql("""
GRANT USAGE ON SCHEMA catalog,ingestion,statistics,discovery TO auralis_online;
GRANT SELECT ON ALL TABLES IN SCHEMA catalog,ingestion,statistics TO auralis_online;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA discovery TO auralis_online;
GRANT SELECT ON public.flyway_schema_history TO auralis_online;
REVOKE INSERT,UPDATE,DELETE ON discovery.audience_snapshot,discovery.audience_count FROM auralis_online;
""")

    def ready(self) -> bool:
        try:
            with HTTP.open(self.origin + "/desktop/ready", timeout=3) as response:
                return json.load(response).get("status") == "UP"
        except (OSError, ValueError):
            return False

    def start(self, key: str) -> None:
        if not self.synthetic:
            manifest = (self.data / "index/manifest.sha256").read_text(encoding="utf-8").strip()
            if not bundle.HEX.fullmatch(manifest):
                raise ValueError("Invalid Lucene manifest SHA-256.")
            self.env.update(
                DESKTOP_SEARCH_INDEX="/bundle/index",
                DESKTOP_SEARCH_MANIFEST=manifest,
                DESKTOP_CURATED="/bundle/curated",
            )
        self.compose("config", "--quiet")
        self.compose("build")
        try:
            self.compose("run", "--rm", "--no-deps", "prepare")
            self.compose("up", "-d", "--wait", "database")
            self.prepare_database()
            self.compose("up", "-d", "--no-build", "python", "java", "web", key=key)
            for _ in range(120):
                if self.ready():
                    print(f"Ready: {self.origin}/wallpaper.html")
                    return
                time.sleep(1)
            raise ValueError("Desktop services did not report readiness.")
        except (Exception, KeyboardInterrupt) as error:
            try:
                self.compose("logs", "--tail", "35", "java", "python", "web")
            finally:
                self.compose("down", "--remove-orphans")
            if isinstance(error, subprocess.CalledProcessError):
                problem = port_problem(self.settings["port"])
                if problem:
                    raise ValueError(problem) from error
            raise


def save_settings(path: Path, settings: dict) -> None:
    pending = path.with_name(f".settings-{secrets.token_hex(8)}.pending")
    descriptor = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(settings, stream, indent=2)
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def parse_service_name(value: str) -> str:
    try:
        return catalog.require_service_name(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    actions = command.add_subparsers(dest="action", required=True)
    for name in ("start", "stop", "status"):
        action = actions.add_parser(name)
        action.add_argument("--state", type=Path, default=HERE / ".local")
    start = actions.choices["start"]
    start.add_argument("--bundle", help="Local catalog ZIP or approved HTTPS URL")
    start.add_argument("--sha256", help="Catalog ZIP SHA-256, required for a URL")
    start.add_argument(
        "--recall-snapshot", type=Path, help="Approved local recall snapshot directory"
    )
    start.add_argument("--port", type=int, default=None)
    start.add_argument("--session-microusd", type=int, default=None)
    start.add_argument("--round-microusd", type=int, default=None)
    start.add_argument(
        "--synthetic", action="store_true", help="Quota-free smoke test with synthetic data"
    )
    export = actions.add_parser("export-bundle")
    export.add_argument(
        "--source",
        required=True,
        type=parse_service_name,
        help="Read-only libpq service name, e.g. auralis_desktop_export",
    )
    for name in ("audience", "index", "curated", "taxonomy", "output"):
        export.add_argument("--" + name, type=Path, required=True)
    return command


def run(args: argparse.Namespace) -> int:
    if args.action == "export-bundle":
        bundle.export_bundle(args)
        return 0

    state = state_path(args.state) if args.action == "start" else args.state.expanduser().resolve()
    settings_path = state / "settings.json"
    if args.action == "start" and settings_path.is_symlink():
        raise ValueError(
            "Desktop settings must be a regular file inside the approved state directory."
        )
    if args.action != "start" and not settings_path.is_file():
        print("Desktop services are stopped. No local state exists for this path.")
        return 0
    if args.action == "start" and not settings_path.exists():
        problem = port_problem(4180 if args.port is None else args.port)
        if problem:
            raise ValueError(problem)

    # Docker Desktop must be able to share these artifacts with its Linux VM.
    state.mkdir(parents=True, exist_ok=True, mode=0o777 if os.name == "nt" else 0o700)
    with lock_state(state):
        if args.action == "start":
            start(args, state, settings_path)
        else:
            desktop = Desktop(state, bundle.read(settings_path))
            if args.action == "stop":
                desktop.compose("down", "--remove-orphans")
            else:
                desktop.compose("ps", "--all")
                running = desktop.compose("ps", "--status", "running", "-q", "web", capture=True)
                ready = bool(running.strip()) and desktop.ready()
                print(f"{desktop.origin} {'UP' if ready else 'UNAVAILABLE'}")
                return 0 if ready else 1
    return 0


def start(args: argparse.Namespace, state: Path, settings_path: Path) -> None:
    exists = settings_path.exists()
    settings = (
        bundle.read(settings_path)
        if exists
        else {
            "port": 4180,
            "session_microusd": 500_000,
            "round_microusd": 300_000,
            "synthetic": args.synthetic,
        }
    )
    for option in ("session_microusd", "round_microusd"):
        value = getattr(args, option)
        if exists and value is not None and value != settings[option]:
            raise ValueError(f"Use a fresh --state to change {option.replace('_', '-')}.")
        if value is not None:
            settings[option] = value
    previous_port = settings["port"]
    if args.port is not None:
        settings["port"] = args.port
    if not 0 < settings["round_microusd"] <= settings["session_microusd"] <= 2_147_483_647:
        raise ValueError("Budget must satisfy 0 < round <= session <= 2147483647 microdollars.")
    if args.synthetic and not settings["synthetic"]:
        raise ValueError("Use a fresh --state for a synthetic smoke test.")
    if settings["synthetic"] and (args.bundle or args.recall_snapshot):
        raise ValueError("Synthetic mode generates its own fixtures.")

    data = state / "bundle"
    if not settings["synthetic"] and data.exists():
        bundle.required_metadata(bundle.read(data / "bundle.json"))
        if args.bundle and (
            bundle.source_hash(args.bundle, args.sha256)
            != (data / bundle.ARCHIVE_DIGEST).read_text(encoding="ascii")
        ):
            raise ValueError("This state has a different catalog. Choose a new --state for it.")

    recall = args.recall_snapshot or settings.get("recall_snapshot")
    recall = Path(recall).expanduser().resolve() if recall else None
    if exists:
        current = Desktop(state, settings | {"port": previous_port})
        if current.compose("ps", "--status", "running", "-q", capture=True).strip():
            if settings["port"] != previous_port or (
                args.recall_snapshot and str(recall) != settings.get("recall_snapshot")
            ):
                raise ValueError(
                    "Stop this desktop project before changing its port or recall path."
                )
            if current.ready():
                print(f"Already running at {current.origin}.")
                return
            raise ValueError("The desktop project is not ready. Stop it, then rerun start.")

    problem = port_problem(settings["port"])
    if problem:
        raise ValueError(problem)
    if not settings["synthetic"]:
        if not (args.bundle or data.exists()):
            raise ValueError("First start requires an approved --bundle or --synthetic.")
        if not recall:
            raise ValueError("Provide a separately approved --recall-snapshot directory.")
        if not all((recall / name).is_file() for name in bundle.RECALL_FILES):
            raise ValueError("The recall snapshot must contain dataset.json and both NPZ files.")
        settings["recall_snapshot"] = str(recall)

    key = ""
    if not settings["synthetic"]:
        key = (os.environ.get(KEY) or getpass.getpass("OpenAI API key: ")).strip()
        if not key:
            raise ValueError("An OpenAI API key is required.")
    if not data.exists():
        if settings["synthetic"]:
            synthetic.create_bundle(data)
        else:
            bundle.import_bundle(args.bundle, args.sha256, state)
    if not exists:
        settings["credentials"] = {key: secrets.token_urlsafe(36) for key in CREDENTIALS}
    save_settings(settings_path, settings)
    desktop = Desktop(state, settings)
    desktop.config.mkdir(exist_ok=True)
    desktop.start(key)


def main() -> int:
    if sys.version_info < (3, 11):
        print("Desktop requires Python 3.11 or later.", file=sys.stderr)
        return 1
    command = parser()
    args, extra = command.parse_known_args()
    if extra:
        if args.action == "export-bundle":
            command.error("Unexpected export-bundle arguments.")
        command.error(f"unrecognized arguments: {' '.join(extra)}")
    try:
        return run(args)
    except subprocess.CalledProcessError as error:
        print(f"Desktop: {args.action} failed (exit code {error.returncode}).", file=sys.stderr)
        return 1
    except (OSError, ValueError, BadZipFile, KeyError) as error:
        print(f"Desktop: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
