"""Drive the production Java jar and Python serve with an isolated PG18 and synthetic providers."""

import argparse
import contextlib
import hashlib
import http.cookiejar
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from auralisy_intelligence.contracts import content_digest, digest
from auralisy_intelligence.genre_taxonomy import taxonomy_hash
from auralisy_intelligence.preferences import ReferenceCatalog
from auralisy_intelligence.preferences.stage import PARSER_REVISION
from auralisy_intelligence.providers.config import load_role_bindings
from auralisy_intelligence.providers.java_config import java_config
from auralisy_intelligence.recall import WeightConfig
from psycopg import sql
from psycopg.rows import dict_row

INTELLIGENCE = Path(__file__).resolve().parents[1]
PRODUCT = INTELLIGENCE.parents[1]
sys.path.insert(0, str(PRODUCT / "apps/runtime"))
from host import Child, environment  # noqa: E402
from pg_lock_window import lock_diagnostic_window  # noqa: E402

GENRE = str(UUID(int=101))


def write(path, value):
    path.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(action, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = action()
        if result:
            return result
        time.sleep(0.035)
    raise TimeoutError(f"timed out in {action.__name__}")


def events(root):
    path = root / "provider-events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
        if line.endswith("\n")
    ]


def assets(root, origin):
    albums = [str(UUID(int=i)) for i in range(1, 9)]
    genres = [{"id": GENRE, "name": "Rock", "aliases": [], "description": "Rock"}]
    write(
        root / "taxonomy.json",
        {
            "format_version": 1,
            "core_manifest_sha256": "a" * 64,
            "source_sha256": "0" * 64,
            "genres": [{"id": GENRE, "name": "Rock", "aliases": [], "parents": []}],
        },
    )
    revision = taxonomy_hash(root / "taxonomy.json")
    reference = {"taxonomy_sha256": revision, "genres": genres, "branches": []}
    write(root / "reference.json", reference)
    write(
        root / "train.json",
        {
            "format_version": 1,
            "dataset_id": "synthetic",
            "window": "0",
            "album_ids": albums,
            "tag_ids": [GENRE],
            "tag_parents": [],
            "album_tags": [[1]] * len(albums),
            "interactions": [[1 + (i + user) % 4 for i in range(len(albums))] for user in range(6)],
            "provenance": {"taxonomy_sha256": revision},
        },
    )
    audience = {
        "format_version": 2,
        "scope": "FULL_CATALOG",
        "source": "LISTENBRAINZ_SAMPLE",
        "catalog_dataset_id": 1,
        "source_manifest_sha256": "a" * 64,
        "dataset_id": "core:" + "a" * 64,
        "window": "0",
        "sample_listeners": 100,
        "counts": [{"release_group_id": album, "listeners": 60} for album in albums],
    }
    audience["digest"] = content_digest(audience, "digest")
    write(root / "audience.json", audience)
    binding = {key: audience[key] for key in ("dataset_id", "window", "sample_listeners")}
    binding["snapshot_digest"] = audience["digest"]
    service = read(INTELLIGENCE / "agent-service.example.json")
    service.update(
        genre_taxonomy="taxonomy.json",
        reference="reference.json",
        recall_snapshot="train.json",
        audience_snapshot="audience.json",
        audience_binding=binding,
        evidence_mode="live",
        model_bindings=str(INTELLIGENCE / "autonomous-bindings.example.json"),
        control_url=origin,
        control_token_env="AURALIS_SERVICE_TOKEN",
    )
    for quota in service["service_resources"]["quota_groups"].values():
        quota.update(rpm=100000, tpm=100000000)
    write(root / "agent-service.json", service)
    template = read(INTELLIGENCE / "tests/fixtures/agent-contract-cases.json")[0]["message"][
        "payload"
    ]
    settings = {key: template[key] for key in ("agent_policy", "selection_config", "web_guards")}
    settings.update(java_config(load_role_bindings(service["model_bindings"])))
    settings["selection_config"].update(
        recall_target_count=2, shortlist_target_count=2, recommendation_target_count=2
    )
    settings.update(
        stage_capacity=settings["selection_config"],
        reference_revision=ReferenceCatalog(reference).revision,
        parser_revisions=[PARSER_REVISION, "generic-discovery.v2"],
        reference_genres={GENRE: "Rock"},
        recall_binding={
            "strategy_revision": "A-V2",
            "data_snapshot_id": "synthetic",
            "window": "0",
            "weights_digest": digest(asdict(WeightConfig())),
        },
        audience_binding={
            "dataset": binding["dataset_id"],
            "window": "0",
            "sampleListeners": 100,
            "snapshotDigest": binding["snapshot_digest"],
        },
        budget_limits={
            "session_cap_microusd": 1000000,
            "round_cap_microusd": 1000000,
            "model_calls": 100,
            "search_calls": 50,
            "read_calls": 100,
            "lane_caps": {"FOREGROUND": 500000, "BACKGROUND": 500000},
        },
        idle_close_ms=500,
        capabilities=["web_search.v1", "web_read.v1"],
    )
    settings["web_guards"].update(max_bytes=100000, read_timeout_ms=3000, max_redirects=2)
    settings["agent_policy"].update(audience_binding=binding, audience_min_listeners=50)
    settings["agent_policy"]["recall"]["weights"] = asdict(WeightConfig())
    settings["agent_policy"]["writing"].update(short_min_han=40, short_max_han=180)
    settings["agent_policy"]["digest"] = content_digest(settings["agent_policy"], "digest")
    write(root / "recommendation.json", settings)
    return albums, audience


