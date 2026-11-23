"""Regression checks for desktop start recovery and the distribution file boundary."""

import argparse
import io
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import bundle
import catalog
import desktop
import synthetic

EXPORT_INPUTS = [
    "--audience",
    "audience.json",
    "--index",
    "index",
    "--curated",
    "curated",
    "--taxonomy",
    "taxonomy",
    "--output",
    "catalog.zip",
]


class DesktopTest(unittest.TestCase):
    def test_state_paths_use_checkout_boundaries(self):
        for name in ("review-state", "apps/frontend/src/review-state", "apps/backend/review-state"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "outside Git"):
                desktop.state_path(desktop.ROOT / name)
        for state in (desktop.HERE / ".local", desktop.ROOT / "local-data" / "review-state"):
            self.assertEqual(desktop.state_path(state), state.resolve())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(desktop.state_path(root / "state"), root / "state")
            (root / ".git").mkdir()
            with self.assertRaisesRegex(ValueError, "outside Git"):
                desktop.state_path(root / "state")

    def test_occupied_port_does_not_create_settings_or_bundle(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            state = Path(directory) / "state"
            args = desktop.parser().parse_args(
                [
                    "start",
                    "--synthetic",
                    "--state",
                    str(state),
                    "--port",
                    str(listener.getsockname()[1]),
                ]
            )
            with self.assertRaisesRegex(ValueError, "keep the same --state and bundle"):
                desktop.run(args)
            self.assertFalse(state.exists())

    def test_bundle_allowlist_and_same_state_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(
                source="synthetic",
                taxonomy=root / "taxonomy",
                index=root / "index",
                curated=root / "curated",
                audience=root / "audience.json",
                output=root / "desktop.zip",
            )
            index_files = {"index/segments_1", "index/_0.si", "index/_0_Lucene912_0.psm"}
            for name in bundle.ARTIFACTS | index_files:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="utf-8")
            private_files = (
                "taxonomy/snapshot/interactions.npz",
                "taxonomy/snapshot/album_tags.npz",
                "index/settings.json",
                "curated/01-no-bad-memories/unrelated.json",
            )
            for name in private_files:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("private marker", encoding="utf-8")
            bundle.write_json(args.audience, synthetic.audience())

            def fixture_csv(_source, target, _audience):
                target.mkdir()
                for table, columns in catalog.COLUMNS.items():
                    (target / f"{table}.csv").write_text(
                        ",".join(columns.split(", ")) + "\n", encoding="utf-8"
                    )

            with patch.object(catalog, "export_csv", side_effect=fixture_csv):
                bundle.export_bundle(args)
            with ZipFile(args.output) as archive:
                self.assertEqual(set(archive.namelist()), bundle.REQUIRED | index_files)
                for name in private_files:
                    self.assertNotIn(name, archive.namelist())
            recall = root / "taxonomy" / "snapshot"
            (recall / "dataset.json").write_text("fixture", encoding="utf-8")
            state = root / "state"
            command = [
                "start",
                "--state",
                str(state),
                "--bundle",
                str(args.output),
                "--recall-snapshot",
                str(recall),
            ]
            with (
                patch.object(desktop, "port_problem", return_value=None),
                patch.object(desktop.Desktop, "start"),
                patch.object(desktop.Desktop, "compose", return_value=""),
                patch.dict(os.environ, {desktop.KEY: "unused synthetic test key"}),
            ):
                for port in ("44180", "44181"):
                    self.assertEqual(
                        desktop.run(desktop.parser().parse_args([*command, "--port", port])), 0
                    )
                    settings = bundle.read(state / "settings.json")
                    self.assertEqual(settings["port"], int(port))
                    if port == "44180":
                        original = settings["credentials"]
                    self.assertEqual(settings["credentials"], original)
                args.output.write_bytes(b"different archive")
                with self.assertRaisesRegex(ValueError, "different catalog"):
                    desktop.run(desktop.parser().parse_args(command))

    def test_archive_and_audience_reject_unapproved_fields(self):
        names = list(bundle.REQUIRED | {"index/segments_1", "index/_0.si"})
        for name in (
            "taxonomy/snapshot/interactions.npz",
            "catalog/album_listen.csv",
            "curated/01-no-bad-memories/private.json",
            "index/extra.json",
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "allowlist"):
                bundle.check_members([*names, name])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audience.json"
            audience = synthetic.audience()
            audience["counts"][0]["listener_id"] = "private marker"
            bundle.write_json(path, audience)
            with self.assertRaisesRegex(ValueError, "only an album ID and an aggregate count"):
                catalog.read_audience(path)

    def test_export_rejects_connection_strings_without_echoing_password(self):
        password = "PLACEHOLDER_DESKTOP_PASSWORD"
        sources = (
            [f"service=auralis password={password}"],
            [f"postgresql://reader:{password}@localhost/catalog"],
            ["service=auralis", f"password={password}"],
            ["auralis", f"password={password}"],
        )
        for form, source in enumerate(sources):
            stderr = io.StringIO()
            argv = ["desktop.py", "export-bundle", "--source", *source, *EXPORT_INPUTS]
            with (
                self.subTest(form=form),
                patch.object(sys, "argv", argv),
                patch.object(catalog.subprocess, "run") as child,
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as error,
            ):
                desktop.main()
            self.assertEqual(error.exception.code, 2)
            self.assertNotIn(password, stderr.getvalue())
            child.assert_not_called()

    def test_export_passes_only_service_and_reports_only_operation_and_exit_code(self):
        password = "PLACEHOLDER_DESKTOP_PASSWORD"
        service = "auralis_desktop_export"
        stderr = io.StringIO()
        stdout = io.StringIO()
        failure = subprocess.CalledProcessError(
            17, ["psql", f"password={password}"], output=password, stderr=password
        )
        credentials = {
            "PGSERVICEFILE": "test-service.conf",
            "PGPASSFILE": "test-pass.conf",
            "PGPASSWORD": password,
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                sys, "argv", ["desktop.py", "export-bundle", "--source", service, *EXPORT_INPUTS]
            ),
            patch.object(catalog.shutil, "which", return_value="psql"),
            patch.object(catalog.subprocess, "run", side_effect=failure) as child,
            patch.dict(os.environ, credentials),
            redirect_stderr(stderr),
            redirect_stdout(stdout),
        ):
            target = Path(directory) / "catalog"
            with patch.object(
                bundle,
                "export_bundle",
                side_effect=lambda args: catalog.export_csv(
                    args.source, target, synthetic.audience()
                ),
            ):
                self.assertEqual(desktop.main(), 1)
        child.assert_called_once()
        command = child.call_args.args[0]
        options = child.call_args.kwargs
        self.assertEqual(command, ["psql", "-Xq", "-w", "-v", "ON_ERROR_STOP=1"])
        self.assertNotIn(password, " ".join(command))
        self.assertNotIn(password, options["input"])
        self.assertNotIn("PGPASSWORD", options["env"])
        self.assertEqual(options["env"]["PGSERVICE"], service)
        self.assertEqual(options["env"]["PGSERVICEFILE"], credentials["PGSERVICEFILE"])
        self.assertEqual(options["env"]["PGPASSFILE"], credentials["PGPASSFILE"])
        self.assertEqual(options["stdout"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "Desktop: export-bundle failed (exit code 17).\n")
