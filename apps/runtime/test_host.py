"""Failure boundaries with real child pipes and isolated files; no provider calls."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock
from unittest.mock import Mock, patch

from host import Child, Failure, Host, environment, exclusive
from recovery import replay


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = json.loads(Path(__file__).with_name("host.example.json").read_text())

    def host(self):
        path = self.root / "host.json"
        path.write_text(json.dumps(self.config))
        return Host(path)

    def test_invalid_names_and_research_database_rejected(self):
        for field, value in (
            ("name", "other-task"),
            ("database", "resonance_research"),
            ("java_port", 543),
            ("java_port", 18765),
        ):
            with self.subTest(field=field, value=value):
                old = self.config[field]
                self.config[field] = value
                with self.assertRaises(Failure):
                    self.host()
                self.config[field] = old

    def test_environment_does_not_pass_database_or_unrelated_secrets_to_agent(self):
        with patch.dict(
            os.environ,
            {
                "AURALIS_DATABASE_PASSWORD": "private",
                "UNRELATED_KEY": "private",
                "AURALIS_SERVICE_TOKEN": "test",
            },
        ):
            env = environment(("AURALIS_SERVICE_TOKEN",))
            self.assertNotIn("AURALIS_DATABASE_PASSWORD", env)
            self.assertNotIn("UNRELATED_KEY", env)
            self.assertEqual("test", env["AURALIS_SERVICE_TOKEN"])

    def provider_host(self, provider, key):
        for field in ("java", "python", "jar", "genre_taxonomy", "search_index"):
            self.config[field] = "." if field == "search_index" else "artifact"
        self.config["search_manifest_sha256"] = "a" * 64
        self.config["agent_config"] = "agent.json"
        self.config["recommendation_config"] = "recommendation.json"
        (self.root / "artifact").touch()
        digest = "sha256:" + "b" * 64
        agent = {
            **({"model_provider": provider} if provider else {}),
            "provider_key_env": key,
            "service_token_env": "AURALIS_SERVICE_TOKEN",
            "control_token_env": "AURALIS_SERVICE_TOKEN",
            "control_url": f"http://127.0.0.1:{self.config['java_port']}",
            "genre_taxonomy": "artifact",
            "reference": "artifact",
            "recall_snapshot": "artifact",
            "model_bindings": "artifact",
            "audience_snapshot": "artifact",
            "evidence_mode": "live",
            "audience_binding": {
                "dataset_id": "catalog",
                "window": "0",
                "sample_listeners": 1,
                "snapshot_digest": digest,
            },
            "service_resources": {},
        }
        (self.root / "agent.json").write_text(json.dumps(agent))
        (self.root / "recommendation.json").write_text(
            json.dumps(
                {
                    "audience_binding": {
                        "dataset": "catalog",
                        "window": "0",
                        "sampleListeners": 1,
                        "snapshotDigest": digest,
                    },
                    "budget_limits": {},
                    "prices": {},
                }
            )
        )
        return self.host()

    def test_provider_check_and_runtime_secret_scope(self):
        for provider, key, other in (
            ("deepseek", "AURALIS_DEEPSEEK_API_KEY", "AURALIS_PRODUCT_TEST_API_KEY"),
            (None, "AURALIS_PRODUCT_TEST_API_KEY", "AURALIS_DEEPSEEK_API_KEY"),
        ):
            with self.subTest(provider=provider):
                host = self.provider_host(provider, key)
                port_independent = patch.multiple(
                    host,
                    stopped=Mock(),
                    sql=Mock(return_value=b"\n t\n"),
                    owned=Mock(),
                    scalar=Mock(return_value="f"),
                    call=Mock(),
                    prepare_reach=Mock(return_value=self.root / "reach.json"),
                    wait_ready=Mock(),
                )
                secrets = {
                    "AURALIS_DATABASE_PASSWORD": "synthetic-database",
                    "AURALIS_SERVICE_TOKEN": "synthetic-service",
                    "AURALIS_SUBJECT_SECRET": "synthetic-subject",
                    key: "synthetic-provider",
                }
                with (
                    port_independent,
                    patch.dict(os.environ, secrets, clear=True),
                    patch(
                        "host.subprocess.run",
                        return_value=Mock(returncode=0, stderr=b'version "21.0.9"'),
                    ),
                    patch("host.Child") as child,
                ):
                    host.preflight()
                    assert key in host.call.call_args.kwargs["env"]
                    os.environ[other] = "unused-provider"
                    child.return_value.process.poll.return_value = 1
                    with self.assertRaisesRegex(Failure, "SERVICE_EXITED"):
                        host.run()
                    python_env, java_env = [call.args[1] for call in child.call_args_list]
                    self.assertIn(key, python_env)
                    self.assertNotIn(other, python_env)
                    self.assertNotIn(key, java_env)
                    self.assertNotIn(other, java_env)

    def test_provider_key_cannot_name_service_token(self):
        host = self.provider_host("deepseek", "AURALIS_SERVICE_TOKEN")
        with (
            patch(
                "host.subprocess.run", return_value=Mock(returncode=0, stderr=b'version "21.0.9"')
            ),
            patch.object(host, "agent_environment") as agent_env,
        ):
            with self.assertRaisesRegex(Failure, "SECRET_SCOPE_MISMATCH"):
                host.preflight()
            agent_env.assert_not_called()

    def test_owned_rejects_foreign_container(self):
        host = self.host()
        with patch.object(host, "docker", return_value=b'[{"Config":{"Labels":{}}}]'):
            with self.assertRaisesRegex(Failure, "FOREIGN_CONTAINER"):
                host.owned()

    def test_os_lock_is_exclusive_and_released(self):
        name = "auralis-test-" + str(os.getpid())
        with exclusive(name):
            with self.assertRaisesRegex(Failure, "HOST_ALREADY_MANAGED"):
                with exclusive(name):
                    pass
        with exclusive(name):
            pass

    def test_child_eof_is_graceful_and_unrelated_output_is_discarded(self):
        child = Child(
            [
                sys.executable,
                "-u",
                "-c",
                "import sys; print('secret-output'); "
                "assert sys.stdin.readline() == 'MAINTENANCE\\n'; "
                "print('AURALIS_MAINTENANCE', flush=True); "
                "assert sys.stdin.read() == ''; print('secret-output')",
            ],
            environment(),
        )
        try:
            child.send("MAINTENANCE\n")
            self.assertTrue(child.maintenance.wait(5))
        finally:
            child.close()
        self.assertEqual(0, child.process.returncode)

    def test_telemetry_variables_pass_to_children(self):
        switches = {
            "AURALIS_TELEMETRY_ENABLED": "false",
            "AURALIS_TRACE_SAMPLE_RATE": "1.0",
            "AURALIS_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:4318/v1/traces",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
            "UNRELATED_SECRET": "synthetic-private",
        }
        with mock.patch.dict(os.environ, switches):
            child = environment()
        self.assertEqual(
            {key: child[key] for key in switches if key != "UNRELATED_SECRET"},
            {key: value for key, value in switches.items() if key != "UNRELATED_SECRET"},
        )
        self.assertNotIn("UNRELATED_SECRET", child)

    def test_child_forwards_only_safe_diagnostic_categories(self):
        output = io.StringIO()
        with redirect_stderr(output):
            child = Child(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    "print('secret-output'); print('AURALIS_AGENT_STARTUP_FAILED FileNotFoundError'); "
                    "print('logger WARN AURALIS_DISPATCH_FAILED Unavailable'); "
                    "print('AURALIS_DISPATCH_LIFECYCLE_FAILED SQLException sqlstate=55P03'); "
                    "print('WARN AURALIS_DB_DIAGNOSTIC phase=delivery outcome=failed "
                    "exception=SQLException sqlstate=55P03 execution=-'); "
                    "print('AURALIS_DB_DIAGNOSTIC phase=delivery outcome=failed "
                    "exception=SQLException sqlstate=55P03 execution=- private-secret'); "
                    "print('AURALIS_DB_LOCK_SNAPSHOT result=ok sessions=5/active/Lock/tuple/5/6/4,3'); "
                    "print('AURALIS_DB_LOCK_SNAPSHOT result=ok sessions=5/active/Lock/tuple/5/6/secret'); "
                    "print('AURALIS_STARTUP_FAILED ValueError private-secret')",
                ],
                environment(),
            )
            child.close()
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "AURALIS_AGENT_STARTUP_FAILED FileNotFoundError",
                "AURALIS_DISPATCH_FAILED Unavailable",
                "AURALIS_DISPATCH_LIFECYCLE_FAILED SQLException sqlstate=55P03",
                "AURALIS_DB_DIAGNOSTIC phase=delivery outcome=failed "
                "exception=SQLException sqlstate=55P03 execution=-",
                "AURALIS_DB_LOCK_SNAPSHOT result=ok sessions=5/active/Lock/tuple/5/6/4,3",
            ],
        )

    def test_replay_validates_entire_csv_before_writing(self):
        host = self.host()
        path = self.root / "barriers.csv"
        path.write_text(
            "credential_digest,revoked_at,expires_at\n"
            + "sha256:"
            + "a" * 64
            + ",2026-01-01T00:00:00+00:00,2027-01-01T00:00:00+00:00\n"
            + "bad',2026-01-01T00:00:00+00:00,2027-01-01T00:00:00+00:00\n"
        )
        with patch.object(host, "sql") as sql:
            with self.assertRaises(Failure):
                replay(host, path)
            sql.assert_not_called()

    def test_cli_does_not_echo_invalid_configuration(self):
        path = self.root / "private.json"
        path.write_text('{"private-secret":')
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("host.py")),
                "check",
                "--config",
                str(path),
            ],
            capture_output=True,
        )
        self.assertEqual(1, result.returncode)
        self.assertEqual(b"AURALIS_OPERATION_FAILED", result.stderr.strip())

    def test_replay_accepts_postgresql_timezone_and_is_monotonic(self):
        host = self.host()
        path = self.root / "barriers.csv"
        path.write_text(
            "credential_digest,revoked_at,expires_at\nsha256:"
            + "a" * 64
            + ",2026-11-09 03:52:42.214435+00,2026-12-09 03:52:42.214435+00\n"
        )
        with patch.object(host, "sql") as sql:
            replay(host, path)
            statement = sql.call_args.args[0]
            self.assertIn("GREATEST(subject_revocation.expires_at", statement)
            self.assertIn("2026-11-09T03:52:42.214435+00:00", statement)
            self.assertTrue(statement.startswith("BEGIN;"))


class DiagnosticWindowTests(unittest.TestCase):
    def test_restores_other_settings_after_one_restore_fails(self):
        from contextlib import contextmanager
        from types import SimpleNamespace

        from pg_lock_window import SETTINGS, lock_diagnostic_window

        before = {
            "log_lock_waits": "off",
            "deadlock_timeout": "750ms",
            "log_min_error_statement": "panic",
            "log_parameter_max_length_on_error": "0",
        }
        settings = dict(before)
        restores = []

        class Admin:
            autocommit = True
            info = SimpleNamespace(dbname="auralis_core_verify")
            restoring = False

            def execute(self, statement, *params):
                if isinstance(statement, str):
                    if "split_part" in statement:
                        return [("deadlock_timeout", settings["deadlock_timeout"])]
                    if "pg_terminate_backend" in statement:
                        return Mock(fetchall=Mock(return_value=[]))
                    return []
                command = statement.as_string()
                name = next(key for key in SETTINGS if f'"{key}"' in command)
                if self.restoring:
                    restores.append(name)
                    if name == "log_lock_waits":
                        raise RuntimeError("fixture restore failure")
                    settings[name] = before[name]
                else:
                    settings[name] = SETTINGS[name]

        @contextmanager
        def online():
            def execute(query):
                name = query.as_string().split('"')[1]
                return Mock(fetchone=Mock(return_value=(settings[name],)))

            yield SimpleNamespace(execute=execute)

        admin = Admin()
        with self.assertRaisesRegex(
            RuntimeError, "DIAGNOSTICS_SETTINGS_NOT_RESTORED:log_lock_waits"
        ):
            with lock_diagnostic_window(admin, online, "auralis_online"):
                admin.restoring = True
        self.assertEqual(restores, list(SETTINGS))
        self.assertEqual(settings["deadlock_timeout"], "750ms")
        self.assertEqual(settings["log_min_error_statement"], "panic")


if __name__ == "__main__":
    unittest.main()