def seed(connection, albums, audience):
    connection.execute(
        "INSERT INTO ingestion.dataset(manifest_sha256,layout_sha256,purpose,manifest,status) "
        "VALUES (%s,%s,'private-development','{}','IMPORTING')",
        ("a" * 64, "b" * 64),
    )
    connection.execute("INSERT INTO ingestion.active_dataset VALUES(true,1)")
    connection.execute("INSERT INTO catalog.genre VALUES(1,1,%s,'Rock','')", (GENRE,))
    connection.execute(
        "INSERT INTO discovery.audience_snapshot VALUES(%s,%s,'0',100,%s,%s,"
        '\'{"scope":"FULL_CATALOG","catalog_dataset_id":1}\',true)',
        (audience["digest"], audience["dataset_id"], audience["digest"], "a" * 64),
    )
    for index, album in enumerate(albums, 1):
        title, artist = f"Synthetic Album {index}", f"Synthetic Artist {index}"
        connection.execute(
            "INSERT INTO catalog.artist VALUES(1,%s,%s,%s,%s,'',NULL,now())",
            (index, str(UUID(int=index + 200)), artist, artist),
        )
        connection.execute(
            "INSERT INTO catalog.album(dataset_id,id,mbid,title,comment,source_credit_id,"
            "primary_type,source_updated) VALUES(1,%s,%s,%s,'',1,'Album',now())",
            (index, album, title),
        )
        connection.execute(
            "INSERT INTO catalog.album_credit VALUES(1,%s,0,%s,%s,'')", (index, index, artist)
        )
        connection.execute("INSERT INTO catalog.album_genre VALUES(1,%s,1,1,1,now())", (index,))
        connection.execute(
            "INSERT INTO discovery.audience_count VALUES(%s,'0',100,%s,60)",
            (audience["digest"], album),
        )
    connection.execute("UPDATE ingestion.dataset SET status='ACTIVE' WHERE id=1")
    connection.execute("ANALYZE")


