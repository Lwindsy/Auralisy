package io.auralisy.persistence;

import com.zaxxer.hikari.HikariDataSource;
import io.micrometer.core.instrument.MeterRegistry;
import jakarta.annotation.PreDestroy;
import java.sql.SQLException;
import java.time.Clock;
import java.util.*;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.stereotype.Component;
import org.springframework.transaction.TransactionException;

/** Bounded database diagnostics. Business paths only enqueue the snapshot. */
@Component
public final class DbDiagnostics {
    private static final org.slf4j.Logger LOG = LoggerFactory.getLogger("io.auralisy.diagnostics");
    private static final long INTERVAL_MS = 10_000;
    private static final String SNAPSHOT_SQL =
            """
            WITH waiters AS MATERIALIZED (
              SELECT pid, state, wait_event_type, wait_event, xact_start, query_start
              FROM pg_stat_activity
              WHERE datname = current_database() AND usename = current_user
                AND wait_event_type = 'Lock'
              ORDER BY query_start, pid LIMIT 5
            )
            SELECT pid, state, wait_event_type, wait_event,
              trunc(extract(epoch FROM (statement_timestamp() - xact_start)) * 1000)::bigint,
              trunc(extract(epoch FROM (statement_timestamp() - query_start)) * 1000)::bigint,
              array_to_string((pg_blocking_pids(pid))[1:8], ',')
            FROM waiters
            """;

    public enum Phase {
        RUNTIME("runtime"),
        RECONCILE("reconcile"),
        OBSERVE("observe"),
        ADMISSION("admission"),
        COMMAND("command"),
        CLAIM("claim"),
        SEND("send"),
        UNAVAILABLE("unavailable"),
        LIFECYCLE("lifecycle"),
        DELIVERY("delivery"),
        GRANTS("invocation-grants"),
        USAGE("usage-receipt"),
        BUDGET("budget-state"),
        AUTHORIZE("read-authorization"),
        READ_RECEIPTS("read-receipts"),
        SEEDS("seed-evidence"),
        PREFERENCES("preference-resolution"),
        CANDIDATES("candidate-validation");

        private final String label;

        Phase(String label) {
            this.label = label;
        }
    }

    private final HikariDataSource pool;
    private final MeterRegistry metrics;
    private final Clock clock;
    private final EnumMap<Phase, Long> skippedAt = new EnumMap<>(Phase.class);
    private final AtomicBoolean inFlight = new AtomicBoolean();
    private long snapshotAt = Long.MIN_VALUE;
    private final java.util.concurrent.ExecutorService snapshots =
            Executors.newSingleThreadExecutor(
                    runnable -> {
                        var thread = new Thread(runnable, "auralis-db-diagnostics");
                        thread.setDaemon(true);
                        return thread;
                    });

    public DbDiagnostics(HikariDataSource pool, MeterRegistry metrics, Clock clock) {
        this.pool = pool;
        this.metrics = metrics;
        this.clock = clock;
    }

    public void skipped(Phase phase, Throwable failure, String execution) {
        contention(phase);
        if (allowSkippedLog(phase)) line(phase, "skipped", failure, execution);
        snapshot();
    }

    public void failed(Phase phase, Throwable failure, String execution) {
        line(phase, "failed", failure, execution);
        if (ShortTransactions.isLockContention(failure)) {
            contention(phase);
            snapshot();
        } else if (isDatabaseFailure(failure)) {
            snapshot();
        }
    }

    public void maintenance(Throwable failure) {
        line(Phase.UNAVAILABLE, "maintenance", failure, null);
    }

    private void contention(Phase phase) {
        metrics.counter("auralisy.db.lock_contention", "phase", phase.label).increment();
    }

    synchronized boolean allowSkippedLog(Phase phase) {
        long now = clock.millis();
        var last = skippedAt.get(phase);
        if (last != null && now - last < INTERVAL_MS) return false;
        skippedAt.put(phase, now);
        return true;
    }

    public static boolean isDatabaseFailure(Throwable failure) {
        for (Throwable cause : chain(failure)) {
            if (cause instanceof SQLException
                    || cause instanceof DataAccessException
                    || cause instanceof TransactionException) return true;
        }
        return false;
    }

