"""Opt-in role-scoped PostgreSQL diagnostics for a local test or preview."""

from contextlib import contextmanager

SETTINGS = {
    "log_lock_waits": "on",
    "deadlock_timeout": "500ms",
    "log_min_error_statement": "error",
}


@contextmanager
def lock_diagnostic_window(admin, connect_online, role):
    """Use an autocommit administrator connection; the application pool must be stopped."""
    from psycopg import sql

    database = admin.info.dbname
    target = sql.SQL("ALTER ROLE {} IN DATABASE {}").format(
        sql.Identifier(role), sql.Identifier(database)
    )

    def overrides():
        return dict(
            admin.execute(
                """
                SELECT split_part(setting,'=',1), substring(setting FROM position('=' IN setting)+1)
                FROM pg_db_role_setting s
                JOIN pg_database d ON d.oid = s.setdatabase
                JOIN pg_roles r ON r.oid = s.setrole
                CROSS JOIN unnest(s.setconfig) setting
                WHERE d.datname=%s AND r.rolname=%s AND split_part(setting,'=',1)=ANY(%s)
                """,
                (database, role, list(SETTINGS)),
            )
        )

    def actual():
        with connect_online() as connection:
            names = (*SETTINGS, "log_parameter_max_length_on_error")
            return {
                name: connection.execute(
                    sql.SQL("SHOW {}").format(sql.Identifier(name))
                ).fetchone()[0]
                for name in names
            }

    if not admin.autocommit:
        raise ValueError("DIAGNOSTICS_ADMIN_REQUIRES_AUTOCOMMIT")
    previous = overrides()
    before = actual()
    if before["log_parameter_max_length_on_error"] != "0":
        raise ValueError("DIAGNOSTICS_ERROR_PARAMETERS_MUST_BE_DISABLED")
    existing = [
        pid
        for (pid,) in admin.execute(
            "SELECT pid FROM pg_stat_activity WHERE datname=%s AND usename=%s", (database, role)
        )
    ]
    evidence = {"before": before, "previous_overrides": previous}
    try:
        for name, value in SETTINGS.items():
            admin.execute(
                target + sql.SQL(" SET {} TO {}").format(sql.Identifier(name), sql.Literal(value))
            )
        evidence["active"] = actual()
        if evidence["active"] != {**before, **SETTINGS}:
            raise RuntimeError("DIAGNOSTICS_SETTINGS_NOT_APPLIED")
        yield evidence
    finally:
        for name in SETTINGS:
            setting = sql.Identifier(name)
            if name in previous:
                admin.execute(
                    target + sql.SQL(" SET {} TO {}").format(setting, sql.Literal(previous[name]))
                )
            else:
                admin.execute(target + sql.SQL(" RESET {}").format(setting))
        stopped = admin.execute(
            """
            SELECT pg_terminate_backend(pid, 3000) FROM pg_stat_activity
            WHERE datname=%s AND usename=%s AND NOT (pid = ANY(%s))
            """,
            (database, role, existing),
        ).fetchall()
        if any(not success for (success,) in stopped):
            raise RuntimeError("DIAGNOSTICS_SESSION_TERMINATION_FAILED")
        evidence["restored"] = actual()
        if overrides() != previous or evidence["restored"] != before:
            raise RuntimeError("DIAGNOSTICS_SETTINGS_NOT_RESTORED")