class Browser:
    def __init__(self, origin):
        self.origin = origin
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )
        self.csrf = ""

    def request(self, route, data=None):
        headers = {
            "Origin": self.origin,
            "Idempotency-Key": str(uuid4()),
            "X-CSRF-Token": self.csrf,
            "Content-Type": "application/json",
        }
        payload = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(self.origin + route, headers=headers, data=payload)
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def submit(self):
        status, bootstrap = self.request("/v1/bootstrap", {})
        assert status == 200, (status, bootstrap)
        self.csrf = bootstrap["csrf_token"]
        state = bootstrap["state"]
        status, submitted = self.request(
            "/v1/discovery-sessions",
            {
                "expected_session_id": state["session_id"],
                "expected_revision": state["revision"],
                "input": {
                    "mode": "explore",
                    "language": "zh-CN",
                    "request_text": "",
                    "liked_genres": [{"genre_id": GENRE, "strength": "special", "focused": True}],
                    "disliked_genre_ids": [],
                    "liked_albums": [],
                },
            },
        )
        assert status == 202, (status, submitted)


def healthy(origin, route, token=None):
    headers = (
        {"Authorization": "Bearer " + token, "X-Auralis-Audience": "intelligence"} if token else {}
    )
    try:
        with urllib.request.urlopen(
            urllib.request.Request(origin + route, headers=headers), timeout=1
        ) as response:
            return response.status == 200
    except OSError:
        return False


def services(stack, root, args, jdbc, role, password, java_port, python_port, receipt):
    token = (
        args.service_token_file.read_text(encoding="ascii").strip()
        if args.service_token_file
        else secrets.token_urlsafe(48)
    )
    origin = f"http://127.0.0.1:{java_port}"
    python_origin = f"http://127.0.0.1:{python_port}"
    env = environment()
    env.update(AURALIS_SERVICE_TOKEN=token)
    if args.telemetry in ("false", "true"):
        env["AURALIS_TELEMETRY_ENABLED"] = args.telemetry
    else:
        env.pop("AURALIS_TELEMETRY_ENABLED", None)
    if args.telemetry == "true":
        env["AURALIS_TRACE_SAMPLE_RATE"] = "1.0"
    if args.otlp:
        env.update(
            OTEL_EXPORTER_OTLP_ENDPOINT=args.otlp,
            AURALIS_OTLP_TRACES_ENDPOINT=args.otlp + "/v1/traces",
        )
    agent_env = {
        **env,
        "PYTHONPATH": str(INTELLIGENCE / "src"),
        "AURALIS_PRODUCT_TEST_API_KEY": "synthetic-key",
        "PYTHONUTF8": "1",
    }
    agent = Child(
        [
            sys.executable,
            str(INTELLIGENCE / "tests/java_integration_peer.py"),
            "--config",
            str(root / "agent-service.json"),
            "--port",
            str(python_port),
        ],
        agent_env,
    )
    stack.callback(agent.close)
    wait_for(lambda: healthy(python_origin, "/internal/v1/runtime", token) or check_alive(agent))
    java_env = {
        **env,
        "AURALIS_JDBC_URL": jdbc,
        "AURALIS_DATABASE_USER": role,
        "AURALIS_DATABASE_PASSWORD": password,
        "AURALIS_SUBJECT_SECRET": secrets.token_urlsafe(48),
        "AURALIS_PRODUCT_ORIGIN": origin,
        "AURALIS_PYTHON_URL": python_origin,
        "AURALIS_CONTRACT_REVISION": "0.32",
        "AURALIS_ALLOW_INSECURE_COOKIE": "true",
        "AURALIS_MANAGED_STDIN": "true",
        "AURALIS_DISPATCH_ENABLED": "true",
        "AURALIS_RECOMMENDATION_CONFIG": str(root / "recommendation.json"),
        "AURALIS_GENRE_TAXONOMY": str(root / "taxonomy.json"),
        "SERVER_PORT": str(java_port),
    }
    java = Child([args.java, "-Xmx384m", "-jar", str(args.jar)], java_env)

    def shutdown():
        if java.process.poll() is None:
            java.send("MAINTENANCE\n")
            java.maintenance.wait(10)
        agent.close()
        java.close()
        receipt["process_exit"] = {
            "java": java.process.returncode,
            "python": agent.process.returncode,
        }

    stack.callback(shutdown)
    wait_for(lambda: healthy(origin, "/actuator/health/readiness") or check_alive(java), timeout=50)
    receipt["python_source_module"] = read(root / "python-pid.json")["source"]
    return origin