    public static String sqlStates(Throwable failure) {
        var states = new LinkedHashSet<String>();
        for (Throwable cause : chain(failure)) {
            if (cause instanceof SQLException sql
                    && sql.getSQLState() != null
                    && sql.getSQLState().matches("[0-9A-Z]{5}")) states.add(sql.getSQLState());
            if (states.size() == 5) break;
        }
        return states.isEmpty() ? "-" : String.join(",", states);
    }

    private static List<Throwable> chain(Throwable root) {
        var pending = new ArrayDeque<Throwable>();
        var seen = Collections.newSetFromMap(new IdentityHashMap<Throwable, Boolean>());
        var chain = new ArrayList<Throwable>();
        pending.add(root);
        while (!pending.isEmpty() && chain.size() < 32) {
            var next = pending.removeFirst();
            if (!seen.add(next)) continue;
            chain.add(next);
            if (next.getCause() != null) pending.addLast(next.getCause());
            if (next instanceof SQLException sql && sql.getNextException() != null)
                pending.addLast(sql.getNextException());
        }
        return chain;
    }

    static String format(Phase phase, String outcome, Throwable failure, String execution) {
        return "AURALIS_DB_DIAGNOSTIC phase="
                + phase.label
                + " outcome="
                + outcome
                + " exception="
                + category(failure)
                + " sqlstate="
                + sqlStates(failure)
                + " execution="
                + (execution != null
                                && execution.matches("[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")
                        ? execution
                        : "-");
    }

    private static void line(Phase phase, String outcome, Throwable failure, String execution) {
        LOG.warn("{}", format(phase, outcome, failure, execution));
    }

    private static String category(Throwable failure) {
        String simple = failure.getClass().getSimpleName();
        return simple.matches("[A-Za-z][A-Za-z0-9_]{0,79}") ? simple : "Exception";
    }

    private synchronized void snapshot() {
        long now = clock.millis();
        if ((snapshotAt != Long.MIN_VALUE && now - snapshotAt < INTERVAL_MS)
                || snapshots.isShutdown()
                || !inFlight.compareAndSet(false, true)) return;
        snapshotAt = now;
        snapshots.execute(
                () -> {
                    try {
                        var status = pool.getHikariPoolMXBean();
                        if (status == null || status.getIdleConnections() == 0) {
                            snapshotResult("skipped_busy");
                            return;
                        }
                        var entries = readSnapshot();
                        snapshotResult("ok");
                        LOG.warn(
                                "AURALIS_DB_LOCK_SNAPSHOT result=ok sessions={}",
                                entries.isEmpty() ? "-" : String.join(";", entries));
                    } catch (SQLException | RuntimeException failure) {
                        snapshotResult("failed");
                        LOG.warn(
                                "AURALIS_DB_LOCK_SNAPSHOT result=failed exception={} sqlstate={}",
                                category(failure),
                                sqlStates(failure));
                    } finally {
                        inFlight.set(false);
                    }
                });
    }

    private List<String> readSnapshot() throws SQLException {
        var entries = new ArrayList<String>();
        try (var connection = pool.getConnection()) {
            connection.setReadOnly(true);
            connection.setAutoCommit(false);
            try (var statement = connection.createStatement()) {
                statement.execute("SET LOCAL statement_timeout = '250ms'");
                try (var rows = statement.executeQuery(SNAPSHOT_SQL)) {
                    while (rows.next()) {
                        String blockers = rows.getString(7);
                        entries.add(
                                rows.getInt(1)
                                        + "/"
                                        + label(rows.getString(2))
                                        + "/"
                                        + label(rows.getString(3))
                                        + "/"
                                        + label(rows.getString(4))
                                        + "/"
                                        + age(rows.getString(5))
                                        + "/"
                                        + age(rows.getString(6))
                                        + "/"
                                        + (blockers == null || blockers.isEmpty()
                                                ? "-"
                                                : blockers));
                    }
                }
            } finally {
                connection.rollback();
            }
        }
        return entries;
    }

    private static String label(String value) {
        return value == null || !value.matches("[A-Za-z0-9_ ]{1,48}")
                ? "-"
                : value.replace(' ', '_');
    }

    private static String age(String milliseconds) {
        return milliseconds == null ? "-" : milliseconds;
    }

    private void snapshotResult(String result) {
        metrics.counter("auralisy.db.lock_snapshot", "result", result).increment();
    }

    @PreDestroy
    synchronized void close() {
        snapshots.shutdownNow();
    }
}