def check_alive(child):
    if child.process.poll() is not None:
        raise RuntimeError(f"process exited {child.process.returncode}: {child.process.args[0]}")
    return False


def lock_round(root, browser, admin, connect_admin, role, polling, receipt):
    def ready_to_lock():
        observed = {
            row["kind"]: row
            for row in events(root)
            if row["kind"] in {"long_call_inflight", "long_delivery_waiting"}
        }
        cards = admin.execute(
            "SELECT count(*) AS count FROM discovery.recommendation_card"
        ).fetchone()
        return observed if len(observed) == 2 and cards["count"] == 2 else None

    observed = wait_for(ready_to_lock)
    delivery = observed["long_delivery_waiting"]
    execution_id = delivery["execution_id"]
    execution = admin.execute(
        "SELECT i.subject_id,e.state,e.coordinator_boot,e.python_boot,"
        "e.cancellation_requested,i.technical_deadline FROM discovery.execution e "
        "JOIN discovery.discovery_intent i ON i.id=e.run_id WHERE e.id=%s",
        (execution_id,),
    ).fetchone()
    assert execution["state"] == "RUNNING" and not execution["cancellation_requested"], execution
    assert execution["technical_deadline"].timestamp() > time.time() + 5, execution
    receipt.update(
        execution=execution,
        original_delivery=delivery,
        inflight=observed["long_call_inflight"],
        waiters=[],
        polls=[],
    )
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            started = time.monotonic()
            status, value = browser.request("/v1/current-state")
            receipt["polls"].append(
                {"at": time.time(), "status": status, "code": value.get("code")}
            )
            stop.wait(max(0, 0.6 - (time.monotonic() - started)))

    tracker = threading.Thread(target=poll, daemon=True) if polling else None
    try:
        with connect_admin(autocommit=False) as locker:
            receipt["lock_pid"] = locker.info.backend_pid
            assert locker.execute(
                "SELECT id FROM discovery.subject WHERE id=%s FOR UPDATE",
                (execution["subject_id"],),
            ).fetchone()
            receipt["locked_at"] = time.time()
            started = time.monotonic()
            if tracker:
                tracker.start()
            (root / "send-long-delivery").touch()
            seen = set()
            while time.monotonic() - started < 5:
                rows = admin.execute(
                    """
                    SELECT pid,wait_event_type,wait_event,pg_blocking_pids(pid) AS blockers
                    FROM pg_stat_activity WHERE datname=current_database() AND usename=%s
                    AND wait_event_type='Lock'
                    """,
                    (role,),
                ).fetchall()
                for row in rows:
                    fingerprint = (row["pid"], row["wait_event"], tuple(row["blockers"]))
                    if fingerprint not in seen:
                        seen.add(fingerprint)
                        receipt["waiters"].append({"at": time.time(), **row})
                sample_peaks(admin, role, receipt)
                time.sleep(0.04)
            locker.rollback()
            receipt["released_at"] = time.time()
    finally:
        (root / "finish-inflight-call").touch()
        stop.set()
        if tracker and tracker.ident:
            tracker.join(6)
    assert any(receipt["lock_pid"] in row["blockers"] for row in receipt["waiters"]), receipt[
        "waiters"
    ]
    return execution_id, delivery["delivery_id"]


def sample_peaks(admin, role, receipt):
    count = admin.execute(
        "SELECT count(*) AS count FROM pg_stat_activity "
        "WHERE datname=current_database() AND usename=%s",
        (role,),
    ).fetchone()["count"]
    receipt["connection_peak"] = max(receipt.get("connection_peak", 0), count)


def collect(root, admin, execution_id, delivery_id, receipt):
    receipt["runtime_control"] = admin.execute(
        "SELECT * FROM diagnostic_audit ORDER BY revision"
    ).fetchall()
    receipt["final_execution"] = admin.execute(
        "SELECT state,physical_released_at,cancellation_requested "
        "FROM discovery.execution WHERE id=%s",
        (execution_id,),
    ).fetchone()
    receipt["original_delivery_result"] = admin.execute(
        "SELECT id,result FROM discovery.delivery_receipt WHERE id=%s", (delivery_id,)
    ).fetchone()
    receipt["ledger"] = admin.execute(
        """
        SELECT i.id, i.purpose, i.outcome, i.send_consumed, i.settled_microusd,
               count(u.receipt_id) AS receipts
        FROM discovery.invocation i LEFT JOIN discovery.usage_receipt u ON u.invocation_id=i.id
        WHERE i.execution_id=%s GROUP BY i.id ORDER BY i.id
        """,
        (execution_id,),
    ).fetchall()
    all_events = events(root)
    receipt["pipeline"] = [row for row in all_events if row["kind"] == "pipeline"]
    receipt["callbacks"] = [row for row in all_events if row["kind"] == "callback_http"]
    receipt["errors"] = [
        row for row in all_events if row["kind"] in {"control_error", "stage_error"}
    ]
    receipt["provider_counts"] = dict(
        Counter(row["schema"] for row in all_events if row["kind"] == "model")
    )
    receipt["inflight_cancelled"] = any(row["kind"] == "long_call_cancelled" for row in all_events)


def audit_runtime(admin):
    admin.execute("""
        CREATE TABLE diagnostic_audit (
            observed_at timestamptz, coordinator_boot text, python_boot text,
            maintenance boolean, revision bigint
        );
        CREATE FUNCTION record_diagnostic_runtime() RETURNS trigger LANGUAGE plpgsql
        SECURITY DEFINER SET search_path=pg_catalog AS $$
        BEGIN
          INSERT INTO public.diagnostic_audit VALUES (clock_timestamp(),NEW.coordinator_boot,
                                                       NEW.python_boot,NEW.maintenance,NEW.revision);
          RETURN NEW;
        END $$;
        CREATE TRIGGER record_diagnostic_runtime AFTER UPDATE ON discovery.runtime_control
        FOR EACH ROW EXECUTE FUNCTION record_diagnostic_runtime();
        """)


def check_database_readiness(admin, role, origin):
    uri = origin + "/actuator/health/readiness"

    def status():
        try:
            with urllib.request.urlopen(uri, timeout=4) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    ready = status()
    assert ready == 200
    identifier = sql.Identifier(role)
    admin.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(identifier))
    try:
        admin.execute(
            "SELECT pg_terminate_backend(pid, 3000) FROM pg_stat_activity "
            "WHERE datname=current_database() AND usename=%s",
            (role,),
        ).fetchall()
        wait_for(lambda: status() == 503, timeout=15)
        return {"before": ready, "without_database": 503}
    finally:
        admin.execute(sql.SQL("ALTER ROLE {} LOGIN").format(identifier))


def scenario(args, root, polling, lock):
    root.mkdir(parents=True)
    java_port, python_port = args.java_port or port(), port()
    albums, audience = assets(root, f"http://127.0.0.1:{java_port}")
    write(root / "faults.json", {"subject_lock_probe": lock})
    # A fresh database and role avoid affecting any other IT or installed service.
    database = "auralis_core"
    role = "lock_verify_" + uuid4().hex
    password = secrets.token_urlsafe(30)
    migrator_password = secrets.token_urlsafe(30)
    admin_password = os.environ["AURALIS_TEST_POSTGRES_ADMIN_PASSWORD"]
    host, pg_port = "127.0.0.1", args.pg_port
    jdbc = f"jdbc:postgresql://{host}:{pg_port}/{database}"

    def connect_admin(*, dbname=database, autocommit=True):
        return psycopg.connect(
            host=host,
            port=pg_port,
            dbname=dbname,
            user="postgres",
            password=admin_password,
            autocommit=autocommit,
            row_factory=dict_row,
        )

    def connect_online():
        return psycopg.connect(
            host=host, port=pg_port, dbname=database, user=role, password=password
        )

    receipt = {
        "polling": polling,
        "lock": lock,
        "java_jar": str(args.jar),
        "java_jar_sha256": hashlib.sha256(args.jar.read_bytes()).hexdigest(),
        "python_source_root": str(INTELLIGENCE / "src"),
        "online_role": role,
        "database": database,
    }
    try:
        with contextlib.ExitStack() as stack:
            setup = stack.enter_context(connect_admin(dbname="postgres"))
            setup.execute("SET log_min_error_statement='panic'")
            assert setup.execute(
                "SELECT 1 FROM pg_database WHERE datname='auralis_core_verify'"
            ).fetchone()
            setup.execute(
                sql.SQL("CREATE ROLE auralis_migrator LOGIN PASSWORD {}").format(
                    sql.Literal(migrator_password)
                )
            )
            stack.callback(setup.execute, "DROP ROLE auralis_migrator")
            setup.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
            stack.callback(setup.execute, sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
            setup.execute(
                sql.SQL("CREATE DATABASE {} OWNER auralis_migrator").format(
                    sql.Identifier(database)
                )
            )
            stack.callback(
                setup.execute,
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)),
            )
            env = {
                **environment(),
                "AURALIS_JDBC_URL": jdbc,
                "AURALIS_MIGRATOR_USER": "auralis_migrator",
                "AURALIS_MIGRATOR_PASSWORD": migrator_password,
            }
            with (root / "migration.log").open("wb") as output:
                subprocess.run(
                    [args.java, "-jar", str(args.jar), "--offline-migrate"],
                    env=env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    check=True,
                    timeout=60,
                )
            admin = stack.enter_context(connect_admin())
            grant = sql.Identifier(role)
            for command in (
                "GRANT USAGE ON SCHEMA catalog,ingestion,statistics,discovery TO {}",
                "GRANT SELECT ON ALL TABLES IN SCHEMA catalog,ingestion,statistics TO {}",
                "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA discovery TO {}",
                "GRANT SELECT ON public.flyway_schema_history TO {}",
            ):
                admin.execute(sql.SQL(command).format(grant))
            admin.execute(
                "CREATE TABLE public.bootstrap_probe(id bigint primary key,value varchar(255))"
            )
            admin.execute(
                sql.SQL("GRANT SELECT,INSERT,DELETE ON public.bootstrap_probe TO {}").format(grant)
            )
            seed(admin, albums, audience)
            audit_runtime(admin)
            if lock:
                # Deliberately retain one pre-existing role/database override across the window.
                admin.execute(
                    sql.SQL("ALTER ROLE {} IN DATABASE {} SET deadlock_timeout='750ms'").format(
                        grant, sql.Identifier(database)
                    )
                )
                diagnostic_admin = stack.enter_context(
                    psycopg.connect(
                        host=host,
                        port=pg_port,
                        dbname=database,
                        user="postgres",
                        password=admin_password,
                        autocommit=True,
                    )
                )
                receipt["postgres_window"] = stack.enter_context(
                    lock_diagnostic_window(diagnostic_admin, connect_online, role)
                )
                probe_secret = "synthetic-parameter-" + uuid4().hex
                receipt["test_secret_sha256"] = hashlib.sha256(probe_secret.encode()).hexdigest()
                with connect_online() as connection:
                    try:
                        connection.execute("SELECT length(%s) / 0", (probe_secret,))
                    except psycopg.errors.DivisionByZero:
                        receipt["test_parameter_error"] = "22012"
            with contextlib.ExitStack() as processes:
                origin = services(
                    processes, root, args, jdbc, role, password, java_port, python_port, receipt
                )
                browser = Browser(origin)
                browser.submit()
                if lock:
                    execution, delivery = lock_round(
                        root, browser, admin, connect_admin, role, polling, receipt
                    )
                else:
                    execution = wait_for(
                        lambda: admin.execute(
                            "SELECT id FROM discovery.execution LIMIT 1"
                        ).fetchone()
                    )["id"]
                    delivery = "-"

                def released():
                    sample_peaks(admin, role, receipt)
                    return admin.execute(
                        "SELECT physical_released_at AS active FROM discovery.execution WHERE id=%s",
                        (execution,),
                    ).fetchone()["active"]

                wait_for(released)
                collect(root, admin, execution, delivery, receipt)
                if lock and not polling:
                    receipt["readiness"] = check_database_readiness(admin, role, origin)
                if not lock:
                    assert receipt["final_execution"]["state"] == "CLOSED", receipt[
                        "final_execution"
                    ]
                    assert receipt["provider_counts"], receipt
                    assert (
                        sum(
                            row["purpose"] == "WRITE_LONG" and row["outcome"] == "COMPLETED"
                            for row in receipt["ledger"]
                        )
                        >= 2
                    ), receipt["ledger"]
                    if args.hold:
                        time.sleep(args.hold)
    finally:
        write(root / "receipt.json", receipt)
    if lock:
        assert_recovered(receipt)


def assert_recovered(receipt):
    assert receipt["pipeline"][0]["name"] == "ProgressivePipeline", receipt["pipeline"]
    assert receipt["connection_peak"] <= 8, receipt["connection_peak"]
    assert not any(row["kind"] == "stage_error" for row in receipt["errors"]), receipt["errors"]
    assert receipt["original_delivery_result"], receipt["errors"]
    assert receipt["original_delivery_result"]["result"]["status"] == "ACCEPTED"
    assert not any(
        row["maintenance"]
        and row["coordinator_boot"] == receipt["execution"]["coordinator_boot"]
        and row["observed_at"].timestamp() >= receipt["locked_at"]
        for row in receipt["runtime_control"]
    ), receipt["runtime_control"]
    assert not receipt["inflight_cancelled"], receipt["errors"]
    assert receipt["final_execution"]["physical_released_at"].timestamp() > receipt["released_at"]
    assert not receipt["final_execution"]["cancellation_requested"]
    original_id = receipt["original_delivery"]["delivery_id"]
    original_callbacks = [
        row for row in receipt["callbacks"] if row.get("delivery_id") == original_id
    ]
    assert any(
        row["status"] == 503
        and row["error_code"] == "DEPENDENCY_UNAVAILABLE"
        and row["retry_disposition"] == "READ_STATUS_OR_SAFE_RETRY"
        for row in original_callbacks
    ), original_callbacks
    assert len({row["payload_digest"] for row in original_callbacks}) == 1
    assert bool(receipt["polls"]) == receipt["polling"]
    assert original_callbacks[-1]["status"] == 200, original_callbacks
    assert sum(row["status"] == 200 for row in original_callbacks) == 1
    long_calls = [row for row in receipt["ledger"] if row["purpose"] == "WRITE_LONG"]
    assert sum(row["send_consumed"] for row in long_calls) == sum(
        count
        for schema, count in receipt["provider_counts"].items()
        if schema.startswith("long_review")
    )
    assert len(long_calls) >= 2 and all(row["receipts"] == 1 for row in long_calls), long_calls


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-port", type=int, required=True)
    parser.add_argument("--java", required=True)
    parser.add_argument("--jar", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--telemetry", choices=("unset", "false", "true"), default="unset")
    parser.add_argument("--otlp")
    parser.add_argument("--java-port", type=int)
    parser.add_argument("--service-token-file", type=Path)
    parser.add_argument("--hold", type=int, default=0)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    args.jar = args.jar.resolve(strict=True)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    if args.smoke:
        scenario(args, args.output / "synthetic", False, False)
    else:
        failures = []
        for name, poll in (("without-polling", False), ("with-polling", True)):
            try:
                scenario(args, args.output / name, poll, True)
            except Exception as error:
                failures.append(error)
                print(f"{name}: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        if failures:
            raise failures[0]


if __name__ == "__main__":
    main()
